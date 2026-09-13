"""Downloading every picture the catalogue lists ahead of time: Download All Pictures.

The pictures go into the details pane's own cache through ``MediaCache.fetch``,
so they are the files the pane would fetch, kept and refreshed by the same
rules, and asked for at the same pace: one request at a time to each site, at
least a second apart. One worker per site runs at once, so the whole takes
about as long as the site with the most pictures left. A picture already in
the cache and fresh is passed over without a request, so a download that was
stopped carries on where it stopped.

A busy site can answer that it does not have pictures it has, as GitHub's raw
file server did for a quarter of an hour of the first full run. So a worker
whose site misses several pictures in a row pauses before going on, longer
each time it goes on missing, and every picture missed is asked for once more
at the end; only a picture missed both times counts as unavailable.
"""

from __future__ import annotations

import threading
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

from ..jobs.cancellation import is_cancelled, sleep_unless_cancelled
from ..models import MediaItem
from .media import POLITE_INTERVAL, MediaCache, host_key, picture_key

# Every site gets one request a second at most (media.py and http.py).
SECONDS_PER_PICTURE = POLITE_INTERVAL
# Below this many cached pictures of a source, their average says little about its rest.
ESTIMATE_SAMPLE = 100
# Misses in a row from one site before its worker pauses, and the pauses.
BACKOFF_AFTER = 5
BACKOFF_FIRST = 60.0
BACKOFF_MOST = 15 * 60.0

Address = tuple[str, str]  # the picture's address and its source


@dataclass(frozen=True, slots=True)
class PictureCount:
    """How many of the catalogue's pictures are on this computer."""

    total: int
    cached: int
    size: int  # bytes the cached pictures take
    remaining_by_site: dict[str, int] = field(default_factory=dict)
    # By cache folder, one per source: (pictures cached, their bytes, pictures left).
    by_source: dict[str, tuple[int, int, int]] = field(default_factory=dict)

    @property
    def remaining(self) -> int:
        return self.total - self.cached

    @property
    def seconds(self) -> float:
        """How long the rest takes: the site with the most pictures left sets it."""
        return max(self.remaining_by_site.values(), default=0) * SECONDS_PER_PICTURE

    @property
    def estimated_size(self) -> int | None:
        """The space the rest will take, each source's from its own pictures so far.

        The sources' pictures differ tenfold in size (libretro-thumbnails keeps
        full-size screens), so one average for all would be far out. None while
        a source with pictures left has too few cached to go by.
        """
        total = 0.0
        for cached, size, remaining in self.by_source.values():
            if not remaining:
                continue
            if cached < ESTIMATE_SAMPLE:
                return None
            total += size / cached * remaining
        return round(total)


@dataclass(frozen=True, slots=True)
class PictureProgress:
    done: int
    total: int
    fetched: int = 0
    unavailable: int = 0


@dataclass(frozen=True, slots=True)
class PictureSummary:
    total: int
    already: int  # on this computer before
    fetched: int
    unavailable: int  # the site did not have it, or it is not a picture
    stopped: bool = False

    @property
    def done(self) -> int:
        return self.already + self.fetched + self.unavailable


def count(cache: MediaCache, addresses: Sequence[Address]) -> PictureCount:
    remaining: Counter[str] = Counter()
    sizes = cache.cached_sizes()
    # Per cache folder: pictures cached, their bytes, pictures left.
    sources: dict[str, list[int]] = {}
    for url, source in addresses:
        folder, key = picture_key(url, source)
        tally = sources.setdefault(folder, [0, 0, 0])
        size = sizes.get(folder, {}).get(key)
        if size is None:
            remaining[host_key(url)] += 1
            tally[2] += 1
        else:
            tally[0] += 1
            tally[1] += size
    cached = sum(tally[0] for tally in sources.values())
    size = sum(tally[1] for tally in sources.values())
    by_source = {folder: (a, b, c) for folder, (a, b, c) in sources.items()}
    return PictureCount(len(addresses), cached, size, dict(remaining), by_source)


def download(
    cache: MediaCache,
    addresses: Sequence[Address],
    progress: Callable[[PictureProgress], None] | None = None,
    cancel: object | None = None,
) -> PictureSummary:
    """Fetch every picture in ``addresses`` into ``cache``, one worker per site.

    Stops early, with ``stopped`` set, when ``cancel`` is cancelled or the
    cache is switched off (Download Screenshots and Background Information,
    or Online Downloads).
    """
    lock = threading.Lock()
    tally = Counter[str]()
    missed: list[Address] = []
    stopped = threading.Event()

    def report() -> None:
        if progress is not None:
            progress(
                PictureProgress(
                    tally["already"] + tally["fetched"] + tally["unavailable"],
                    len(addresses),
                    tally["fetched"],
                    tally["unavailable"],
                )
            )

    def halted() -> bool:
        if stopped.is_set() or is_cancelled(cancel) or not cache.enabled:
            stopped.set()
            return True
        return False

    def work(site: list[Address], recheck: bool) -> None:
        in_a_row, pause = 0, BACKOFF_FIRST
        for url, source in site:
            if halted():
                return
            had = cache.cached_picture(url, source) is not None
            found, asked = cache.fetch_telling(
                MediaItem("", url, source), cancel=cancel, recheck_missing=recheck
            )
            if found is None and halted():
                return
            with lock:
                if recheck:
                    if found is not None:
                        tally["unavailable"] -= 1
                        tally["fetched"] += 1
                else:
                    outcome = "already" if had and found else "fetched" if found else "unavailable"
                    tally[outcome] += 1
                    # A picture missed twice in a row before is gone; the rest get another try.
                    if found is None and (asked or cache.misses(url, source) < 2):
                        missed.append((url, source))
                report()
            if found is not None or not asked:
                in_a_row, pause = 0, BACKOFF_FIRST
                continue
            in_a_row += 1
            if in_a_row >= BACKOFF_AFTER:
                # The site may be turning requests away: give it a rest.
                if sleep_unless_cancelled(pause, cancel):
                    stopped.set()
                    return
                in_a_row, pause = 0, min(pause * 2, BACKOFF_MOST)

    def run(chosen: Sequence[Address], recheck: bool) -> None:
        by_site: dict[str, list[Address]] = {}
        for address in chosen:
            by_site.setdefault(host_key(address[0]), []).append(address)
        workers = [
            threading.Thread(
                target=work, args=(site, recheck), name=f"pictures-{name}", daemon=True
            )
            for name, site in by_site.items()
        ]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join()

    run(addresses, recheck=False)
    if missed and not stopped.is_set():
        run(missed, recheck=True)
    return PictureSummary(
        len(addresses),
        tally["already"],
        tally["fetched"],
        tally["unavailable"],
        stopped=stopped.is_set() and sum(tally.values()) < len(addresses),
    )

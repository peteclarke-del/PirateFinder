"""Downloading every picture the catalogue lists ahead of time: Download All Pictures.

The pictures go into the details pane's own cache through ``MediaCache.fetch``,
so they are the files the pane would fetch, kept and refreshed by the same
rules, and asked for at the same pace: one request at a time to each site, at
least a second apart. One worker per site runs at once, so the whole takes
about as long as the site with the most pictures left. A picture already in
the cache and fresh is passed over without a request, so a download that was
stopped carries on where it stopped.
"""

from __future__ import annotations

import threading
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

from ..jobs.cancellation import is_cancelled
from ..models import MediaItem
from .media import POLITE_INTERVAL, MediaCache, host_key, picture_key

# Every site gets one request a second at most (media.py and http.py).
SECONDS_PER_PICTURE = POLITE_INTERVAL
# Below this many cached pictures, their average says little about the rest.
ESTIMATE_SAMPLE = 100

Address = tuple[str, str]  # the picture's address and its source


@dataclass(frozen=True, slots=True)
class PictureCount:
    """How many of the catalogue's pictures are on this computer."""

    total: int
    cached: int
    size: int  # bytes the cached pictures take
    remaining_by_site: dict[str, int] = field(default_factory=dict)

    @property
    def remaining(self) -> int:
        return self.total - self.cached

    @property
    def seconds(self) -> float:
        """How long the rest takes: the site with the most pictures left sets it."""
        return max(self.remaining_by_site.values(), default=0) * SECONDS_PER_PICTURE

    @property
    def estimated_size(self) -> int | None:
        """The space the rest will take, from the pictures cached so far; None with too few."""
        if self.cached < ESTIMATE_SAMPLE:
            return None
        return round(self.size / self.cached * self.remaining)


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
    cached = 0
    remaining: Counter[str] = Counter()
    keys = cache.cached_keys()
    for url, source in addresses:
        folder, key = picture_key(url, source)
        if key in keys.get(folder, ()):
            cached += 1
        else:
            remaining[host_key(url)] += 1
    return PictureCount(len(addresses), cached, cache.picture_bytes(), dict(remaining))


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
    by_site: dict[str, list[Address]] = {}
    for address in addresses:
        by_site.setdefault(host_key(address[0]), []).append(address)
    lock = threading.Lock()
    tally = Counter[str]()
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

    def work(site: list[Address]) -> None:
        for url, source in site:
            if stopped.is_set() or is_cancelled(cancel) or not cache.enabled:
                stopped.set()
                return
            had = cache.cached_picture(url, source) is not None
            found = cache.fetch(MediaItem("", url, source), cancel=cancel)
            if found is None and (is_cancelled(cancel) or not cache.enabled):
                stopped.set()
                return
            outcome = "already" if had and found else "fetched" if found else "unavailable"
            with lock:
                tally[outcome] += 1
                report()

    workers = [
        threading.Thread(target=work, args=(site,), name=f"pictures-{name}", daemon=True)
        for name, site in by_site.items()
    ]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join()
    return PictureSummary(
        len(addresses),
        tally["already"],
        tally["fetched"],
        tally["unavailable"],
        stopped=stopped.is_set() and sum(tally.values()) < len(addresses),
    )

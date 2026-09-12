"""What every source importer receives: a polite cached fetcher and the series.

A source module exposes ``INFO: SourceInfo`` and ``collect(ctx) -> Iterable
[DiskRecord]``. It must fetch through ``ctx.fetch`` so that downloads are
cached, throttled per host and available offline on the next build.
"""

from __future__ import annotations

import hashlib
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from .series import GroupRegistry, SeriesRegistry

USER_AGENT = "PirateFinder-catalogue-builder/0.1 (+https://github.com/peteclarke-del/PirateFinder)"


class OfflineError(RuntimeError):
    """A source needed a download that is not cached while building offline."""


@dataclass(slots=True)
class BuildContext:
    cache_dir: Path
    series: SeriesRegistry
    offline: bool = False
    inputs: dict[str, Path] = field(default_factory=dict)  # source id -> local file
    log: Callable[[str], None] = lambda message: print(message, file=sys.stderr)
    groups: GroupRegistry = field(default_factory=GroupRegistry.load)  # data/groups.toml
    _last_request: dict[str, float] = field(default_factory=dict)

    def input(self, source_id: str) -> Path | None:
        """A local file given on the command line in place of a download."""
        return self.inputs.get(source_id)

    def cache_path(self, url: str, name: str | None = None) -> Path:
        digest = hashlib.sha1(url.encode()).hexdigest()[:16]
        host = urllib.parse.urlsplit(url).netloc or "local"
        base = name or Path(urllib.parse.urlsplit(url).path).name or "index"
        return self.cache_dir / host / f"{digest}-{base}"

    def fetch(
        self,
        url: str,
        *,
        name: str | None = None,
        min_interval: float = 1.0,
        max_age_days: float = 7.0,
        retries: int = 4,
    ) -> Path:
        """Download ``url`` into the cache unless a fresh copy is already there."""
        target = self.cache_path(url, name)
        if target.exists():
            age_days = (time.time() - target.stat().st_mtime) / 86400
            if self.offline or age_days <= max_age_days:
                return target
        if self.offline:
            raise OfflineError(f"not cached: {url}")
        target.parent.mkdir(parents=True, exist_ok=True)
        host = urllib.parse.urlsplit(url).netloc
        delay = 5.0
        for attempt in range(retries + 1):
            wait = self._last_request.get(host, 0.0) + min_interval - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            self._last_request[host] = time.monotonic()
            request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            try:
                with urllib.request.urlopen(request, timeout=120) as response:
                    partial = target.with_suffix(target.suffix + ".part")
                    with partial.open("wb") as handle:
                        while chunk := response.read(1 << 16):
                            handle.write(chunk)
                    partial.replace(target)
                    return target
            except urllib.error.HTTPError as error:
                if error.code in (429, 500, 502, 503, 504) and attempt < retries:
                    retry_after = error.headers.get("Retry-After", "")
                    time.sleep(float(retry_after) if retry_after.isdigit() else delay)
                    delay *= 2
                    continue
                raise
            except (urllib.error.URLError, TimeoutError):
                if attempt < retries:
                    time.sleep(delay)
                    delay *= 2
                    continue
                raise
        raise RuntimeError(f"unreachable: {url}")

    def fetch_text(self, url: str, *, encoding: str = "utf-8", **options: object) -> str:
        return self.fetch(url, **options).read_bytes().decode(encoding, errors="replace")

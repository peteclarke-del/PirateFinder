"""Pictures and Wikipedia summaries for the details pane, fetched on demand.

The catalogue lists picture URLs and Wikipedia article titles; nothing is
downloaded until the details pane shows a disc, and nothing at all when the
user switches off "Download screenshots and background information".

Pictures are cached as ``<cache>/media/<source>/<sha1 of the URL>.<ext>``
with a small JSON file beside each one. A cached picture is used for 30 days
and then revalidated with If-None-Match and If-Modified-Since. A picture the
server does not have (404 or 410), or a reply that is not a PNG, GIF or JPEG
of at most 8 MB, is remembered for 7 days so it is not asked for again.
Only one request at a time goes to each host, and requests to the small
hobby sites (Atari Legend, D-Bug, Demozoo) are at least a second apart.

Wikipedia summaries come from the REST API's page summary. The extract, the
article title and its address are kept for 30 days; disambiguation pages are
ignored and the summary's thumbnail is never used, since its licence is
not given with it.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import tempfile
import threading
import time
import urllib.parse
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .. import __version__, paths
from ..branding import HOMEPAGE
from ..models import MediaItem, TriviaItem
from .http import (
    DownloadCancelled,
    Downloader,
    DownloadError,
    HostThrottle,
    HttpReply,
    ReplyTooLarge,
)

DAY = 24 * 60 * 60
FRESH_SECONDS = 30 * DAY
MISSING_SECONDS = 7 * DAY
MAX_PICTURE_BYTES = 8 * 1024 * 1024
MAX_SUMMARY_BYTES = 1024 * 1024
# Small hobby sites: one request a second across all their host names.
POLITE_DOMAINS = ("atarilegend.com", "d-bug.me", "demozoo.org")
POLITE_INTERVAL = 1.0

WIKIPEDIA_SUMMARY_URL = "https://en.wikipedia.org/api/rest_v1/page/summary/{title}"
WIKIPEDIA_SOURCE = "Wikipedia"
WIKIPEDIA_LICENCE = "CC BY-SA 4.0"
# Wikimedia asks every client to say who it is and how to reach its authors.
MEDIA_USER_AGENT = (
    f"PirateFinder/{__version__} (+{HOMEPAGE}; pictures and background information "
    "for a catalogue of Amiga and Atari ST disks)"
)

_MAGIC = (
    (b"\x89PNG\r\n\x1a\n", "png"),
    (b"GIF87a", "gif"),
    (b"GIF89a", "gif"),
    (b"\xff\xd8\xff", "jpg"),
)
_EXTENSIONS = ("png", "gif", "jpg")
_OK = "ok"
_MISSING = "missing"

# Shared by every cache so two panes fetching at once still keep to the rules.
_THROTTLE = HostThrottle(POLITE_INTERVAL)
_HOST_LOCKS: dict[str, threading.Lock] = {}
_HOST_LOCKS_GUARD = threading.Lock()


def picture_type(data: bytes) -> str:
    """ "png", "gif" or "jpg" from the file's first bytes, or "" for anything else."""
    for magic, extension in _MAGIC:
        if data.startswith(magic):
            return extension
    return ""


def host_key(url: str) -> str:
    """The name requests to ``url`` are counted under: a polite domain, or the host."""
    host = (urllib.parse.urlsplit(url).hostname or "").lower()
    for domain in POLITE_DOMAINS:
        if host == domain or host.endswith("." + domain):
            return domain
    return host


def _host_lock(key: str) -> threading.Lock:
    with _HOST_LOCKS_GUARD:
        return _HOST_LOCKS.setdefault(key, threading.Lock())


def _folder_name(source: str) -> str:
    return re.sub(r"[^a-z0-9._-]+", "_", source.lower()).strip("._") or "other"


def picture_key(url: str, source: str) -> tuple[str, str]:
    """The folder and the file name, without extension, a picture is cached under."""
    return _folder_name(source), _digest(url.strip())


def _digest(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8"), usedforsecurity=False).hexdigest()


class MediaCache:
    """Downloads and caches pictures and Wikipedia summaries for the details pane."""

    def __init__(
        self,
        downloader: Any = None,
        cache_dir: Path | None = None,
        *,
        enabled: Callable[[], bool] | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.downloader = downloader or Downloader(user_agent=MEDIA_USER_AGENT)
        self.root = Path(cache_dir) if cache_dir is not None else paths.cache_dir() / "media"
        self._enabled = enabled or (lambda: True)
        self._clock = clock

    @property
    def enabled(self) -> bool:
        return bool(self._enabled())

    # Pictures ------------------------------------------------------------------

    def cached_picture(self, url: str, source: str) -> Path | None:
        """The picture on disk for ``url``, fresh or not, or None; never uses the network."""
        folder = self.root / _folder_name(source)
        key = _digest(url.strip())
        meta = _read_json(folder / f"{key}.json")
        if meta.get("status") != _OK:
            return None
        path = folder / f"{key}.{meta.get('ext', '')}"
        return path if path.is_file() else None

    def cached_sizes(self) -> dict[str, dict[str, int]]:
        """The pictures on disk, as their sizes by key by source folder, from one
        listing of each folder.

        A picture file is written only when a fetch succeeded and removed when
        the site no longer has it, so its presence is enough.
        """
        found: dict[str, dict[str, int]] = {}
        with contextlib.suppress(OSError):
            for folder in os.scandir(self.root):
                if not folder.is_dir():
                    continue
                sizes = found.setdefault(folder.name, {})
                with contextlib.suppress(OSError):
                    for entry in os.scandir(folder.path):
                        stem, _, extension = entry.name.rpartition(".")
                        if extension in _EXTENSIONS:
                            with contextlib.suppress(OSError):
                                sizes[stem] = entry.stat().st_size
        return found

    def picture_bytes(self) -> int:
        """The space the cached pictures take, summaries and notes left out."""
        return sum(sum(sizes.values()) for sizes in self.cached_sizes().values())

    def fetch(self, item: MediaItem, *, cancel: object | None = None) -> Path | None:
        """The cached picture for ``item``, downloading it when needed; None when unavailable."""
        if not self.enabled:
            return None
        url = item.url.strip()
        if urllib.parse.urlsplit(url).scheme.lower() not in ("http", "https"):
            return None
        folder = self.root / _folder_name(item.source)
        key = _digest(url)
        meta_path = folder / f"{key}.json"
        with _host_lock(host_key(url)):
            meta = _read_json(meta_path)
            now = self._clock()
            age = now - float(meta.get("fetched", 0) or 0)
            cached = folder / f"{key}.{meta.get('ext', '')}" if meta.get("status") == _OK else None
            if cached is not None and not cached.is_file():
                cached = None
            if meta.get("status") == _MISSING and age < MISSING_SECONDS:
                return None
            if cached is not None and age < FRESH_SECONDS:
                return cached
            headers = {"User-Agent": MEDIA_USER_AGENT}
            if cached is not None and meta.get("etag"):
                headers["If-None-Match"] = str(meta["etag"])
            if cached is not None and meta.get("last_modified"):
                headers["If-Modified-Since"] = str(meta["last_modified"])
            try:
                reply = self._request(url, headers, MAX_PICTURE_BYTES, cancel)
            except DownloadCancelled:
                return None
            except ReplyTooLarge:
                self._forget_picture(folder, key, url, now)
                return None
            except DownloadError:
                return cached  # a stale picture is better than none while the site is down
            if reply.status == 304 and cached is not None:
                meta["fetched"] = now
                _write_json(meta_path, meta)
                return cached
            if reply.status in (404, 410) or reply.status >= 300:
                self._forget_picture(folder, key, url, now)
                return None
            extension = picture_type(reply.data)
            if not extension:
                self._forget_picture(folder, key, url, now)
                return None
            target = folder / f"{key}.{extension}"
            try:
                _write_bytes(target, reply.data)
            except OSError:
                return None
            for other in _EXTENSIONS:
                if other != extension:
                    with contextlib.suppress(OSError):
                        (folder / f"{key}.{other}").unlink()
            _write_json(
                meta_path,
                {
                    "url": url,
                    "status": _OK,
                    "ext": extension,
                    "fetched": now,
                    "etag": reply.header("ETag"),
                    "last_modified": reply.header("Last-Modified"),
                },
            )
            return target

    def _forget_picture(self, folder: Path, key: str, url: str, now: float) -> None:
        for extension in _EXTENSIONS:
            with contextlib.suppress(OSError):
                (folder / f"{key}.{extension}").unlink()
        _write_json(folder / f"{key}.json", {"url": url, "status": _MISSING, "fetched": now})

    # Wikipedia -----------------------------------------------------------------

    def wikipedia_summary(self, title: str, *, cancel: object | None = None) -> TriviaItem | None:
        """The English Wikipedia summary of the article ``title``, or None."""
        title = title.strip()
        if not title or not self.enabled:
            return None
        meta_path = self.root / "wikipedia" / f"{_digest(title)}.json"
        # Titles keep their parentheses, as Wikipedia writes them; a slash is
        # part of the title, so it is escaped.
        url = WIKIPEDIA_SUMMARY_URL.format(
            title=urllib.parse.quote(title.replace(" ", "_"), safe="()!*',:")
        )
        with _host_lock(host_key(url)):
            meta = _read_json(meta_path)
            now = self._clock()
            age = now - float(meta.get("fetched", 0) or 0)
            if meta.get("status") == _MISSING and age < MISSING_SECONDS:
                return None
            if meta.get("status") == _OK and age < FRESH_SECONDS:
                return _summary_item(meta)
            try:
                reply = self._request(
                    url,
                    {"User-Agent": MEDIA_USER_AGENT, "Accept": "application/json"},
                    MAX_SUMMARY_BYTES,
                    cancel,
                )
            except DownloadCancelled:
                return None
            except DownloadError:
                return _summary_item(meta) if meta.get("status") == _OK else None
            summary = _parse_summary(reply) if reply.status == 200 else None
            if summary is None:
                _write_json(meta_path, {"title": title, "status": _MISSING, "fetched": now})
                return None
            _write_json(meta_path, {**summary, "status": _OK, "fetched": now})
            return _summary_item(summary)

    def _request(
        self, url: str, headers: dict[str, str], limit: int, cancel: object | None
    ) -> HttpReply:
        key = host_key(url)
        if key in POLITE_DOMAINS:
            _THROTTLE.wait(key, cancel)
        return self.downloader.get_reply(url, headers=headers, max_bytes=limit, cancel=cancel)


def _parse_summary(reply: HttpReply) -> dict[str, str] | None:
    try:
        data = json.loads(reply.data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("type") == "disambiguation":
        return None
    extract = str(data.get("extract") or "").strip()
    if not extract:
        return None
    urls = data.get("content_urls")
    desktop = urls.get("desktop") if isinstance(urls, dict) else None
    page = str(desktop.get("page") or "") if isinstance(desktop, dict) else ""
    return {"title": str(data.get("title") or ""), "text": extract, "url": page}


def _summary_item(meta: dict[str, Any]) -> TriviaItem:
    return TriviaItem(
        kind="summary",
        text=str(meta.get("text", "")),
        source=WIKIPEDIA_SOURCE,
        url=str(meta.get("url", "")),
        licence=WIKIPEDIA_LICENCE,
        title=str(meta.get("title", "")),
    )


def _read_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_json(path: Path, data: dict[str, Any]) -> None:
    with contextlib.suppress(OSError):  # a cache that cannot be written only costs a request
        _write_bytes(path, (json.dumps(data, sort_keys=True) + "\n").encode("utf-8"))


def _write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write(data)
        os.replace(temporary, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(temporary)
        raise

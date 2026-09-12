"""Polite HTTP downloads with throttling, retry and resume.

Every request names the application in its User-Agent. Requests to one host
are at least a second apart across the whole process. A 429 or 5xx response
is retried with exponential backoff, honouring ``Retry-After``. A download is
written to ``<target>.part`` first and resumed with a Range request when that
file is already there, then renamed into place.
"""

from __future__ import annotations

import contextlib
import email.utils
import http.client
import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .. import __version__
from ..branding import HOMEPAGE
from ..jobs.cancellation import is_cancelled, sleep_unless_cancelled

USER_AGENT = f"PirateFinder/{__version__} (+{HOMEPAGE})"
RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
CHUNK_SIZE = 64 * 1024
# A server that asks for a longer pause than this is treated as unavailable.
MAX_RETRY_AFTER = 120.0

DownloadProgress = Callable[[int, int | None], None]


class DownloadError(RuntimeError):
    """A download failed; the message is a sentence fit to show the user."""


class DownloadCancelled(DownloadError):
    """The user cancelled a download."""


class HostThrottle:
    """Keeps requests to each host at least ``interval`` seconds apart."""

    def __init__(
        self,
        interval: float = 1.0,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] | None = None,
    ) -> None:
        self.interval = interval
        self._clock = clock
        self._sleep = sleep
        self._next: dict[str, float] = {}
        self._lock = threading.Lock()

    def wait(self, host: str, cancel: object | None = None) -> None:
        """Block until a request to ``host`` is allowed, then reserve the slot."""
        with self._lock:
            now = self._clock()
            start = max(now, self._next.get(host, now))
            self._next[host] = start + self.interval
        delay = start - now
        if delay <= 0:
            return
        if self._sleep is not None:
            self._sleep(delay)
        elif sleep_unless_cancelled(delay, cancel):
            raise DownloadCancelled("The download was cancelled.")


# Shared by every Downloader so a prefetch and a foreground download together
# still keep to one request a second per host.
SHARED_THROTTLE = HostThrottle()


def _retry_after(value: str | None, now: datetime | None = None) -> float | None:
    """Seconds to wait from a Retry-After header in either of its two forms."""
    if not value:
        return None
    value = value.strip()
    if value.isdigit():
        return float(value)
    with contextlib.suppress(TypeError, ValueError, IndexError):
        when = email.utils.parsedate_to_datetime(value)
        if when.tzinfo is None:
            when = when.replace(tzinfo=UTC)
        return max(0.0, (when - (now or datetime.now(UTC))).total_seconds())
    return None


def _content_range_total(value: str | None) -> int | None:
    # "bytes 100-999/1000"
    if not value or "/" not in value:
        return None
    total = value.rsplit("/", 1)[1].strip()
    return int(total) if total.isdigit() else None


def _host(url: str) -> str:
    return urllib.parse.urlsplit(url).netloc.lower() or "local"


class Downloader:
    """Downloads files and small documents on behalf of the application."""

    def __init__(
        self,
        *,
        user_agent: str = USER_AGENT,
        timeout: float = 60.0,
        retries: int = 4,
        backoff: float = 2.0,
        throttle: HostThrottle | None = None,
        sleep: Callable[[float], None] | None = None,
        opener: urllib.request.OpenerDirector | None = None,
    ) -> None:
        self.user_agent = user_agent
        self.timeout = timeout
        self.retries = retries
        self.backoff = backoff
        self.throttle = throttle or SHARED_THROTTLE
        self._sleep = sleep
        self._opener = opener or urllib.request.build_opener()

    def download(
        self,
        url: str,
        target: Path,
        *,
        progress: DownloadProgress | None = None,
        cancel: object | None = None,
    ) -> Path:
        """Download ``url`` to ``target``, resuming a partial file; return ``target``."""
        target = Path(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        partial = target.with_name(target.name + ".part")
        delay = self.backoff
        range_allowed = True
        last_error = ""
        for attempt in range(self.retries + 1):
            if is_cancelled(cancel):
                raise DownloadCancelled("The download was cancelled.")
            offset = partial.stat().st_size if partial.exists() and range_allowed else 0
            headers = {"Range": f"bytes={offset}-"} if offset else {}
            try:
                with self._open(url, headers, cancel) as response:
                    status = getattr(response, "status", 200)
                    if offset and status != 206:
                        offset = 0  # the server ignored the range; start again
                    total = self._total(response, offset)
                    self._copy(response, partial, offset, total, progress, cancel)
                partial.replace(target)
                return target
            except urllib.error.HTTPError as error:
                error.close()
                if error.code == 416 and offset:
                    # The partial file does not fit the server's copy; start again.
                    partial.unlink(missing_ok=True)
                    range_allowed = False
                    continue
                if error.code not in RETRY_STATUSES or attempt == self.retries:
                    raise DownloadError(_http_message(url, error.code)) from error
                wait = self._retry_wait(error, delay)
                last_error = _http_message(url, error.code)
            except DownloadError:
                raise
            except (OSError, http.client.HTTPException) as error:
                last_error = _network_message(url, error)
                if attempt == self.retries:
                    raise DownloadError(last_error) from error
                wait = delay
            self._pause(wait, cancel)
            delay *= 2
        raise DownloadError(last_error or f"The download from {_host(url)} failed.")

    def get_bytes(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        max_bytes: int = 16 * 1024 * 1024,
        cancel: object | None = None,
    ) -> bytes:
        """Fetch a small document into memory, with the same retry rules."""
        delay = self.backoff
        for attempt in range(self.retries + 1):
            if is_cancelled(cancel):
                raise DownloadCancelled("The download was cancelled.")
            try:
                with self._open(url, headers or {}, cancel) as response:
                    data = response.read(max_bytes + 1)
                if len(data) > max_bytes:
                    raise DownloadError(f"The reply from {_host(url)} was larger than expected.")
                return data
            except urllib.error.HTTPError as error:
                error.close()
                if error.code not in RETRY_STATUSES or attempt == self.retries:
                    raise DownloadError(_http_message(url, error.code)) from error
                wait = self._retry_wait(error, delay)
            except DownloadError:
                raise
            except (OSError, http.client.HTTPException) as error:
                if attempt == self.retries:
                    raise DownloadError(_network_message(url, error)) from error
                wait = delay
            self._pause(wait, cancel)
            delay *= 2
        raise DownloadError(f"The request to {_host(url)} failed.")

    def get_json(self, url: str, *, headers: dict[str, str] | None = None, **options: Any) -> Any:
        """Fetch and parse a JSON document."""
        data = self.get_bytes(url, headers=headers, **options)
        try:
            return json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as error:
            raise DownloadError(f"The reply from {_host(url)} could not be read.") from error

    def _open(self, url: str, headers: dict[str, str], cancel: object | None) -> Any:
        scheme = urllib.parse.urlsplit(url).scheme.lower()
        if scheme not in ("http", "https"):
            raise DownloadError(f"Only web addresses can be downloaded, not {url}.")
        self.throttle.wait(_host(url), cancel)
        request = urllib.request.Request(url, headers={"User-Agent": self.user_agent, **headers})
        return self._opener.open(request, timeout=self.timeout)

    def _retry_wait(self, error: urllib.error.HTTPError, delay: float) -> float:
        asked = _retry_after(error.headers.get("Retry-After") if error.headers else None)
        if asked is None:
            return delay
        if asked > MAX_RETRY_AFTER:
            minutes = max(1, round(asked / 60))
            raise DownloadError(
                f"The server asked PirateFinder to wait {minutes} minutes before trying again."
            ) from error
        return max(asked, 0.0)

    def _pause(self, seconds: float, cancel: object | None) -> None:
        if self._sleep is not None:
            self._sleep(seconds)
            if is_cancelled(cancel):
                raise DownloadCancelled("The download was cancelled.")
        elif sleep_unless_cancelled(seconds, cancel):
            raise DownloadCancelled("The download was cancelled.")

    @staticmethod
    def _total(response: Any, offset: int) -> int | None:
        headers = response.headers
        total = _content_range_total(headers.get("Content-Range")) if offset else None
        if total is None:
            length = headers.get("Content-Length")
            if length and length.isdigit():
                total = int(length) + offset
        return total

    @staticmethod
    def _copy(
        response: Any,
        partial: Path,
        offset: int,
        total: int | None,
        progress: DownloadProgress | None,
        cancel: object | None,
    ) -> None:
        done = offset
        last_report = 0.0
        with partial.open("ab" if offset else "wb") as handle:
            if progress is not None:
                progress(done, total)
            while True:
                if is_cancelled(cancel):
                    raise DownloadCancelled("The download was cancelled.")
                chunk = response.read(CHUNK_SIZE)
                if not chunk:
                    break
                handle.write(chunk)
                done += len(chunk)
                now = time.monotonic()
                if progress is not None and now - last_report >= 0.1:
                    last_report = now
                    progress(done, total)
        if total is not None and done < total:
            raise ConnectionError(f"the connection closed after {done} of {total} bytes")
        if progress is not None:
            progress(done, total)


def _http_message(url: str, code: int) -> str:
    host = _host(url)
    if code == 404:
        return f"{host} no longer has this file (HTTP 404)."
    if code in (401, 403):
        return f"{host} refused the download (HTTP {code})."
    if code == 429:
        return f"{host} is limiting requests; try again later (HTTP 429)."
    if code >= 500:
        return f"{host} is having problems; try again later (HTTP {code})."
    return f"The download from {host} failed (HTTP {code})."


def _network_message(url: str, error: BaseException) -> str:
    reason = getattr(error, "reason", error)
    return f"{_host(url)} could not be reached: {reason}."

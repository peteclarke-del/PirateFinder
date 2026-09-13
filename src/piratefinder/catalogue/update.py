"""Check for and install a newer catalogue snapshot.

New catalogues are published as GitHub release assets named by the layout
they hold, ``catalogue-layout<N>.sqlite.gz`` with a ``.sha256`` file beside
it, in a release tagged ``catalogue-YYYY-MM-DD`` (``N`` is
``schema.SCHEMA_VERSION`` of the builder that made it). The check looks only
for the asset of the layout this version reads, so a catalogue it cannot
read is never offered. The download is checked against the published
SHA-256, decompressed next to ``paths.updated_catalogue_path()``, opened
read-only to check its layout again, and only then renamed into place, so a
failed or cancelled update never leaves a partial catalogue behind.

A check that cannot reach the release list, or cannot read it, raises
UpdateError with the reason; None means the check worked and found nothing
newer.
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import re
import sqlite3
import tempfile
import zlib
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .. import paths
from ..branding import RELEASES_API
from ..jobs.cancellation import is_cancelled
from ..online.http import DownloadCancelled, Downloader, DownloadError
from ..online.releases import UpdateCancelled, UpdateError, release_list
from . import schema
from .store import layout_problem

DEFAULT_FEED_URL = RELEASES_API
# A catalogue is tens of megabytes; this refuses a decompression bomb.
MAX_CATALOGUE_BYTES = 2 * 1024 * 1024 * 1024
_DATE = re.compile(r"(\d{4}-\d{2}-\d{2})")
_SHA256 = re.compile(r"\b([0-9a-fA-F]{64})\b")

Progress = Callable[[int, int | None], None]


@dataclass(frozen=True, slots=True)
class UpdateInfo:
    """A published catalogue newer than the installed one."""

    built_at: str  # YYYY-MM-DD
    tag: str
    url: str  # the catalogue.sqlite.gz asset
    sha256_url: str  # the matching .sha256 asset
    asset_name: str = ""
    size: int | None = None
    page_url: str = ""
    notes: str = ""


def asset_name(layout: int = schema.SCHEMA_VERSION) -> str:
    """The release asset of a catalogue in ``layout``: "catalogue-layout2.sqlite.gz"."""
    return f"catalogue-layout{layout}.sqlite.gz"


def _date_in(text: str) -> str:
    match = _DATE.search(text or "")
    return match.group(1) if match else ""


def newest_release(releases: Iterable[Any], current_built_at: str = "") -> UpdateInfo | None:
    """The newest release carrying this version's layout that is newer than ``current_built_at``.

    The build date comes from the tag, ``catalogue-YYYY-MM-DD``.
    """
    current = _date_in(current_built_at)
    name = asset_name()
    best: UpdateInfo | None = None
    for release in releases:
        if not isinstance(release, dict) or release.get("draft") or release.get("prerelease"):
            continue
        assets = {
            str(asset.get("name", "")): asset
            for asset in release.get("assets") or []
            if isinstance(asset, dict)
        }
        asset, checksum = assets.get(name), assets.get(f"{name}.sha256")
        if asset is None or checksum is None:
            continue
        url = str(asset.get("browser_download_url", ""))
        sha256_url = str(checksum.get("browser_download_url", ""))
        tag = str(release.get("tag_name", ""))
        built_at = _date_in(tag)
        if not (url and sha256_url and built_at) or (current and built_at <= current):
            continue
        if best is not None and built_at <= best.built_at:
            continue
        size = asset.get("size")
        best = UpdateInfo(
            built_at=built_at,
            tag=tag,
            url=url,
            sha256_url=sha256_url,
            asset_name=name,
            size=size if isinstance(size, int) else None,
            page_url=str(release.get("html_url", "")),
            notes=str(release.get("body") or ""),
        )
    return best


def check_for_update(
    current_built_at: str,
    feed_url: str = DEFAULT_FEED_URL,
    timeout: float = 20.0,
    *,
    downloader: Downloader | None = None,
) -> UpdateInfo | None:
    """A newer catalogue than ``current_built_at``, or None when there is none.

    Raises UpdateError, with the reason as a sentence, when the release list
    cannot be fetched or read.
    """
    downloader = downloader or Downloader(timeout=timeout, retries=1)
    return newest_release(release_list(feed_url, downloader), current_built_at)


def _published_sha256(text: str, asset_name: str) -> str:
    lines = text.splitlines()
    for line in lines:
        if asset_name and asset_name in line:
            match = _SHA256.search(line)
            if match:
                return match.group(1).lower()
    match = _SHA256.search(text)
    return match.group(1).lower() if match else ""


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1 << 20):
            digest.update(chunk)
    return digest.hexdigest()


def _decompress(source: Path, target: Path, cancel: object | None) -> str:
    """Decompress gzip ``source`` into ``target``; return the SHA-256 of the output."""
    decompressor = zlib.decompressobj(16 + zlib.MAX_WBITS)
    digest = hashlib.sha256()
    written = 0
    with source.open("rb") as reader, target.open("wb") as writer:
        while chunk := reader.read(1 << 20):
            if is_cancelled(cancel):
                raise UpdateCancelled("The catalogue update was cancelled.")
            try:
                data = decompressor.decompress(chunk)
            except zlib.error as error:
                raise UpdateError("The downloaded catalogue is damaged.") from error
            written += len(data)
            if written > MAX_CATALOGUE_BYTES:
                raise UpdateError("The downloaded catalogue is larger than any real catalogue.")
            digest.update(data)
            writer.write(data)
        if not decompressor.eof:
            raise UpdateError("The downloaded catalogue is cut short.")
        writer.flush()
        os.fsync(writer.fileno())
    return digest.hexdigest()


def check_catalogue_file(path: Path) -> str:
    """Open a catalogue read-only and return its build date; raise UpdateError if unusable."""
    try:
        connection = sqlite3.connect(f"{Path(path).resolve().as_uri()}?mode=ro", uri=True)
    except sqlite3.Error as error:
        raise UpdateError("The downloaded catalogue could not be opened.") from error
    try:
        problem = layout_problem(schema.schema_version(connection))
        if problem:
            raise UpdateError(f"The downloaded catalogue {problem}.")
        try:
            connection.execute("SELECT COUNT(*) FROM disks").fetchone()
            row = connection.execute("SELECT value FROM meta WHERE key = 'built_at'").fetchone()
        except sqlite3.Error as error:
            raise UpdateError("The downloaded catalogue is damaged.") from error
        return str(row[0]) if row else ""
    finally:
        connection.close()


def install_update(
    info: UpdateInfo,
    progress: Progress | None = None,
    cancel: object | None = None,
    *,
    downloader: Downloader | None = None,
    target: Path | None = None,
) -> Path:
    """Download, check and install ``info``; return the installed catalogue's path."""
    downloader = downloader or Downloader()
    target = Path(target) if target is not None else paths.updated_catalogue_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    download = target.with_name(f".{target.name}.{info.built_at}.gz")
    partial = download.with_name(download.name + ".part")
    unpacked = ""
    try:
        try:
            checksum_text = downloader.get_bytes(
                info.sha256_url, max_bytes=64 * 1024, cancel=cancel
            )
            expected = _published_sha256(checksum_text.decode("utf-8", "replace"), info.asset_name)
            if not expected:
                raise UpdateError("The published checksum for the catalogue could not be read.")
            downloader.download(info.url, download, progress=progress, cancel=cancel)
        except DownloadCancelled as error:
            raise UpdateCancelled("The catalogue update was cancelled.") from error
        except DownloadError as error:
            raise UpdateError(f"The catalogue could not be downloaded. {error}") from error
        compressed_sha256 = _sha256_file(download)
        handle, unpacked = tempfile.mkstemp(
            prefix=f".{target.name}.", suffix=".new", dir=target.parent
        )
        os.close(handle)
        unpacked_sha256 = _decompress(download, Path(unpacked), cancel)
        if expected not in (compressed_sha256, unpacked_sha256):
            raise UpdateError(
                "The downloaded catalogue does not match its published checksum, "
                "so it was not installed."
            )
        check_catalogue_file(Path(unpacked))
        if is_cancelled(cancel):
            raise UpdateCancelled("The catalogue update was cancelled.")
        os.chmod(unpacked, 0o644)
        os.replace(unpacked, target)
        unpacked = ""
        return target
    except OSError as error:
        raise UpdateError(f"The catalogue could not be saved: {error}") from error
    finally:
        for leftover in (download, partial, Path(unpacked) if unpacked else None):
            if leftover is not None:
                with contextlib.suppress(OSError):
                    leftover.unlink()

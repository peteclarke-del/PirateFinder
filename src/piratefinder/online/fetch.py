"""Fetch a catalogue location into the download folder.

The file is downloaded into the cache, the disk image is taken out of its
container when there is one, and it is checked against the catalogue hash
before it is saved. TOSEC gives MD5, SHA-1 and CRC32 of the raw sector image,
so those are compared with the decoded sectors as well as the file; Atari
Legend gives the SHA-512 of the file as stored. A location with no checksum
of its own and no dump tied to it (a D-Bug or crew-list MSA, an exxos zip) is
accepted when it is a copy of any dump the catalogue lists for its disc,
compared as the library matches its files. An image that fails the check is
deleted. Only when no dump of the disc has a checksum is the image kept, and
the caller told that it could not be checked.
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import tempfile
import unicodedata
import urllib.parse
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from .. import paths
from ..archive_layout import DEFAULT_FOLDERS, platform_folder
from ..images.archives import base_name, header_kind, is_archive_name, is_disk_image_name
from ..library.scanner import entry_from_bytes
from ..library.userdb import LibraryEntry
from ..models import ImageRecord, Location, Platform
from .http import DownloadCancelled, Downloader, DownloadError, DownloadProgress

HASH_KINDS = ("sha512", "sha1", "md5", "crc32")
_UNSAFE = set('<>:"/\\|?*')


class FetchError(RuntimeError):
    """A location could not be fetched or failed its check; the message is for the user."""


def sanitise_name(name: str, fallback: str = "download") -> str:
    """A file or folder name that is safe on Linux and on SMB network shares."""
    name = unicodedata.normalize("NFC", name)
    cleaned = "".join("_" if ch in _UNSAFE or ord(ch) < 32 else ch for ch in name)
    cleaned = cleaned.strip().lstrip(".").rstrip(" .")
    if not cleaned:
        return fallback
    encoded = cleaned.encode("utf-8")
    if len(encoded) > 200:
        suffix = Path(cleaned).suffix if len(Path(cleaned).suffix) <= 8 else ""
        stem = cleaned[: len(cleaned) - len(suffix)]
        while len((stem + suffix).encode("utf-8")) > 200:
            stem = stem[:-1]
        cleaned = stem.rstrip(" .") + suffix
    return cleaned


def _normalise_hash(kind: str, value: str) -> str:
    value = value.strip().lower()
    if kind == "crc32":
        value = value.removeprefix("0x").zfill(8)
    return value


def _inspect_function() -> Callable[[bytes, str], Any]:
    from ..images.inspect import inspect_bytes

    return inspect_bytes


def _archives_module() -> Any:
    from ..images import archives

    return archives


def _member_name(member: Any) -> str:
    return str(getattr(member, "name", member))


def _file_members(archives: Any, path: Path) -> list[str]:
    return [
        _member_name(member)
        for member in archives.members(path)
        if not getattr(member, "is_dir", False) and not _member_name(member).endswith("/")
    ]


def _base(name: str) -> str:
    return base_name(name).casefold()


def _choose_member(names: list[str], wanted: str, image: ImageRecord | None, source: str) -> str:
    """The member to take from an archive, or FetchError when it is ambiguous."""
    if wanted:
        for candidates in (
            [name for name in names if name == wanted],
            [name for name in names if name.casefold() == wanted.casefold()],
            [name for name in names if _base(name) == _base(wanted)],
        ):
            if candidates:
                return candidates[0]
        raise FetchError(f"The archive from {source} does not contain {wanted}.")
    images = [name for name in names if is_disk_image_name(name)]
    if len(images) == 1:
        return images[0]
    if image is not None:
        named = [name for name in images if _base(name) == _base(image.name)]
        if named:
            return named[0]
    if not images:
        raise FetchError(f"The archive from {source} holds no disk image.")
    raise FetchError(
        f"The archive from {source} holds {len(images)} disk images and the catalogue "
        "does not say which one to use."
    )


def _looks_like_archive(path: Path, location: Location) -> bool:
    if location.container:
        return True
    if is_archive_name(path.name) and not is_disk_image_name(path.name):
        return True
    with path.open("rb") as handle:
        head = handle.read(8)
    # A gzip file is left to inspect_bytes, which decodes ADZ and gzipped images.
    return header_kind(head) not in ("", "gz")


def _extract(
    downloaded: Path,
    location: Location,
    image: ImageRecord | None,
    url_name: str,
    archives: Any,
) -> tuple[str, bytes]:
    """The (name, bytes) of the disk image in a downloaded file."""
    source = urllib.parse.urlsplit(location.url).netloc or location.provider
    if not _looks_like_archive(downloaded, location):
        name = location.member or url_name
        if not is_disk_image_name(name) and image is not None and is_disk_image_name(image.name):
            name = image.name
        return base_name(name), downloaded.read_bytes()
    archives = archives or _archives_module()
    try:
        member = _choose_member(_file_members(archives, downloaded), location.member, image, source)
        return base_name(member), archives.read_member(downloaded, member)
    except FetchError:
        raise
    except Exception as error:  # corrupt archives and missing tools become a clear message
        raise FetchError(f"The archive from {source} could not be opened: {error}") from error


def _identify(
    data: bytes, name: str, inspect_bytes: Callable[[bytes, str], Any]
) -> tuple[LibraryEntry, Platform | None]:
    """The image as a library index entry (file and raw sector hashes), and its platform.

    The scanner's ``entry_from_bytes`` makes the entry, so a download is
    hashed exactly as a library file is.
    """
    found: list[Any] = []

    def inspect(image: bytes, image_name: str) -> Any:
        inspection = inspect_bytes(image, image_name)
        found.append(inspection)
        return inspection

    entry = entry_from_bytes(name, "", name, data, inspect)
    return entry, (getattr(found[0], "platform", None) if found else None)


def _expected_hashes(location: Location, image: ImageRecord | None) -> list[tuple[str, str]]:
    kind = location.hash_kind.strip().lower()
    if kind in HASH_KINDS and location.hash_value.strip():
        return [(kind, _normalise_hash(kind, location.hash_value))]
    if image is None:
        return []
    return [
        (kind, _normalise_hash(kind, getattr(image, kind)))
        for kind in HASH_KINDS
        if getattr(image, kind, "")
    ]


def verify(
    entry: LibraryEntry, expected: list[tuple[str, str]], *, container_sha512: str = ""
) -> str:
    """The kind of the expected (kind, value) pair ``entry`` matches.

    MD5, SHA-1 and CRC32 are compared with the file and with the decoded
    sectors, SHA-512 with the file and with the downloaded container. Raises
    FetchError when none matches.
    """
    for kind, value in expected:
        if kind == "sha512":
            candidates = {entry.sha512, container_sha512}
        elif kind in ("md5", "sha1", "crc32"):
            candidates = {getattr(entry, kind), getattr(entry, f"raw_{kind}")}
        else:
            continue
        if value and value in candidates:
            return kind
    kinds = ", ".join(sorted({kind.upper() for kind, _value in expected}))
    raise FetchError(
        f"The downloaded image does not match the catalogue {kinds} checksum, so it was deleted."
    )


def match_dump(entry: LibraryEntry, dumps: Sequence[ImageRecord]) -> ImageRecord | None:
    """The dump of the disc a download with no checksum of its own is a copy of.

    Compared as the library matches its files (``library.match_entry``).
    None when no dump of the disc has a checksum; raises FetchError when
    some have and none matches.
    """
    from ..library.library import DumpSet, match_entry

    known = DumpSet(dumps)
    if not known.hashed:
        return None
    record = match_entry(known, entry)
    if record is None:
        raise FetchError(
            "The downloaded image does not match the checksum of any dump the catalogue "
            "lists for this disc, so it was deleted."
        )
    return record


def _save_name(name: str, image: ImageRecord | None) -> str:
    """The catalogue's file name when it describes the same format, else the member name."""
    suffix = Path(name).suffix.lower()
    if image is not None and image.name:
        catalogue = Path(Path(image.name.replace("\\", "/")).name)
        if catalogue.suffix.lower() == suffix:
            return catalogue.name
        if suffix:
            return catalogue.stem + suffix
    return name


def _same_file(path: Path, data: bytes) -> bool:
    try:
        if path.stat().st_size != len(data):
            return False
        return hashlib.sha1(path.read_bytes()).digest() == hashlib.sha1(data).digest()
    except OSError:
        return False


def _store(folder: Path, file_name: str, data: bytes) -> tuple[Path, bool]:
    """Write ``data`` into ``folder`` without replacing a different file.

    Returns the path and whether an identical file was already there.
    """
    folder.mkdir(parents=True, exist_ok=True)
    stem, suffix = Path(file_name).stem, Path(file_name).suffix
    handle, temporary = tempfile.mkstemp(prefix=".piratefinder-", suffix=".part", dir=folder)
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        number = 1
        while True:
            candidate = folder / (file_name if number == 1 else f"{stem} ({number}){suffix}")
            if candidate.exists():
                if _same_file(candidate, data):
                    return candidate, True
                number += 1
                continue
            try:
                os.link(temporary, candidate)
            except FileExistsError:
                continue
            except OSError:
                # Some network file systems have no hard links.
                if candidate.exists():
                    continue
                os.replace(temporary, candidate)
            return candidate, False
    finally:
        with contextlib.suppress(OSError):
            os.unlink(temporary)


def cache_path_for(url: str, cache_dir: Path | None = None) -> Path:
    """Where a download of ``url`` is kept until it has been checked.

    Each URL gets its own folder so the file keeps its published name, which
    is how a gzip file names the image inside it.
    """
    folder = (cache_dir or paths.cache_dir()) / "downloads"
    name = urllib.parse.unquote(Path(urllib.parse.urlsplit(url).path).name) or "download"
    digest = hashlib.sha1(url.encode("utf-8")).hexdigest()[:16]
    return folder / digest / sanitise_name(name)


def fetch_location(
    location: Location,
    image: ImageRecord | None,
    *,
    download_folder: str | Path,
    folders: Sequence[str] = DEFAULT_FOLDERS,
    platform: Platform | None = None,
    downloader: Downloader | None = None,
    progress: DownloadProgress | None = None,
    cancel: object | None = None,
    notes: list[str] | None = None,
    cache_dir: Path | None = None,
    archives: Any = None,
    inspect_bytes: Callable[[bytes, str], Any] | None = None,
    dumps: Sequence[ImageRecord] = (),
) -> Path:
    """Download, check and save one location; return the saved image's path.

    The image is checked against the location's own checksum, else against
    ``image``, the dump the location is tied to. When neither has one, it
    must be a copy of one of ``dumps``, every dump the catalogue lists for
    the disc; the dump it matched names the saved file and a note. Only when
    no dump of the disc has a checksum is the image kept unchecked.

    The image is saved as ``<download_folder>/<platform>/<type>/<crew>/<file>``,
    with ``folders`` giving the type and crew (see ``archive_layout``).

    Sentences for the user, such as a warning that the image could not be
    checked, are appended to ``notes``. Raises FetchError on failure and
    DownloadCancelled when ``cancel`` is triggered.
    """
    downloader = downloader or Downloader()
    cached = cache_path_for(location.url, cache_dir)
    url_name = urllib.parse.unquote(Path(urllib.parse.urlsplit(location.url).path).name)
    matched: ImageRecord | None = None
    try:
        try:
            downloader.download(location.url, cached, progress=progress, cancel=cancel)
        except DownloadCancelled:
            raise
        except DownloadError as error:
            raise FetchError(str(error)) from error
        name, data = _extract(cached, location, image, url_name, archives)
        entry, found_platform = _identify(data, name, inspect_bytes or _inspect_function())
        expected = _expected_hashes(location, image)
        if expected:
            container_sha512 = ""
            if any(kind == "sha512" for kind, _value in expected):
                container_sha512 = hashlib.sha512(cached.read_bytes()).hexdigest()
            verify(entry, expected, container_sha512=container_sha512)
        else:
            matched = match_dump(entry, dumps)
    finally:
        with contextlib.suppress(OSError):
            cached.unlink()
        with contextlib.suppress(OSError):
            cached.parent.rmdir()  # only when no partial download is left to resume
    folder = Path(download_folder) / platform_folder(platform or found_platform)
    for part, fallback in zip(folders or DEFAULT_FOLDERS, DEFAULT_FOLDERS, strict=False):
        folder /= sanitise_name(part, fallback)
    saved, existed = _store(folder, sanitise_name(_save_name(name, image or matched)), data)
    if notes is not None:
        if matched is not None:
            notes.append(
                "The download has no checksum of its own; it matched the catalogue dump "
                f"{matched.name}."
            )
        elif not expected:
            notes.append(
                "No checksum is known for any dump of this disc, so the download was not checked."
            )
        if existed:
            notes.append(f"An identical copy was already in the download folder: {saved}.")
    return saved

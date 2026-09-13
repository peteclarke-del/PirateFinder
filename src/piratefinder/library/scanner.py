"""Incremental scan of the library folders.

The scan first lists candidate files by suffix, then reads only the files whose
size or modification time changed since the last scan. Archives are opened
with ``images.archives``, which follows one nested archive level and names
nested members ``inner.zip::image.st``, and every disk image found is
inspected with ``images.inspect``, which also checks its boot block for
viruses; the status is kept with the entry. Files that have gone from a folder
that was read completely are removed from the index. Unreadable files,
corrupt archives and missing tools are reported in the summary and never stop
the scan.
"""

from __future__ import annotations

import hashlib
import os
import stat
import time
import zlib
from collections.abc import Callable, Iterable, Iterator, Mapping
from pathlib import Path
from typing import Any

from ..images.archives import ARCHIVE_SUFFIXES, base_name, is_disk_image_name
from ..images.inspect import IMAGE_SUFFIXES, MAX_IMAGE_SIZE
from ..jobs.cancellation import Cancellation, is_cancelled
from ..models import ScanSummary
from .userdb import LibraryEntry, UserDatabase

__all__ = [
    "Cancellation",
    "Scanner",
    "read_image_bytes",
]

CANDIDATE_SUFFIXES = IMAGE_SUFFIXES | ARCHIVE_SUFFIXES

# Anything larger is not a floppy image, even as flux.

# Metadata folders that network storage keeps beside the user's files.
SKIPPED_FOLDERS = frozenset(
    {"@eaDir", "#recycle", "#snapshot", "$RECYCLE.BIN", "System Volume Information", "lost+found"}
)

Progress = Callable[[str, int, int], None]
Identify = Callable[[LibraryEntry], LibraryEntry]


def _archives_module() -> Any:
    from ..images import archives

    return archives


def _inspect_function() -> Callable[[bytes, str], Any]:
    from ..images.inspect import inspect_bytes

    return inspect_bytes


def _member_name(member: Any) -> str:
    return str(getattr(member, "name", member))


def _member_size(member: Any) -> int | None:
    size = getattr(member, "size", None)
    return size if isinstance(size, int) else None


def read_image_bytes(path: str | Path, member: str = "", archives: Any = None) -> bytes:
    """The bytes of a library image: the file itself, or a member of an archive."""
    if not member:
        return Path(path).read_bytes()
    archives = archives or _archives_module()
    return archives.read_member(Path(path), member)


def file_hashes(data: bytes) -> dict[str, str]:
    """CRC32, MD5, SHA-1 and SHA-512 of ``data`` as lower-case hex."""
    return {
        "crc32": f"{zlib.crc32(data) & 0xFFFFFFFF:08x}",
        "md5": hashlib.md5(data).hexdigest(),
        "sha1": hashlib.sha1(data).hexdigest(),
        "sha512": hashlib.sha512(data).hexdigest(),
    }


def _hash_values(source: Any) -> dict[str, str]:
    """Hashes from a mapping or an object with hash attributes."""
    if source is None:
        return {}
    result: dict[str, str] = {}
    for kind in ("crc32", "md5", "sha1", "sha512"):
        value = source.get(kind) if isinstance(source, Mapping) else getattr(source, kind, None)
        if value:
            result[kind] = str(value).lower()
    return result


def entry_from_bytes(
    path: str,
    member: str,
    name: str,
    data: bytes,
    inspect_bytes: Callable[[bytes, str], Any],
) -> LibraryEntry:
    """Inspect one image and turn the result into an index entry."""
    stored = file_hashes(data)
    suffix_format = Path(name).suffix.lower().lstrip(".")
    try:
        inspection = inspect_bytes(data, name)
    except Exception as error:  # an undecodable image stays in the library, unmatched
        return LibraryEntry(
            path=path,
            member=member,
            size=len(data),
            format=suffix_format,
            **stored,
            error=f"The image could not be decoded: {error}",
        )
    reported = _hash_values(getattr(inspection, "hashes", None))
    raw_hashes = _hash_values(getattr(inspection, "raw_hashes", None))
    raw = getattr(inspection, "raw", None)
    listing = getattr(inspection, "listing", ()) or ()
    image_format = getattr(inspection, "format", "") or suffix_format
    report = getattr(inspection, "virus", None)
    status = getattr(report, "status", "")
    return LibraryEntry(
        path=path,
        member=member,
        size=len(data),
        format=str(getattr(image_format, "value", image_format)).lower(),
        crc32=reported.get("crc32", stored["crc32"]),
        md5=reported.get("md5", stored["md5"]),
        sha1=reported.get("sha1", stored["sha1"]),
        sha512=reported.get("sha512", stored["sha512"]),
        raw_crc32=raw_hashes.get("crc32", ""),
        raw_md5=raw_hashes.get("md5", ""),
        raw_sha1=raw_hashes.get("sha1", ""),
        raw_size=len(raw) if isinstance(raw, bytes | bytearray) else None,
        volume_label=str(getattr(inspection, "volume_label", "") or ""),
        listing=tuple(str(item) for item in listing),
        boot_status=str(getattr(status, "value", status) or ""),
        boot_name=str(getattr(report, "name", "") or ""),
    )


class Scanner:
    """Indexes library folders into the user database."""

    def __init__(
        self,
        userdb: UserDatabase,
        identify: Identify | None = None,
        *,
        inspect_bytes: Callable[[bytes, str], Any] | None = None,
        archives: Any = None,
    ) -> None:
        self.userdb = userdb
        self.identify = identify or (lambda entry: entry)
        self._inspect_bytes = inspect_bytes
        self._archives = archives

    @property
    def archives(self) -> Any:
        """The archive module in use, loaded on first use."""
        if self._archives is None:
            self._archives = _archives_module()
        return self._archives

    @property
    def inspect_bytes(self) -> Callable[[bytes, str], Any]:
        """The inspection function in use, loaded on first use."""
        if self._inspect_bytes is None:
            self._inspect_bytes = _inspect_function()
        return self._inspect_bytes

    def scan(
        self,
        folders: Iterable[str | Path],
        progress: Progress | None = None,
        cancel: object | None = None,
    ) -> ScanSummary:
        """Bring the index up to date with ``folders`` and report what changed."""
        started = time.monotonic()
        roots = list(dict.fromkeys(_normalise_folder(folder) for folder in folders))
        errors: list[str] = []
        unreadable: set[str] = set()
        candidates: dict[str, os.stat_result] = {}

        def report(message: str, current: int, total: int) -> None:
            if progress is not None:
                progress(message, current, total)

        for root in roots:
            if is_cancelled(cancel):
                break
            if not os.path.isdir(root):
                errors.append(f"The folder {root} could not be read, so its files were kept.")
                unreadable.add(root)
                continue
            report(f"Looking for disk images in {root}", len(candidates), 0)
            for path, info in _walk(root, errors, unreadable, cancel):
                candidates[path] = info
                if len(candidates) % 200 == 0:
                    report(f"Looking for disk images in {root}", len(candidates), 0)

        known = self.userdb.scanned_files()
        new = 0
        unchanged: list[str] = []
        total = len(candidates)
        for index, (path, info) in enumerate(sorted(candidates.items())):
            if is_cancelled(cancel):
                break
            previous = known.get(path)
            if (
                previous is not None
                and previous[0] == info.st_size
                and previous[1] == info.st_mtime
                and not previous[2]
            ):
                unchanged.append(path)
                continue
            report(f"Reading {os.path.basename(path)}", index + 1, total)
            entries, file_errors = self.index_file(path)
            errors.extend(file_errors)
            new += self.userdb.store_file(
                path, info.st_size, info.st_mtime, entries, "; ".join(file_errors)
            )
        if unchanged:
            self.userdb.mark_seen(unchanged)

        cancelled = is_cancelled(cancel)
        removed = 0
        if not cancelled:
            gone = [
                path
                for path in known
                if path not in candidates
                and _under_any(path, roots)
                and not _under_any(path, unreadable)
            ]
            removed = self.userdb.remove_files(gone)

        images = matched = 0
        for root in roots:
            found, found_matched = self.userdb.counts_under(root)
            images += found
            matched += found_matched
        report("Scan finished" if not cancelled else "Scan cancelled", total, total)
        return ScanSummary(
            folders=tuple(roots),
            files_seen=len(candidates),
            images_found=images,
            matched=matched,
            unmatched=images - matched,
            new=new,
            removed=removed,
            errors=tuple(errors),
            seconds=time.monotonic() - started,
            cancelled=cancelled,
        )

    def index_file(self, path: str | Path) -> tuple[list[LibraryEntry], list[str]]:
        """Inspect every image in one file; return the entries and any errors."""
        path = str(path)
        entries: list[LibraryEntry] = []
        errors: list[str] = []
        try:
            for member, name, data in self._images_in(Path(path), errors):
                entry = entry_from_bytes(path, member, name, data, self.inspect_bytes)
                try:
                    entry = self.identify(entry)
                except Exception as error:  # a matching fault must not lose the file
                    errors.append(f"{path}: the image could not be matched: {error}")
                entries.append(entry)
        except ImportError as error:
            errors.append(f"{path}: image support is not installed: {error}")
        except MemoryError:
            errors.append(f"{path}: the file is too large to read.")
        except Exception as error:  # unreadable files and archive faults are reported
            errors.append(f"{path}: {_describe(error)}")
        return entries, errors

    def _images_in(self, path: Path, errors: list[str]) -> Iterator[tuple[str, str, bytes]]:
        """Yield (member, name, data) for every image in a file."""
        if is_disk_image_name(path.name):
            if path.stat().st_size > MAX_IMAGE_SIZE:
                errors.append(f"{path}: the file is too large to be a floppy image.")
                return
            yield "", path.name, path.read_bytes()
            return
        archives = self.archives

        def member_error(name: str, message: str) -> None:
            errors.append(f"{path}: {name}: {message}")

        iter_members = getattr(archives, "iter_members", None)
        if iter_members is not None:
            # Reads the archive once, which matters for solid 7z files.
            for member, data in iter_members(path, on_error=member_error):
                name = _member_name(member)
                yield name, base_name(name), data
            return
        for member in archives.members(path):
            name = _member_name(member)
            if name.endswith("/") or not is_disk_image_name(name):
                continue
            size = _member_size(member)
            if size is not None and size > MAX_IMAGE_SIZE:
                member_error(name, "the member is too large to be a floppy image.")
                continue
            try:
                yield name, base_name(name), archives.read_member(path, name)
            except Exception as error:  # one unreadable member does not spoil the rest
                member_error(name, _describe(error))


def _describe(error: BaseException) -> str:
    text = str(error).strip()
    if isinstance(error, PermissionError):
        return "permission denied."
    if isinstance(error, FileNotFoundError):
        return "the file disappeared during the scan."
    return text or error.__class__.__name__


def _normalise_folder(folder: str | Path) -> str:
    return os.path.normpath(os.path.abspath(os.path.expanduser(str(folder))))


def _under_any(path: str, folders: Iterable[str]) -> bool:
    return any(
        path == folder or path.startswith(folder.rstrip(os.sep) + os.sep) for folder in folders
    )


def _walk(
    root: str, errors: list[str], unreadable: set[str], cancel: object | None
) -> Iterator[tuple[str, os.stat_result]]:
    """Candidate files under ``root``, following links without looping."""
    visited: set[tuple[int, int]] = set()
    pending = [root]
    while pending:
        if is_cancelled(cancel):
            return
        folder = pending.pop()
        try:
            info = os.stat(folder)
        except OSError as error:
            errors.append(f"The folder {folder} could not be read: {_describe(error)}")
            unreadable.add(folder)
            continue
        key = (info.st_dev, info.st_ino)
        if key in visited:
            continue
        visited.add(key)
        try:
            with os.scandir(folder) as iterator:
                children = list(iterator)
        except OSError as error:
            errors.append(f"The folder {folder} could not be read: {_describe(error)}")
            unreadable.add(folder)
            continue
        subfolders: list[str] = []
        for child in children:
            if child.name.startswith(".") or child.name in SKIPPED_FOLDERS:
                continue
            try:
                if child.is_dir():
                    subfolders.append(child.path)
                elif (
                    child.is_file()
                    and os.path.splitext(child.name)[1].lower() in CANDIDATE_SUFFIXES
                ):
                    child_info = child.stat()
                    if stat.S_ISREG(child_info.st_mode):
                        yield child.path, child_info
            except OSError as error:
                errors.append(f"{child.path}: {_describe(error)}")
        pending.extend(sorted(subfolders, reverse=True))

"""The library: local disk images matched against the catalogue.

Matching tries the raw sector image first, as TOSEC lists it, then the file as
stored, as Atari Legend lists it: raw MD5, raw SHA-1, raw CRC32 with size,
file SHA-512 and file MD5. Images that cannot be decoded to raw sectors are
matched with their file hashes in the raw steps, since for those formats the
catalogue hashes the file itself.
"""

from __future__ import annotations

import os
import threading
from collections.abc import Callable, Iterable
from dataclasses import asdict, replace
from datetime import datetime
from pathlib import Path
from typing import Any

from ..models import Availability, ImageRecord, LocalFile, ScanSummary
from .scanner import Progress, Scanner, base_name, read_image_bytes
from .userdb import LibraryEntry, UserDatabase


def _folder_prefix(folder: str | Path) -> str:
    root = os.path.normpath(os.path.abspath(os.path.expanduser(str(folder))))
    return root.rstrip(os.sep) + os.sep


def match_entry(catalogue: Any, entry: LibraryEntry) -> ImageRecord | None:
    """The catalogue image an index entry is a copy of, or None."""
    decoded = bool(entry.raw_md5 or entry.raw_sha1 or entry.raw_crc32)
    md5 = entry.raw_md5 if decoded else entry.md5
    sha1 = entry.raw_sha1 if decoded else entry.sha1
    crc32 = entry.raw_crc32 if decoded else entry.crc32
    size = entry.raw_size if decoded and entry.raw_size is not None else entry.size
    attempts: list[dict[str, Any]] = []
    if md5:
        attempts.append({"md5": md5})
    if sha1:
        attempts.append({"sha1": sha1})
    if crc32 and size:
        attempts.append({"crc32": crc32, "size": size})
    if entry.sha512:
        attempts.append({"sha512": entry.sha512})
    if entry.md5 and entry.md5 != md5:
        attempts.append({"md5": entry.md5})
    for hashes in attempts:
        record = catalogue.match_image(**hashes)
        if record is not None:
            return record
    return None


def display_name(path: str, member: str = "") -> tuple[str, dict[str, Any]]:
    """A readable name for a file or member, and its TOSEC fields when it has them.

    A TOSEC style name is shortened with ``catalogue.naming.tidy_label``;
    any other name is shown as its file name without the extension.
    """
    name = base_name(member) if member else Path(path).name
    readable = Path(name).stem.replace("_", " ").strip() or name
    try:
        from ..catalogue.naming import parse_tosec_name, tidy_label
    except ImportError:
        return readable, {}
    try:
        parsed = parse_tosec_name(name)
    except Exception:  # a name the parser cannot read is simply not TOSEC style
        return readable, {}
    if not (parsed.date or parsed.flags or parsed.extra):
        return readable, {}
    fields = {
        key: list(value) if isinstance(value, tuple) else value
        for key, value in asdict(parsed).items()
    }
    return tidy_label(name) or readable, fields


class Library:
    """Local images: scanning, matching, availability and unmatched-file search."""

    def __init__(
        self,
        userdb: UserDatabase,
        catalogue: Any,
        *,
        inspect_bytes: Callable[[bytes, str], Any] | None = None,
        archives: Any = None,
    ) -> None:
        self.userdb = userdb
        self.catalogue = catalogue
        self._scanner = Scanner(
            userdb, self._identify, inspect_bytes=inspect_bytes, archives=archives
        )
        self._online_cache: dict[frozenset[str], set[int]] = {}
        self._lock = threading.Lock()

    def set_catalogue(self, catalogue: Any) -> None:
        """Use a newer catalogue. Call ``rematch`` afterwards to match files again."""
        with self._lock:
            self.catalogue = catalogue
            self._online_cache.clear()

    def scan(
        self,
        folders: Iterable[str | Path],
        progress: Progress | None = None,
        controller: object | None = None,
    ) -> ScanSummary:
        """Scan library folders incrementally; ``controller`` may cancel it."""
        return self._scanner.scan(folders, progress, controller)

    def files_for_disk(self, disk_id: int) -> list[LocalFile]:
        """Local images matched to one catalogue disk."""
        return [entry.to_local() for entry in self.userdb.entries(disk_id=disk_id)]

    def files_for_image(self, image_id: int) -> list[LocalFile]:
        """Local copies of one catalogue image."""
        return [entry.to_local() for entry in self.userdb.entries(image_id=image_id)]

    def availability(
        self, disk_ids: Iterable[int], providers: Iterable[str]
    ) -> dict[int, Availability]:
        """For each disk: local, online with one of ``providers``, or missing."""
        ids = list(dict.fromkeys(disk_ids))
        local = self.userdb.disks_present(ids)
        online = self._online_disks(providers)
        result: dict[int, Availability] = {}
        for disk_id in ids:
            if disk_id in local:
                result[disk_id] = Availability.LOCAL
            elif disk_id in online:
                result[disk_id] = Availability.ONLINE
            else:
                result[disk_id] = Availability.MISSING
        return result

    def search_unmatched(self, text: str, limit: int = 200) -> list[LocalFile]:
        """Unmatched images by file name, volume label or file listing; all when blank."""
        return [entry.to_local() for entry in self.userdb.search_unmatched(text, limit)]

    def add_file(self, path: Path) -> list[LocalFile]:
        """Index one file straight away, for example after a download."""
        target = os.path.normpath(os.path.abspath(str(path)))
        info = os.stat(target)
        entries, errors = self._scanner.index_file(target)
        self.userdb.store_file(target, info.st_size, info.st_mtime, entries, "; ".join(errors))
        return [entry.to_local() for entry in entries]

    def rematch(self) -> int:
        """Match every indexed image again; return how many are matched afterwards."""
        with self._lock:
            catalogue = self.catalogue
        changes: list[tuple[int, int | None, int | None]] = []
        matched = 0
        for entry in self.userdb.entries():
            record = match_entry(catalogue, entry)
            image_id, disk_id = (record.id, record.disk_id) if record else (None, None)
            matched += disk_id is not None
            if entry.id is not None and (image_id, disk_id) != (entry.image_id, entry.disk_id):
                changes.append((entry.id, image_id, disk_id))
        if changes:
            self.userdb.set_matches(changes)
        return matched

    def forget_folder(self, folder: str | Path) -> int:
        """Remove every file below a folder that is no longer in the library."""
        prefix = _folder_prefix(folder)
        paths = [path for path in self.userdb.scanned_files() if path.startswith(prefix)]
        return self.userdb.remove_files(paths)

    def forget_outside(self, folders: Iterable[str | Path]) -> int:
        """Remove every file that is not below one of ``folders``.

        Called before a scan so that folders the user took out of the library
        stop contributing results, without touching anything still listed.
        """
        prefixes = tuple(_folder_prefix(folder) for folder in folders)
        paths = [path for path in self.userdb.scanned_files() if not path.startswith(prefixes)]
        return self.userdb.remove_files(paths) if paths else 0

    def read_bytes(self, local: LocalFile) -> bytes:
        """The image bytes of a local file, opening archives as needed."""
        return read_image_bytes(local.path, local.member, self._scanner.archives)

    def last_scan(self) -> str:
        """When a scan last saw a file, as local ISO time, or "" before the first scan."""
        seen = self.userdb.last_scan()
        if seen is None:
            return ""
        return datetime.fromtimestamp(seen).isoformat(timespec="seconds")

    def stats(self) -> dict[str, int]:
        """Counts for the Library page: images, matched, unmatched, duplicates, folders."""
        return self.userdb.library_counts()

    def _identify(self, entry: LibraryEntry) -> LibraryEntry:
        with self._lock:
            catalogue = self.catalogue
        name, parsed = display_name(entry.path, entry.member)
        record = match_entry(catalogue, entry) if catalogue is not None else None
        return replace(
            entry,
            image_id=record.id if record else None,
            disk_id=record.disk_id if record else None,
            display_name=name,
            parsed=parsed,
        )

    def _online_disks(self, providers: Iterable[str]) -> set[int]:
        key = frozenset(providers)
        if not key:
            return set()
        with self._lock:
            cached = self._online_cache.get(key)
            catalogue = self.catalogue
        if cached is not None:
            return cached
        found = set(catalogue.disks_with_locations(key))
        with self._lock:
            if self.catalogue is catalogue:
                self._online_cache[key] = found
        return found

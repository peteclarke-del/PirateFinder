"""The library: local disk images matched against the catalogue.

Matching tries the raw sector image first, as TOSEC lists it, then the file as
stored, as Atari Legend lists it: raw MD5, raw SHA-1, raw CRC32 with size,
file SHA-512 and file MD5. Images that cannot be decoded to raw sectors are
matched with their file hashes in the raw steps, since for those formats the
catalogue hashes the file itself.

A file PirateFinder wrote by removing a boot block virus (``clean_file``)
usually matches no catalogue dump any more; it stays with the disk it was
made from for as long as its sectors are unchanged.

Each image's boot block is checked when it is scanned, with the virus data
of the day. The user database keeps the fingerprint of that data
(``images.virus.fingerprint``); when PirateFinder's built-in virus data or
detection code changes, or the Amiga Bootblock Reader brainfile is
installed, the fingerprint differs and ``recheck_boot_blocks`` checks every
checked boot block again. It reads the boot block only: the first kilobyte
of a plain ADF or ST file, or the sectors a packed image or archive member
decodes to, and hashes nothing.
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import shutil
import stat
import tempfile
import threading
from collections.abc import Callable, Iterable, Sequence
from dataclasses import asdict, dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Any

from ..archive_layout import DEFAULT_FOLDERS, platform_folder
from ..images.archives import image_file_name
from ..images.inspect import (
    BOOT_BLOCK_SIZE,
    boot_block,
    format_platform,
    gzip_member_name,
    sector_suffix,
)
from ..jobs.cancellation import is_cancelled
from ..models import Availability, BootRecheck, ImageRecord, LocalFile, Platform, ScanSummary
from ..online.fetch import sanitise_name
from .scanner import Progress, Scanner, read_image_bytes
from .userdb import LibraryEntry, UserDatabase

# Formats a cleaned image is written back in, in place; the others get a new file.
REWRITTEN_FORMATS = frozenset({"adf", "st", "msa"})
# Formats whose file is the sectors themselves, so the boot block is its first bytes.
SECTOR_FORMATS = frozenset({"adf", "st"})
# The user database meta key holding the virus data fingerprint the boot blocks
# were last checked with.
BOOT_FINGERPRINT = "boot_fingerprint"


@dataclass(frozen=True, slots=True)
class CleanNames:
    """The file names cleaning one library image writes, before any numbering."""

    cleaned: str  # the cleaned image; the file's own name when it is rewritten in place
    backup: str = ""  # the copy of the original kept beside it; "" when the original is unchanged


def clean_names(local: LocalFile, image_format: str, platform: Platform | None) -> CleanNames:
    """What ``Library.clean_file`` writes for ``local``, an ``image_format`` image for ``platform``.

    A plain ADF, ST or MSA file is rewritten in place and the original kept as "<name>.bak".
    Any other file, and any image inside an archive, is saved as a new "<stem> (cleaned)" file,
    in its own format when that is ADF, ST or MSA and as a sector image for the platform
    (".adf" or ".st") otherwise. The stem is the image's without a gzip suffix, so "game.st.gz"
    gives "game (cleaned).st". An existing file is never replaced: a name that is taken gets
    the first free number, as in "game (cleaned) (2).st" or "game.st.2.bak".
    """
    name = image_file_name(local.path, local.member)
    image_format = image_format.lower()
    if image_format in REWRITTEN_FORMATS and not local.member:
        return CleanNames(name, f"{name}.bak")
    suffix = f".{image_format}" if image_format in REWRITTEN_FORMATS else sector_suffix(platform)
    cleaned = f"{Path(gzip_member_name(name)).stem} (cleaned){suffix}"
    return CleanNames(sanitise_name(cleaned) if local.member else cleaned)


def local_name(local: LocalFile) -> str:
    """The name a library file is shown and queued under: its display name, else its file name."""
    return local.display_name or image_file_name(local.path, local.member)


def _folder_prefix(folder: str | Path) -> str:
    root = os.path.normpath(os.path.abspath(os.path.expanduser(str(folder))))
    return root.rstrip(os.sep) + os.sep


def match_entry(catalogue: Any, entry: LibraryEntry) -> ImageRecord | None:
    """The catalogue image an index entry is a copy of, or None.

    ``catalogue`` is anything with ``match_image``: the whole catalogue, or a
    ``DumpSet`` holding the dumps of one disc.
    """
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


class DumpSet:
    """The catalogue dumps of one disc, searched as ``Catalogue.match_image`` searches them all.

    A download that carries no checksum of its own is checked with
    ``match_entry(DumpSet(dumps), entry)``: it must be a copy of one of the
    dumps the catalogue lists for its disc. Good dumps win over bad ones,
    then the lowest rank, as in the catalogue.
    """

    def __init__(self, records: Iterable[ImageRecord]) -> None:
        self.records = sorted(records, key=lambda record: (record.bad, record.rank, record.id))

    @property
    def hashed(self) -> bool:
        """True when at least one dump has a checksum a download can be compared with."""
        return any(
            record.md5 or record.sha1 or record.sha512 or (record.crc32 and record.size)
            for record in self.records
        )

    def match_image(
        self,
        *,
        md5: str = "",
        sha1: str = "",
        sha512: str = "",
        crc32: str = "",
        size: int | None = None,
    ) -> ImageRecord | None:
        """The dump with one of these hashes, trying MD5, SHA-1, SHA-512, then CRC32 with size."""
        for kind, value in (("md5", md5), ("sha1", sha1), ("sha512", sha512)):
            if value:
                for record in self.records:
                    if getattr(record, kind).lower() == value.lower():
                        return record
        if crc32 and size is not None:
            for record in self.records:
                if record.crc32.lower() == crc32.lower() and record.size == size:
                    return record
        return None


def _sector_sha1(entry: LibraryEntry) -> str:
    return (entry.raw_sha1 or entry.sha1).lower()


def display_name(path: str, member: str = "") -> tuple[str, dict[str, Any]]:
    """A readable name for a file or member, and its TOSEC fields when it has them.

    A TOSEC style name is shortened with ``catalogue.naming.tidy_label``;
    any other name is shown as its file name without the extension.
    """
    name = image_file_name(path, member)
    readable = Path(name).stem.replace("_", " ").strip() or name
    try:
        from ..catalogue.naming import parse_tosec_name, tidy_label
    except ImportError:
        return readable, {}
    try:
        parsed = parse_tosec_name(name)
    except Exception:  # a name the parser cannot read is not TOSEC style
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
        self._boot_lock = threading.Lock()  # one boot block check at a time

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
        """Scan library folders incrementally; ``controller`` may cancel it.

        The boot blocks already indexed are checked again first when the
        virus data changed since they were checked (``recheck_boot_blocks``).
        """
        self.recheck_boot_blocks(progress, controller)
        return self._scanner.scan(folders, progress, controller)

    def recheck_boot_blocks(
        self, progress: Progress | None = None, cancel: object | None = None
    ) -> BootRecheck | None:
        """Check every indexed boot block again when the virus data changed.

        None when the boot blocks were checked with the virus data in use
        now. Otherwise each image whose boot block was checked is read again
        (the boot block only, see the module's description) and its status
        stored when it changed; then the fingerprint of the virus data is
        kept, unless ``cancel`` stopped the check. An image that cannot be
        read now keeps its status and is read in full by the next scan.
        """
        from ..images import virus

        with self._boot_lock:
            wanted = virus.fingerprint()
            if self.userdb.meta(BOOT_FINGERPRINT) == wanted:
                return None
            entries = self.userdb.checked_boot_blocks()
            changes: list[tuple[int, str, str]] = []
            unreadable: list[str] = []
            checked = 0
            for index, entry in enumerate(entries):
                if is_cancelled(cancel):
                    break
                if progress is not None:
                    name = image_file_name(entry.path, entry.member)
                    progress(f"Checking the boot block of {name}", index + 1, len(entries))
                found = self._boot_block(entry)
                if found is None:
                    unreadable.append(entry.path)
                    continue
                checked += 1
                report = virus.detect(*found)
                status, name = report.status.value, report.name
                if entry.id is not None and (status, name) != (entry.boot_status, entry.boot_name):
                    changes.append((entry.id, status, name))
            cancelled = is_cancelled(cancel)
            self.userdb.set_boot_statuses(changes)
            self.userdb.read_again(dict.fromkeys(unreadable))
            if not cancelled:
                self.userdb.set_meta(BOOT_FINGERPRINT, wanted)
            return BootRecheck(checked, len(changes), len(unreadable), cancelled)

    def _boot_block(self, entry: LibraryEntry) -> tuple[bytes, Platform] | None:
        """The boot block of an indexed image and its platform; None when it cannot be read."""
        try:
            if not entry.member and entry.format in SECTOR_FORMATS:
                with open(entry.path, "rb") as handle:
                    return handle.read(BOOT_BLOCK_SIZE), format_platform(entry.format)
            data = read_image_bytes(entry.path, entry.member, self._scanner.archives)
        except Exception:  # a file that went, or an archive that no longer opens
            return None
        return boot_block(data, image_file_name(entry.path, entry.member))

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

    def local_disks(self) -> set[int]:
        """Every catalogue disk with at least one local image."""
        return self.userdb.local_disk_ids()

    def search_unmatched(self, text: str, limit: int = 200) -> list[LocalFile]:
        """Unmatched images by file name, volume label or file listing; all when blank."""
        return [entry.to_local() for entry in self.userdb.search_unmatched(text, limit)]

    def infected_files(self, limit: int = 200) -> list[LocalFile]:
        """Images whose boot block held a virus when it was last checked, by name."""
        return [entry.to_local() for entry in self.userdb.infected_entries(limit)]

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
        origins = self.userdb.cleaned_origins()
        for entry in self.userdb.entries():
            record = match_entry(catalogue, entry)
            image_id, disk_id = (record.id, record.disk_id) if record else (None, None)
            if record is None and not entry.member:
                origin = origins.get(entry.path)
                if origin is not None and origin[0] == _sector_sha1(entry):
                    disk_id = origin[1]
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
        disk_id = record.disk_id if record else None
        if record is None and not entry.member:
            origin = self.userdb.cleaned_origin(entry.path)
            if origin is not None and origin[0] == _sector_sha1(entry):
                disk_id = origin[1]
        return replace(
            entry,
            image_id=record.id if record else None,
            disk_id=disk_id,
            display_name=name,
            parsed=parsed,
        )

    # Removing boot block viruses -----------------------------------------------

    def clean_file(
        self,
        local: LocalFile,
        *,
        download_folder: str | Path | None = None,
        folders: Sequence[str] = DEFAULT_FOLDERS,
    ) -> LocalFile:
        """Remove the boot block virus from a local image; return the cleaned file.

        A plain ADF, ST or MSA file is rewritten in place, with a backup of the
        original beside it. A DMS, ADZ, gzip or STX file is left alone and the
        cleaned sectors are saved next to it as a new file. An image inside an
        archive is saved as a new file under ``download_folder``/<platform>/
        ``folders``. ``clean_names`` says what the files are called. The result
        is indexed straight away and keeps the disk the original belonged to.

        Raises ``images.virus.VirusError`` with a sentence for the user when
        there is no removable virus, and OSError when a file cannot be written.
        """
        from ..images import virus

        data = self.read_bytes(local)
        name = image_file_name(local.path, local.member)
        inspection = self._scanner.inspect_bytes(data, name)
        raw = getattr(inspection, "raw", None)
        platform = getattr(inspection, "platform", None)
        if raw is None or platform is None:
            raise virus.VirusError(
                f"{name} could not be read as a disk image, so its boot block cannot be cleaned."
            )
        report = virus.detect(raw, platform)
        cleaned = virus.clean(raw, platform)
        image_format = str(getattr(inspection, "format", "") or "").lower()
        names = clean_names(local, image_format, platform)
        if local.member and download_folder is None:
            raise virus.VirusError(
                f"{name} is inside an archive, so the cleaned image needs a download "
                "folder to be saved in."
            )
        payload = self._cleaned_payload(image_format, cleaned, inspection)
        if local.member:
            folder = Path(download_folder) / platform_folder(platform)
            for part, fallback in zip(folders or DEFAULT_FOLDERS, DEFAULT_FOLDERS, strict=False):
                folder /= sanitise_name(part, fallback)
            target = _write_new(folder, names.cleaned, payload)
        elif names.backup:
            target = Path(local.path)
            _keep_backup(target, names.backup)
            _replace_file(target, payload)
        else:
            target = _write_new(Path(local.path).parent, names.cleaned, payload)
        if local.disk_id is not None:
            self.userdb.record_cleaned(
                str(target),
                hashlib.sha1(cleaned, usedforsecurity=False).hexdigest(),
                local.disk_id,
                source_path=local.path,
                source_member=local.member,
                virus=report.name,
            )
        entries = self.add_file(target)
        if not entries:
            raise OSError(f"The cleaned image {target} could not be indexed.")
        return entries[0]

    @staticmethod
    def _cleaned_payload(image_format: str, cleaned: bytes, inspection: Any) -> bytes:
        """The file bytes for cleaned sectors: packed again for an MSA, else the sectors."""
        if image_format != "msa":
            return cleaned
        from ..images.vendor.floppy_geometry import geometry_for_layout
        from ..images.vendor.msa import st_to_msa

        shape = inspection.geometry
        return st_to_msa(cleaned, geometry_for_layout(shape.cylinders, shape.heads, shape.sectors))

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


def _numbered(name: str, number: int) -> str:
    """ "x.adf" as "x (2).adf", and a backup "x.adf.bak" as "x.adf.2.bak"."""
    if number == 1:
        return name
    if name.endswith(".bak"):
        return f"{name[:-4]}.{number}.bak"
    return f"{Path(name).stem} ({number}){Path(name).suffix}"


def _free_name(folder: Path, name: str) -> Path:
    """``folder/name``, or the first numbered variant of it that is not taken."""
    number = 1
    while (folder / _numbered(name, number)).exists():
        number += 1
    return folder / _numbered(name, number)


def _keep_backup(target: Path, name: str) -> Path:
    """Copy ``target`` to ``name`` beside it, or a numbered variant, never replacing a file."""
    number = 1
    while True:
        backup = target.parent / _numbered(name, number)
        try:
            with target.open("rb") as source, backup.open("xb") as copy:
                shutil.copyfileobj(source, copy)
                copy.flush()
                os.fsync(copy.fileno())
        except FileExistsError:
            number += 1
            continue
        shutil.copystat(target, backup)
        return backup


def _write_new(folder: Path, name: str, data: bytes) -> Path:
    """Write ``data`` to a new file in ``folder`` without replacing anything there."""
    folder.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(prefix=".piratefinder-", suffix=".part", dir=folder)
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o644)
        while True:
            target = _free_name(folder, name)
            try:
                os.link(temporary, target)
            except FileExistsError:
                continue
            except OSError:
                # Some network file systems have no hard links.
                if target.exists():
                    continue
                os.replace(temporary, target)
            return target
    finally:
        with contextlib.suppress(OSError):
            os.unlink(temporary)


def _replace_file(target: Path, data: bytes) -> None:
    """Replace ``target`` with ``data`` atomically, keeping its permissions."""
    mode = stat.S_IMODE(target.stat().st_mode)
    handle, temporary = tempfile.mkstemp(prefix=".piratefinder-", suffix=".part", dir=target.parent)
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, target)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(temporary)
        raise

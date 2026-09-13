"""List and read the members of zip, 7z and gzip files.

Local collections are often an archive of archives, such as a 7z holding one
zip per disk. One level of nesting is followed and written as
``outer/path/inner.zip::image.st``. Zip and gzip are read with the standard
library; 7z needs the ``7z`` command, and without it a 7z file raises
:class:`ArchiveError` with a sentence saying so.

Members are only ever read into memory, never extracted by name, and every
read is capped so a small archive that expands without limit is refused.
Member names that climb out of the archive (absolute paths, ``..``) are left
out of listings and refused by :func:`read_member`.
"""

from __future__ import annotations

import io
import os
import re
import shutil
import subprocess
import tempfile
import zipfile
import zlib
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import IO

from .inspect import (
    IMAGE_SUFFIXES,
    MAX_IMAGE_SIZE,
    DecodeError,
    gunzip,
    gzip_member_name,
    suffix_of,
)

#: The largest member read into memory as a disk image.
MAX_MEMBER_SIZE = MAX_IMAGE_SIZE
#: The largest archive inside an archive that is opened.
MAX_NESTED_SIZE = 256 * 1024 * 1024
#: Listings longer than this are refused; floppy collections have thousands.
MAX_MEMBERS = 100_000
#: Separates an archive member from a member of an archive nested inside it.
NESTED = "::"
LIST_TIMEOUT = 600

# Archives that are opened, in a library folder or inside another archive.
_NESTED_SUFFIXES = {".zip": "zip", ".7z": "7z", ".gz": "gz"}
ARCHIVE_SUFFIXES = frozenset(_NESTED_SUFFIXES)
_SUFFIX_KINDS = {**_NESTED_SUFFIXES, ".adz": "gz"}
_MAGIC = ((b"PK\x03\x04", "zip"), (b"PK\x05\x06", "zip"), (b"7z\xbc\xaf\x27\x1c", "7z"))
_MAGIC += ((b"\x1f\x8b", "gz"),)
_CHUNK = 1024 * 1024


class ArchiveError(Exception):
    """An archive could not be listed or a member could not be read."""


@dataclass(frozen=True, slots=True)
class Member:
    name: str
    size: int


@dataclass(frozen=True, slots=True)
class _Entry:
    name: str  # normalised, "" when the stored name is unsafe
    stored: str  # the name as the archive holds it
    size: int
    crc: int | None = None
    directory: bool = False
    encrypted: bool = False


def find_7z() -> str | None:
    """The 7-Zip command, when one is installed."""
    for command in ("7z", "7zz", "7za"):
        found = shutil.which(command)
        if found:
            return found
    return None


def archive_kind(path: str | os.PathLike[str]) -> str:
    """The archive type by suffix, or by header when the suffix says nothing.

    Returns "zip", "7z", "gz", or "" for a file that is not an archive.
    """
    path = Path(path)
    suffix = path.suffix.lower()
    if suffix in _SUFFIX_KINDS:
        return _SUFFIX_KINDS[suffix]
    if suffix in IMAGE_SUFFIXES:
        return ""
    try:
        with path.open("rb") as stream:
            head = stream.read(8)
    except OSError:
        return ""
    return next((kind for magic, kind in _MAGIC if head.startswith(magic)), "")


def is_archive(path: str | os.PathLike[str]) -> bool:
    return archive_kind(path) != ""


def base_name(name: str) -> str:
    """The file name of a member, without its folders or outer archive.

    "set/inner.zip::disks/game.st" is "game.st".
    """
    return PurePosixPath(name.rsplit(NESTED, 1)[-1].replace("\\", "/")).name


def image_file_name(path: str | os.PathLike[str], member: str = "") -> str:
    """The file name of a library image: the member's inside an archive, else the file's."""
    return base_name(member) if member else Path(path).name


def is_disk_image_name(name: str) -> bool:
    """Whether a file or member name has a disk image suffix."""
    return suffix_of(base_name(name)) in IMAGE_SUFFIXES


def is_archive_name(name: str) -> bool:
    """Whether a file or member name has the suffix of an archive that is opened."""
    return suffix_of(base_name(name)) in ARCHIVE_SUFFIXES


def members(path: str | os.PathLike[str]) -> list[Member]:
    """Every file in the archive, with archives inside it listed as their members.

    An inner archive that cannot be opened is listed as itself.
    """
    found: list[Member] = []
    with _open_path(path) as outer:
        entries = outer.entries()
        nested = [entry for entry in entries if _nested_kind(entry)]
        opened: dict[str, list[Member]] = {}
        for entry, data in outer.read_many(nested, MAX_NESTED_SIZE, on_error=_ignore):
            try:
                with _open_bytes(_nested_kind(entry), data, entry.name) as inner:
                    opened[entry.name] = [
                        Member(f"{entry.name}{NESTED}{item.name}", item.size)
                        for item in inner.entries()
                    ]
            except ArchiveError:
                continue
        for entry in entries:
            found += opened.get(entry.name, [Member(entry.name, entry.size)])
    return found


def disk_image_members(path: str | os.PathLike[str]) -> list[Member]:
    """The members whose names mark them as disk images, nested ones included."""
    return [member for member in members(path) if is_disk_image_name(member.name)]


def read_member(path: str | os.PathLike[str], name: str) -> bytes:
    """Read one member. ``name`` may use the nested form ``inner.zip::image.st``.

    An empty name reads the first disk image in the archive.
    """
    if not name:
        images = disk_image_members(path)
        if not images:
            raise ArchiveError(f"{Path(path).name} holds no disk image.")
        name = images[0].name
    outer_name, _, inner_name = name.partition(NESTED)
    if NESTED in inner_name:
        raise ArchiveError("Only one archive inside another is followed.")
    for part in (outer_name, inner_name):
        if part and _safe_name(part) != part:
            raise ArchiveError(f"The member name {part} points outside the archive.")
    with _open_path(path) as outer:
        if not inner_name:
            return outer.read(outer_name, MAX_MEMBER_SIZE)
        kind = _NESTED_SUFFIXES.get(suffix_of(outer_name), "")
        if not kind:
            raise ArchiveError(f"{outer_name} is not an archive.")
        data = outer.read(outer_name, MAX_NESTED_SIZE)
    with _open_bytes(kind, data, outer_name) as inner:
        return inner.read(inner_name, MAX_MEMBER_SIZE)


def iter_members(
    path: str | os.PathLike[str],
    names: Iterable[str] | None = None,
    *,
    on_error: Callable[[str, str], None] | None = None,
) -> Iterator[tuple[Member, bytes]]:
    """Yield the disk image members, or the named ones, reading the archive once.

    This is the scanner's path: a solid 7z is decompressed in a single pass
    instead of once per member. A member that cannot be read is passed to
    ``on_error(name, message)`` and skipped.
    """
    report = on_error or _ignore
    wanted = set(names) if names is not None else None

    def chosen(name: str) -> bool:
        return is_disk_image_name(name) if wanted is None else name in wanted

    with _open_path(path) as outer:
        entries = outer.entries()
        direct = []
        for entry in entries:
            if not chosen(entry.name):
                continue
            if entry.size > MAX_MEMBER_SIZE:
                report(entry.name, _too_large(entry.name, MAX_MEMBER_SIZE))
            else:
                direct.append(entry)
        nested = [
            entry
            for entry in entries
            if _nested_kind(entry)
            and (wanted is None or any(item.startswith(entry.name + NESTED) for item in wanted))
        ]
        direct_names = {entry.name for entry in direct}
        for entry, data in outer.read_many(direct + nested, MAX_NESTED_SIZE, on_error=report):
            if entry.name in direct_names:
                if len(data) > MAX_MEMBER_SIZE:
                    report(entry.name, _too_large(entry.name, MAX_MEMBER_SIZE))
                    continue
                yield Member(entry.name, len(data)), data
                continue
            try:
                with _open_bytes(_nested_kind(entry), data, entry.name) as inner:
                    inner_entries = [
                        item
                        for item in inner.entries()
                        if chosen(f"{entry.name}{NESTED}{item.name}")
                    ]
                    for item, item_data in inner.read_many(
                        inner_entries,
                        MAX_MEMBER_SIZE,
                        on_error=lambda name, message, outer=entry.name: report(
                            f"{outer}{NESTED}{name}", message
                        ),
                    ):
                        yield Member(f"{entry.name}{NESTED}{item.name}", len(item_data)), item_data
            except ArchiveError as error:
                report(entry.name, str(error))


def _ignore(_name: str, _message: str) -> None:
    return None


def _nested_kind(entry: _Entry) -> str:
    return _NESTED_SUFFIXES.get(suffix_of(entry.name), "")


def _too_large(name: str, limit: int) -> str:
    return f"{name} is larger than {limit // (1024 * 1024)} MB and was not read."


def _safe_name(stored: str) -> str:
    """The stored name with forward slashes, or "" when it is unsafe to use."""
    name = stored.replace("\\", "/")
    while name.startswith("./"):
        name = name[2:]
    if (
        not name
        or name.startswith("/")
        or re.match(r"^[A-Za-z]:", name)
        or "\0" in name
        or NESTED in name
        or any(part == ".." for part in name.split("/"))
    ):
        return ""
    return name


class _Archive:
    """Common behaviour: listing with safe names, and reading by name."""

    label = "archive"

    def __init__(self) -> None:
        self._all: list[_Entry] | None = None

    def __enter__(self) -> _Archive:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def close(self) -> None:
        return None

    def all_entries(self) -> list[_Entry]:
        if self._all is None:
            self._all = self._list()
            if len(self._all) > MAX_MEMBERS:
                raise ArchiveError(f"The {self.label} lists more than {MAX_MEMBERS:,} files.")
        return self._all

    def entries(self) -> list[_Entry]:
        return [
            entry
            for entry in self.all_entries()
            if entry.name and not entry.directory and not entry.encrypted
        ]

    def find(self, name: str) -> _Entry:
        for entry in self.entries():
            if entry.name == name:
                return entry
        raise ArchiveError(f"{name} is not in the {self.label}.")

    def read(self, name: str, limit: int) -> bytes:
        entry = self.find(name)
        if entry.size > limit:
            raise ArchiveError(_too_large(entry.name, limit))
        return self._read(entry, limit)

    def read_many(
        self,
        entries: list[_Entry],
        limit: int,
        *,
        on_error: Callable[[str, str], None],
    ) -> Iterator[tuple[_Entry, bytes]]:
        for entry in entries:
            if entry.size > limit:
                on_error(entry.name, _too_large(entry.name, limit))
                continue
            try:
                data = self._read(entry, limit)
            except ArchiveError as error:
                on_error(entry.name, str(error))
                continue
            yield entry, data

    def _list(self) -> list[_Entry]:
        raise NotImplementedError

    def _read(self, entry: _Entry, limit: int) -> bytes:
        raise NotImplementedError


class _ZipArchive(_Archive):
    label = "zip file"

    def __init__(self, source: Path | bytes, name: str) -> None:
        super().__init__()
        self._source = source
        self._name = name
        self._spill: Path | None = None
        try:
            self._zip = zipfile.ZipFile(io.BytesIO(source) if isinstance(source, bytes) else source)
        except (zipfile.BadZipFile, OSError, ValueError) as error:
            raise ArchiveError(f"{name} is not a readable zip file: {error}.") from error
        self._infos: dict[str, zipfile.ZipInfo] = {}

    def close(self) -> None:
        self._zip.close()
        if self._spill is not None:
            self._spill.unlink(missing_ok=True)

    def _list(self) -> list[_Entry]:
        found: list[_Entry] = []
        for info in self._zip.infolist():
            name = _safe_name(info.filename)
            if name:
                self._infos[name] = info
            found.append(
                _Entry(
                    name=name.rstrip("/"),
                    stored=info.filename,
                    size=info.file_size,
                    crc=info.CRC,
                    directory=info.is_dir(),
                    encrypted=bool(info.flag_bits & 0x1),
                )
            )
        return found

    def _read(self, entry: _Entry, limit: int) -> bytes:
        info = self._infos[entry.name]
        try:
            with self._zip.open(info) as stream:
                data = stream.read(limit + 1)
        except NotImplementedError:
            # Old Amiga and ST zips use compression methods such as implode
            # that zipfile lacks; 7-Zip reads them.
            return self._read_with_7z(entry, limit)
        except (zipfile.BadZipFile, zlib.error, OSError, EOFError) as error:
            raise ArchiveError(f"{entry.name} is damaged in {self._name}: {error}.") from error
        if len(data) > limit:
            raise ArchiveError(_too_large(entry.name, limit))
        return data

    def _read_with_7z(self, entry: _Entry, limit: int) -> bytes:
        if find_7z() is None:
            raise ArchiveError(
                f"{entry.name} uses a zip compression method that needs 7-Zip. "
                "Install the 7zip package to read it."
            )
        if isinstance(self._source, bytes):
            if self._spill is None:
                self._spill = _spill(self._source, ".zip")
            path = self._spill
        else:
            path = self._source
        with _SevenZipArchive(path, self._name) as other:
            return other.read(entry.name, limit)


class _GzipArchive(_Archive):
    label = "gzip file"

    def __init__(self, source: Path | bytes, name: str) -> None:
        super().__init__()
        self._source = source
        self._name = name

    def _list(self) -> list[_Entry]:
        size = 0
        try:
            if isinstance(self._source, bytes):
                trailer = self._source[-4:]
            else:
                with self._source.open("rb") as stream:
                    stream.seek(-4, os.SEEK_END)
                    trailer = stream.read(4)
            size = int.from_bytes(trailer, "little")
        except OSError as error:
            raise ArchiveError(f"{self._name} cannot be read: {error}.") from error
        inner = _safe_name(gzip_member_name(PurePosixPath(self._name).name))
        return [_Entry(name=inner, stored=inner, size=size)]

    def _read(self, entry: _Entry, limit: int) -> bytes:
        try:
            data = self._source if isinstance(self._source, bytes) else self._source.read_bytes()
        except OSError as error:
            raise ArchiveError(f"{self._name} cannot be read: {error}.") from error
        try:
            return gunzip(data, limit)
        except DecodeError as error:
            raise ArchiveError(f"{self._name}: {error}") from error


class _SevenZipArchive(_Archive):
    label = "7z file"

    def __init__(self, path: Path, name: str, *, spilled: bool = False) -> None:
        super().__init__()
        command = find_7z()
        if command is None:
            if spilled:
                path.unlink(missing_ok=True)
            raise ArchiveError(
                f"{name} is a 7z file, and reading it needs 7-Zip. "
                "Install the 7zip package (or p7zip-full) and try again."
            )
        self._command = command
        self._path = path
        self._name = name
        self._spilled = spilled

    def close(self) -> None:
        if self._spilled:
            self._path.unlink(missing_ok=True)

    def _run(self, *arguments: str) -> subprocess.Popen[bytes]:
        return subprocess.Popen(
            [self._command, *arguments],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=_environment(),
        )

    def _list(self) -> list[_Entry]:
        try:
            completed = subprocess.run(
                [self._command, "l", "-slt", "-ba", "--", str(self._path)],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                env=_environment(),
                timeout=LIST_TIMEOUT,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise ArchiveError(f"7-Zip could not list {self._name}: {error}.") from error
        text = completed.stdout.decode("utf-8", "surrogateescape")
        if completed.returncode > 1:
            detail = _last_error(completed.stderr.decode("utf-8", "replace") or text)
            raise ArchiveError(f"7-Zip could not open {self._name}: {detail}")
        return _parse_listing(text, str(self._path))

    def _read(self, entry: _Entry, limit: int) -> bytes:
        # Ask for the one member first; if the name is a pattern that matched
        # other files, or 7-Zip picks a different entry, the size and CRC
        # disagree and the whole archive is streamed instead.
        data = self._extract(["e", "-so", "-y", "--", str(self._path), entry.stored], limit)
        if data is not None and _matches(entry, data):
            return data
        for _found, streamed in self._stream([entry], limit):
            return streamed
        raise ArchiveError(f"7-Zip could not read {entry.name} from {self._name}.")

    def read_many(
        self,
        entries: list[_Entry],
        limit: int,
        *,
        on_error: Callable[[str, str], None],
    ) -> Iterator[tuple[_Entry, bytes]]:
        readable: list[_Entry] = []
        for entry in entries:
            if entry.size > limit:
                on_error(entry.name, _too_large(entry.name, limit))
            else:
                readable.append(entry)
        if not readable:
            return
        if len(readable) == 1:
            yield from super().read_many(readable, limit, on_error=on_error)
            return
        try:
            yield from self._stream(readable, limit)
        except ArchiveError as error:
            for entry in readable:
                on_error(entry.name, str(error))

    def _extract(self, arguments: list[str], limit: int) -> bytes | None:
        try:
            process = self._run(*arguments)
        except OSError as error:
            raise ArchiveError(f"7-Zip could not be started: {error}.") from error
        assert process.stdout is not None
        try:
            data = process.stdout.read(limit + 1)
        finally:
            _finish(process)
        if len(data) > limit:
            return None
        return data

    def _stream(self, wanted: list[_Entry], limit: int) -> Iterator[tuple[_Entry, bytes]]:
        """Split one ``7z e -so`` of the whole archive by the listed sizes."""
        stream_order = [
            entry for entry in self.all_entries() if not entry.directory and entry.size > 0
        ]
        if any(entry.encrypted for entry in stream_order):
            raise ArchiveError(f"{self._name} holds encrypted files and cannot be streamed.")
        remaining = {id(entry) for entry in wanted}
        try:
            process = self._run("e", "-so", "-y", "--", str(self._path))
        except OSError as error:
            raise ArchiveError(f"7-Zip could not be started: {error}.") from error
        assert process.stdout is not None
        try:
            for entry in stream_order:
                if not remaining:
                    return
                if id(entry) not in remaining or entry.size > limit:
                    if _skip(process.stdout, entry.size) != entry.size:
                        raise ArchiveError(f"7-Zip stopped early while reading {self._name}.")
                    continue
                data = _read_exactly(process.stdout, entry.size)
                if not _matches(entry, data):
                    raise ArchiveError(f"{entry.name} in {self._name} failed its CRC check.")
                remaining.discard(id(entry))
                yield entry, data
        finally:
            _finish(process)


def _parse_listing(text: str, archive_path: str) -> list[_Entry]:
    found: list[_Entry] = []
    for block in re.split(r"\n\s*\n", text.replace("\r\n", "\n")):
        fields: dict[str, str] = {}
        for line in block.splitlines():
            key, separator, value = line.partition(" = ")
            if separator:
                fields[key.strip()] = value
        if "Path" not in fields or "Type" in fields or fields["Path"] == archive_path:
            continue
        stored = fields["Path"]
        crc_text = fields.get("CRC", "").strip()
        attributes = fields.get("Attributes", "")
        found.append(
            _Entry(
                name=_safe_name(stored).rstrip("/"),
                stored=stored,
                size=int(fields.get("Size", "0").strip() or 0),
                crc=int(crc_text, 16) if re.fullmatch(r"[0-9A-Fa-f]{8}", crc_text) else None,
                directory=fields.get("Folder", "").strip() == "+" or attributes.startswith("D"),
                encrypted=fields.get("Encrypted", "").strip() == "+",
            )
        )
    return found


def _matches(entry: _Entry, data: bytes) -> bool:
    if len(data) != entry.size:
        return False
    return entry.crc is None or zlib.crc32(data) & 0xFFFFFFFF == entry.crc


def _read_exactly(stream: IO[bytes], size: int) -> bytes:
    parts: list[bytes] = []
    remaining = size
    while remaining:
        chunk = stream.read(min(_CHUNK, remaining))
        if not chunk:
            break
        parts.append(chunk)
        remaining -= len(chunk)
    return b"".join(parts)


def _skip(stream: IO[bytes], size: int) -> int:
    skipped = 0
    while skipped < size:
        chunk = stream.read(min(_CHUNK, size - skipped))
        if not chunk:
            break
        skipped += len(chunk)
    return skipped


def _finish(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is None:
        process.kill()
    process.wait()
    if process.stdout is not None:
        process.stdout.close()


def _last_error(text: str) -> str:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return lines[-1] if lines else "unknown error"


def _environment() -> dict[str, str]:
    return {
        "HOME": os.environ.get("HOME", "/tmp"),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
    }


def _spill(data: bytes, suffix: str) -> Path:
    handle, name = tempfile.mkstemp(prefix="piratefinder-", suffix=suffix)
    with os.fdopen(handle, "wb") as stream:
        stream.write(data)
    return Path(name)


def _open_path(path: str | os.PathLike[str]) -> _Archive:
    path = Path(path)
    kind = archive_kind(path)
    if not path.is_file():
        raise ArchiveError(f"{path.name} is not a file.")
    if kind == "zip":
        return _ZipArchive(path, path.name)
    if kind == "7z":
        return _SevenZipArchive(path.resolve(), path.name)
    if kind == "gz":
        return _GzipArchive(path, path.name)
    raise ArchiveError(f"{path.name} is not a zip, 7z or gzip file.")


def _open_bytes(kind: str, data: bytes, name: str) -> _Archive:
    if kind == "zip":
        return _ZipArchive(data, name)
    if kind == "7z":
        return _SevenZipArchive(_spill(data, ".7z"), name, spilled=True)
    if kind == "gz":
        return _GzipArchive(data, name)
    raise ArchiveError(f"{name} is not a zip, 7z or gzip file.")

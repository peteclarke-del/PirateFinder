"""Find boot block viruses on Amiga and Atari ST disk images, and remove them.

Amiga. The boot block is the first 1,024 bytes: "DOS" and a flags byte, a
checksum, the root block pointer and boot code. Kickstart runs the code only
when the checksum is right. A boot block that holds the standard install
code of Kickstart 1.3 or 2.0 is clean, whatever is left after that code (many
TOSEC dumps carry leftover bytes there that never run). Anything else is
looked up in the Amiga Bootblock Reader brainfile, when the user has
downloaded it: by CRC, by byte strings and by its seven-byte recognisers. Its
classes decide the status: "v" is a virus, "oldav" a self-copying anti-virus,
"s" a standard block, and the rest named loaders, intros and utilities. A
block the brainfile does not name, or every block when it is not installed,
is matched against the built-in virus signatures in
``data/virus/amiga-signatures.toml`` (the SHA-1 of a stretch of virus code at
a fixed offset) and then the marker rules in ``data/virus/amiga-markers.toml``
(values at fixed offsets, from VirusX 4.0 and AntiCicloVir 2.4).

Atari ST. The boot sector is executable when its 256 big-endian words add up
to 0x1234. An executable sector is matched against the built-in virus body
signatures in ``data/virus/st-signatures.toml``: the SHA-1 of a stretch of
code at a fixed offset, never of the whole sector. Immunisers (a branch
straight to a return) and TOS boot loaders are recognised and left alone.
A sector no signature matches may still carry the marker of a virus listed
in ``data/virus/st-markers.toml``; with code patterns of a boot virus as
well, it is reported as probably that virus, and never cleaned.

Cleaning is offered only for a boot virus identified by its code (a
signature or marker rule on the Amiga, a signature on the ST) on a disk whose
file system is intact: an AmigaDOS root block, or an ST parameter block that
describes the image. The Amiga boot block is replaced with the standard
install block for the disk's DOS type; the ST boot code is cleared and the
sector made non-executable, keeping the parameter block.

A virus TOSEC names on the matched dump (``[v Name]``) is reported as well,
because most flagged Amiga dumps carry file, link or system viruses that
live outside the boot block and cannot be removed here.

Library scans call :func:`detect` on every image, so it works on the first
sectors only, loads its data once and never raises.
"""

from __future__ import annotations

import base64
import contextlib
import functools
import hashlib
import json
import os
import re
import shutil
import struct
import tempfile
import threading
import tomllib
import xml.etree.ElementTree as ElementTree
import zipfile
import zlib
from collections.abc import Callable, Iterable
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath
from typing import Any

from .. import paths
from ..models import Platform, VirusReport, VirusStatus
from .vendor.floppy_geometry import geometry_for_boot_sector

DATA_DIR = Path(__file__).resolve().parent.parent / "data" / "virus"
ST_SIGNATURES = DATA_DIR / "st-signatures.toml"
ST_MARKERS = DATA_DIR / "st-markers.toml"
AMIGA_SIGNATURES = DATA_DIR / "amiga-signatures.toml"
AMIGA_MARKERS = DATA_DIR / "amiga-markers.toml"
VIRUS_KINDS = DATA_DIR / "virus-kinds.toml"
INDICATORS = DATA_DIR / "indicators.toml"

# Raise this whenever a change to the detection code in this module changes
# what ``detect`` reports for some boot block. With the files in DATA_DIR and
# the installed brainfile it makes the ``fingerprint`` the library compares,
# so boot blocks checked before the change are checked again.
DETECTION_VERSION = 1

SOURCE_BUILT_IN = "built-in"
SOURCE_ABR = "Amiga Bootblock Reader"
SOURCE_TOSEC = "TOSEC"

# Amiga boot block layout.
AMIGA_BOOT_SIZE = 1024
AMIGA_BLOCK_SIZE = 512
AMIGA_ROOT_POINTER = 880
_AMIGA_CODE = 12
# The code `install` writes, from the Kickstart 1.3 and 2.0 install commands.
# Each ends with the library names it opens; the rest of the block is zero.
STANDARD_13_CODE = (
    bytes.fromhex("43FA0018 4EAEFFA0 4A80670A 20402068 00167000 4E7570FF 60FA") + b"dos.library\0"
)
STANDARD_20_CODE = (
    bytes.fromhex(
        "43FA003E 70254EAE FDD84A80 670C2240 08E90006 00224EAE FE6243FA 00184EAE"
        " FFA04A80 670A2040 20680016 70004E75 70FF4E75"
    )
    + b"dos.library\0expansion.library\0"
)
# AmigaDOS header block: type T_HEADER at 0, ST_ROOT as the last longword.
_T_HEADER = 2
_ST_ROOT = 1

# Atari ST boot sector layout.
ST_SECTOR = 512
ST_EXECUTABLE_SUM = 0x1234
ST_CODE_START = 0x1E  # after the branch, OEM text, serial number and parameter block
ST_CODE_END = 0x1FE  # the last word balances the checksum
_ST_LOADER_OEM = b"Loader"
_RTS = b"\x4e\x75"
_RELIABLE = "reliable"  # the only st-markers.toml tier that is used

# The Amiga Bootblock Reader, by Jason and Jordan Smith, whose latest release
# the user can download from Preferences.
BRAINFILE_PROJECT = "https://github.com/jasonthesmith79/AmigaBootBlockReader"
BRAINFILE_RELEASE_API = (
    "https://api.github.com/repos/jasonthesmith79/AmigaBootBlockReader/releases/latest"
)
BRAINFILE_FILES = ("brainfile.xml", "searchbrain.xml", "catlist.xml")
_RELEASE_INFO = "release.json"
_MAX_BRAINFILE_MEMBER = 16 * 1024 * 1024
# The brainfile's own categories, used when catlist.xml is missing.
_ABR_VIRUS = "v"
_ABR_OLD_ANTIVIRUS = "oldav"
_ABR_STANDARD = "s"


class VirusError(Exception):
    """A virus cannot be removed; ``message`` says why in a sentence fit to show the user."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class BrainfileError(RuntimeError):
    """The brainfile could not be downloaded or installed; the message is for the user."""


# Built-in data ---------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Signature:
    """The SHA-1 of ``length`` bytes of virus code at ``offset`` in the boot block."""

    name: str
    offset: int
    length: int
    sha1: str
    source: str = ""
    note: str = ""


StSignature = Signature  # the name before Amiga signatures shared the class


@dataclass(frozen=True, slots=True)
class StMarker:
    """A value a virus leaves at a fixed place in the ST boot sector."""

    name: str
    offset: int
    size: int  # 1, 2 or 4 bytes
    value: int
    tier: str = _RELIABLE
    source: str = ""

    @property
    def data(self) -> bytes:
        return self.value.to_bytes(self.size, "big")


@dataclass(frozen=True, slots=True)
class AmigaRule:
    """A virus named by values at fixed offsets in the Amiga boot block."""

    name: str
    checks: tuple[tuple[int, bytes], ...]
    code: bool = False  # the block must also show code patterns
    source: str = ""
    note: str = ""

    def holds(self, block: bytes) -> bool:
        # A plain loop is twice as fast as all() here, and every scanned Amiga
        # boot block that is not standard is tried against every rule.
        for offset, data in self.checks:  # noqa: SIM110
            if block[offset : offset + len(data)] != data:
                return False
        return True


@dataclass(frozen=True, slots=True)
class VirusKind:
    name: str
    platform: str
    kind: str
    note: str = ""
    source: str = ""


@dataclass(frozen=True, slots=True)
class _Indicators:
    st: tuple[tuple[str, bytes, bool], ...]  # label, pattern, word aligned
    amiga: tuple[tuple[str, bytes, bool], ...]
    amiga_vectors: dict[int, str]


def _read_toml(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as handle:
            return tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError):
        return {}


def _signatures(path: Path, size: int) -> tuple[Signature, ...]:
    found = []
    for entry in _read_toml(path).get("signature", []):
        with contextlib.suppress(KeyError, TypeError, ValueError, AttributeError):
            found.append(
                Signature(
                    name=str(entry["name"]),
                    offset=int(entry["offset"]),
                    length=int(entry["length"]),
                    sha1=str(entry["sha1"]).lower(),
                    source=str(entry.get("source", "")),
                    note=str(entry.get("note", "")),
                )
            )
    return tuple(item for item in found if 0 <= item.offset < item.offset + item.length <= size)


@functools.cache
def st_signatures() -> tuple[Signature, ...]:
    """The built-in Atari ST virus signatures."""
    return _signatures(ST_SIGNATURES, ST_SECTOR)


@functools.cache
def amiga_signatures() -> tuple[Signature, ...]:
    """The built-in Amiga boot block virus signatures, in the order they are tried."""
    return _signatures(AMIGA_SIGNATURES, AMIGA_BOOT_SIZE)


@functools.cache
def st_markers() -> tuple[StMarker, ...]:
    """The Atari ST virus markers of the reliable tier; the others are never used."""
    found = []
    for entry in _read_toml(ST_MARKERS).get("marker", []):
        with contextlib.suppress(KeyError, TypeError, ValueError, AttributeError):
            marker = StMarker(
                name=str(entry["name"]),
                offset=int(entry["offset"]),
                size=int(entry["size"]),
                value=int(entry["value"]),
                tier=str(entry.get("tier", "")),
                source=str(entry.get("source", "")),
            )
            if (
                marker.tier == _RELIABLE
                and marker.size in (1, 2, 4)
                and 0 <= marker.offset <= ST_SECTOR - marker.size
                and 0 <= marker.value < 1 << (8 * marker.size)
            ):
                found.append(marker)
    return tuple(found)


@functools.cache
def amiga_rules() -> tuple[AmigaRule, ...]:
    """The built-in Amiga marker rules, in the order they are tried."""
    found = []
    for entry in _read_toml(AMIGA_MARKERS).get("rule", []):
        with contextlib.suppress(KeyError, TypeError, ValueError, AttributeError):
            checks = tuple(
                (int(check["offset"]), bytes.fromhex(str(check["bytes"])))
                for check in entry["checks"]
            )
            if checks and all(
                data and 0 <= offset <= AMIGA_BOOT_SIZE - len(data) for offset, data in checks
            ):
                found.append(
                    AmigaRule(
                        name=str(entry["name"]),
                        checks=checks,
                        code=entry.get("code", False) is True,
                        source=str(entry.get("source", "")),
                        note=str(entry.get("note", "")),
                    )
                )
    return tuple(found)


@functools.cache
def virus_kinds() -> tuple[VirusKind, ...]:
    """Where well-known viruses live, from ``data/virus/virus-kinds.toml``."""
    found = []
    for entry in _read_toml(VIRUS_KINDS).get("virus", []):
        with contextlib.suppress(KeyError, TypeError):
            found.append(
                VirusKind(
                    name=str(entry["name"]),
                    platform=str(entry.get("platform", "")),
                    kind=str(entry["kind"]),
                    note=str(entry.get("note", "")),
                    source=str(entry.get("source", "")),
                )
            )
    return tuple(found)


@functools.cache
def _indicators() -> _Indicators:
    data = _read_toml(INDICATORS)

    def patterns(key: str) -> tuple[tuple[str, bytes, bool], ...]:
        result = []
        for entry in data.get(key, []):
            with contextlib.suppress(KeyError, TypeError, ValueError):
                if "bytes" in entry:
                    result.append((str(entry["label"]), bytes.fromhex(entry["bytes"]), True))
                else:
                    result.append(
                        (str(entry["label"]), str(entry["text"]).encode("latin-1"), False)
                    )
        return tuple(result)

    vectors = {}
    for entry in data.get("amiga_vector", []):
        with contextlib.suppress(KeyError, TypeError, ValueError):
            vectors[int(entry["displacement"])] = str(entry["label"])
    return _Indicators(patterns("st"), patterns("amiga"), vectors)


def _words(name: str) -> tuple[str, ...]:
    words = re.findall(r"[0-9a-z]+", name.casefold())
    if words and words[-1] == "virus":
        words.pop()
    return tuple(words)


def same_virus(first: str, second: str) -> bool:
    """Whether two virus names name the same virus, one a word prefix of the other."""
    a, b = _words(first), _words(second)
    if not a or not b:
        return False
    shorter, longer = (a, b) if len(a) <= len(b) else (b, a)
    return longer[: len(shorter)] == shorter


def virus_kind(name: str, platform: Platform | str | None = None) -> VirusKind | None:
    """What is known about where the named virus lives, or None."""
    words = _words(name)
    best: VirusKind | None = None
    best_length = 0
    for entry in virus_kinds():
        if platform is not None and entry.platform and entry.platform != str(platform):
            continue
        prefix = _words(entry.name)
        if prefix and words[: len(prefix)] == prefix and len(prefix) > best_length:
            best, best_length = entry, len(prefix)
    return best


# Amiga boot block ------------------------------------------------------------


def amiga_checksum(block: bytes) -> int:
    """The boot block checksum Kickstart expects at offset 4.

    It is the complement of the sum, with the carry added back in, of the
    256 big-endian longwords of the block with the checksum field as zero.
    """
    longs = struct.unpack(">256L", block[:AMIGA_BOOT_SIZE].ljust(AMIGA_BOOT_SIZE, b"\0"))
    total = sum(longs) - longs[1]
    while total >> 32:
        total = (total & 0xFFFFFFFF) + (total >> 32)
    return ~total & 0xFFFFFFFF


def amiga_checksum_valid(block: bytes) -> bool:
    return struct.unpack_from(">L", block, 4)[0] == amiga_checksum(block)


def with_amiga_checksum(block: bytes | bytearray) -> bytes:
    """``block`` with its checksum field set to the value Kickstart expects."""
    fixed = bytearray(block[:AMIGA_BOOT_SIZE])
    struct.pack_into(">L", fixed, 4, amiga_checksum(fixed))
    return bytes(fixed)


def standard_boot_block(flags: int = 0, release: str = "") -> bytes:
    """The boot block `install` writes for DOS type ``flags`` (0 to 7).

    ``release`` is "1.3" or "2.0". By default DOS\\0 disks get the Kickstart
    1.3 code and the fast file system and international types the Kickstart
    2.0 code, since only 2.0 can boot those.
    """
    if not release:
        release = "1.3" if flags == 0 else "2.0"
    block = bytearray(AMIGA_BOOT_SIZE)
    block[0:4] = b"DOS" + bytes([flags & 0xFF])
    struct.pack_into(">L", block, 8, AMIGA_ROOT_POINTER)
    code = STANDARD_13_CODE if release == "1.3" else STANDARD_20_CODE
    block[_AMIGA_CODE : _AMIGA_CODE + len(code)] = code
    return with_amiga_checksum(block)


def standard_code(block: bytes) -> str:
    """ "1.3" or "2.0" when the boot code starts with that release's install code."""
    code = block[_AMIGA_CODE:AMIGA_BOOT_SIZE]
    if code.startswith(STANDARD_13_CODE):
        return "1.3"
    if code.startswith(STANDARD_20_CODE):
        return "2.0"
    return ""


def abr_crc(block: bytes) -> int:
    """The CRC the Amiga Bootblock Reader files a boot block under.

    It is the CRC32 of the block with the DOS flags byte cleared and the
    checksum recomputed, so the same code on an OFS and an FFS disk shares
    one entry.
    """
    normal = bytearray(block[:AMIGA_BOOT_SIZE].ljust(AMIGA_BOOT_SIZE, b"\0"))
    normal[3] = 0
    return zlib.crc32(with_amiga_checksum(normal))


def amiga_root_intact(raw: bytes) -> bool:
    """Whether the image has an AmigaDOS root block with a valid checksum."""
    blocks = len(raw) // AMIGA_BLOCK_SIZE
    for key in dict.fromkeys((AMIGA_ROOT_POINTER, 2 * AMIGA_ROOT_POINTER, blocks // 2)):
        if not 2 <= key < blocks:
            continue
        block = raw[key * AMIGA_BLOCK_SIZE : (key + 1) * AMIGA_BLOCK_SIZE]
        longs = struct.unpack(">128L", block)
        if longs[0] == _T_HEADER and longs[127] == _ST_ROOT and sum(longs) & 0xFFFFFFFF == 0:
            return True
    return False


# Atari ST boot sector --------------------------------------------------------


def st_word_sum(sector: bytes) -> int:
    return sum(struct.unpack(">256H", sector[:ST_SECTOR].ljust(ST_SECTOR, b"\0"))) & 0xFFFF


def st_executable(sector: bytes) -> bool:
    return st_word_sum(sector) == ST_EXECUTABLE_SUM


def st_parameters_fit(raw: bytes) -> bool:
    """Whether the boot sector's parameter block describes the image.

    The image may hold whole extra tracks of the same shape, as many menu
    disks formatted to 82 cylinders do.
    """
    declared = geometry_for_boot_sector(raw[:ST_SECTOR])
    if declared is None or declared.size > len(raw):
        return False
    return (len(raw) - declared.size) % (declared.sectors * declared.sides * ST_SECTOR) == 0


def st_immuniser(sector: bytes) -> bool:
    """An executable sector whose opening branch lands on a return: it does nothing."""
    if sector[0] != 0x60:
        return False
    if sector[1] == 0:  # BRA.W: a 16-bit displacement follows
        displacement = struct.unpack_from(">h", sector, 2)[0]
    else:  # BRA.S: the displacement is the signed second byte
        displacement = sector[1] - 0x100 if sector[1] >= 0x80 else sector[1]
    target = 2 + displacement
    return 0 <= target <= ST_SECTOR - 2 and sector[target : target + 2] == _RTS


def _window_match(block: bytes, signatures: Iterable[Signature]) -> Signature | None:
    """The first signature whose code window ``block`` holds, or None."""
    digests: dict[tuple[int, int], str] = {}
    for signature in signatures:
        key = (signature.offset, signature.length)
        if key not in digests:
            body = block[signature.offset : signature.offset + signature.length]
            digests[key] = hashlib.sha1(body, usedforsecurity=False).hexdigest()
        if digests[key] == signature.sha1:
            return signature
    return None


def st_signature(sector: bytes) -> Signature | None:
    """The built-in signature whose virus body the sector holds, or None."""
    return _window_match(sector[:ST_SECTOR], st_signatures())


def st_marker(sector: bytes) -> str | None:
    """The virus whose markers the sector carries, when it carries one virus's only.

    Every entry for the virus must hold. A sector with the markers of two or
    more viruses is an immunised one (UVK immunisation writes several side by
    side), so it names none. Whether the sector is executable, and whether its
    code looks like a virus, is for the caller to decide.
    """
    entries: dict[str, list[bool]] = {}
    for marker in st_markers():
        at = sector[marker.offset : marker.offset + marker.size]
        entries.setdefault(marker.name, []).append(at == marker.data)
    held = [name for name, results in entries.items() if all(results)]
    return held[0] if len(held) == 1 else None


def amiga_identify(block: bytes) -> tuple[str, str] | None:
    """(virus name, note) from the built-in Amiga signatures or marker rules, or None.

    The signatures are tried first, then the rules in file order. A rule
    marked ``code`` holds only when the block also shows code patterns.
    """
    block = block[:AMIGA_BOOT_SIZE]
    signature = _window_match(block, amiga_signatures())
    if signature is not None:
        return signature.name, signature.note
    patterns: list[str] | None = None
    for rule in amiga_rules():
        if not rule.holds(block):
            continue
        if rule.code:
            if patterns is None:
                patterns = amiga_indicators(block)
            if not patterns:
                continue
        return rule.name, rule.note
    return None


# Code patterns ---------------------------------------------------------------


def _find_patterns(
    code: bytes, base: int, patterns: Iterable[tuple[str, bytes, bool]]
) -> list[str]:
    found = []
    for label, pattern, aligned in patterns:
        start = code.find(pattern)
        while start >= 0 and aligned and (base + start) % 2:
            start = code.find(pattern, start + 1)
        if start >= 0:
            found.append(label)
    return found


def st_indicators(sector: bytes) -> list[str]:
    """Descriptions of code patterns in an ST boot sector, for information only."""
    return _find_patterns(sector[ST_CODE_START:ST_CODE_END], ST_CODE_START, _indicators().st)


def amiga_indicators(block: bytes) -> list[str]:
    """Descriptions of code patterns in an Amiga boot block, for information only."""
    code = block[_AMIGA_CODE:AMIGA_BOOT_SIZE]
    tables = _indicators()
    found = _find_patterns(code, _AMIGA_CODE, tables.amiga)
    vectors: list[str] = []
    for offset in range(0, len(code) - 3, 2):
        word = (code[offset] << 8) | code[offset + 1]
        # move.l <ea>,d16(a6) is 0x2D40 to 0x2D7F; immediate data comes first for #imm.
        if word & 0xFFC0 != 0x2D40:
            continue
        field_at = offset + 2 + (4 if word == 0x2D7C else 0)
        if field_at + 2 > len(code):
            continue
        label = tables.amiga_vectors.get((code[field_at] << 8) | code[field_at + 1])
        if label and label not in vectors:
            vectors.append(label)
    if vectors:
        found.append(f"sets {', '.join(vectors)} to stay in memory across a reset")
    return found


def _patterns_sentence(found: list[str]) -> str:
    if not found:
        return ""
    return f" Code patterns found, for information: it {'; it '.join(found)}."


# The Amiga Bootblock Reader brainfile ----------------------------------------


@dataclass(frozen=True, slots=True)
class BootEntry:
    """One boot block the brainfile names."""

    name: str
    klass: str  # the brainfile's class abbreviation, "v" for a virus
    category: str = ""  # the class spelled out, from catlist.xml


class Brainfile:
    """A loaded Amiga Bootblock Reader brainfile."""

    def __init__(
        self,
        crcs: dict[int, BootEntry],
        strings: list[tuple[bytes, BootEntry]],
        recognisers: list[tuple[tuple[tuple[int, int], ...], BootEntry]],
        version: str = "",
    ) -> None:
        self.crcs = crcs
        self.strings = strings
        self.version = version
        self.entries = 0
        self._recognisers: dict[tuple[int, int], list] = {}
        for pairs, entry in recognisers:
            self._recognisers.setdefault(pairs[0], []).append((pairs[1:], entry))

    def __len__(self) -> int:
        return self.entries

    @classmethod
    def parse(
        cls,
        brainfile: bytes,
        searchbrain: bytes | None = None,
        catlist: bytes | None = None,
        version: str = "",
    ) -> Brainfile:
        """Read the three XML files; raises BrainfileError when brainfile.xml is unusable."""
        categories = {}
        for element in _xml_elements(catlist, "Category") if catlist else ():
            abbreviation = (element.findtext("abbrev") or "").strip()
            if abbreviation:
                categories[abbreviation] = (element.findtext("Name") or "").strip()
        crcs: dict[int, BootEntry] = {}
        recognisers: list[tuple[tuple[tuple[int, int], ...], BootEntry]] = []
        strings: list[tuple[bytes, BootEntry]] = []
        count = 0
        for element in _xml_elements(brainfile, "Bootblock"):
            entry = _boot_entry(element, categories)
            if entry is None:
                continue
            count += 1
            for text in (element.findtext("CRC") or "").split(","):
                with contextlib.suppress(ValueError):
                    crcs.setdefault(int(text.strip(), 16), entry)
            pairs = _recogniser(element.findtext("Recog") or "")
            if pairs:
                recognisers.append((pairs, entry))
        if not count:
            raise BrainfileError("The brainfile lists no boot blocks.")
        if searchbrain:
            for element in _xml_elements(searchbrain, "Bootblock"):
                entry = _boot_entry(element, categories)
                text = (element.findtext("Recog") or "").strip()
                if entry is None or not text:
                    continue
                with contextlib.suppress(ValueError):
                    pattern = base64.b64decode(text, validate=True)
                    if pattern:
                        strings.append((pattern, entry))
                        count += 1
        brain = cls(crcs, strings, recognisers, version)
        brain.entries = count
        return brain

    @classmethod
    def load(cls, folder: Path) -> Brainfile:
        """Read an installed brainfile from ``folder``."""
        folder = Path(folder)
        try:
            brainfile = (folder / "brainfile.xml").read_bytes()
        except OSError as error:
            raise BrainfileError(f"The brainfile could not be read: {error}") from error
        optional = {}
        for name in ("searchbrain.xml", "catlist.xml"):
            try:
                optional[name] = (folder / name).read_bytes()
            except OSError:
                optional[name] = None
        return cls.parse(
            brainfile,
            optional["searchbrain.xml"],
            optional["catlist.xml"],
            _release_version(folder),
        )

    def identify(self, block: bytes) -> tuple[BootEntry, str] | None:
        """The entry naming ``block`` and how it was found ("crc", "bytes" or "recogniser")."""
        block = block[:AMIGA_BOOT_SIZE]
        for crc in (abr_crc(block), zlib.crc32(block)):
            entry = self.crcs.get(crc)
            if entry is not None:
                return entry, "crc"
        for pattern, entry in self.strings:
            if pattern in block:
                return entry, "bytes"
        for (offset, value), candidates in self._recognisers.items():
            if offset < len(block) and block[offset] == value:
                for rest, entry in candidates:
                    if all(at < len(block) and block[at] == want for at, want in rest):
                        return entry, "recogniser"
        return None


def _xml_elements(data: bytes, tag: str) -> list[ElementTree.Element]:
    # A document type declaration could expand entities without limit; the
    # brainfile never has one.
    if b"<!DOCTYPE" in data or b"<!ENTITY" in data:
        raise BrainfileError("The brainfile holds a document type declaration and was refused.")
    try:
        root = ElementTree.fromstring(data)
    except ElementTree.ParseError as error:
        raise BrainfileError(f"The brainfile could not be read: {error}.") from error
    return root.findall(tag)


def _boot_entry(element: ElementTree.Element, categories: dict[str, str]) -> BootEntry | None:
    name = (element.findtext("Name") or "").strip()
    klass = (element.findtext("Class") or "").strip()
    if not name:
        return None
    return BootEntry(name, klass, categories.get(klass, ""))


def _recogniser(text: str) -> tuple[tuple[int, int], ...]:
    try:
        values = [int(value) for value in text.split(",") if value.strip()]
    except ValueError:
        return ()
    if len(values) < 2 or len(values) % 2:
        return ()
    pairs = tuple(zip(values[0::2], values[1::2], strict=True))
    if any(not 0 <= offset < AMIGA_BOOT_SIZE or not 0 <= value < 256 for offset, value in pairs):
        return ()
    return pairs


def _release_version(folder: Path) -> str:
    try:
        info = json.loads((folder / _RELEASE_INFO).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    return str(info.get("version", "")) if isinstance(info, dict) else ""


def brainfile_folder() -> Path:
    """Where the downloaded brainfile is kept."""
    return paths.data_dir() / "virus" / "abr"


_brain_lock = threading.Lock()
_brain_cache: tuple[tuple, Brainfile | None] | None = None


def brainfile() -> Brainfile | None:
    """The installed brainfile, loaded once and again after it changes; None when absent."""
    global _brain_cache
    folder = brainfile_folder()
    try:
        info = (folder / "brainfile.xml").stat()
        key: tuple = (str(folder), info.st_mtime_ns, info.st_size)
    except OSError:
        key = (str(folder), None)
    with _brain_lock:
        if _brain_cache is not None and _brain_cache[0] == key:
            return _brain_cache[1]
        brain = None
        if key[1] is not None:
            try:
                brain = Brainfile.load(folder)
            except BrainfileError:
                brain = None
        _brain_cache = (key, brain)
        return brain


def forget_brainfile() -> None:
    """Drop the loaded brainfile so the next detection reads the files again."""
    global _brain_cache
    with _brain_lock:
        _brain_cache = None


def fingerprint() -> str:
    """A hash of everything ``detect`` goes by, other than the boot block itself.

    It covers ``DETECTION_VERSION``, every file in ``DATA_DIR`` and the
    installed brainfile, so it changes when PirateFinder's built-in virus
    data or detection code changes and when the brainfile is installed,
    updated or removed. The library keeps the fingerprint its boot blocks
    were checked with, and checks them again when it differs.
    """
    digest = hashlib.sha256(f"detection {DETECTION_VERSION}\n".encode())
    read = (ST_SIGNATURES, ST_MARKERS, AMIGA_SIGNATURES, AMIGA_MARKERS, VIRUS_KINDS, INDICATORS)
    shipped = [path for path in sorted(DATA_DIR.iterdir()) if path.is_file()]
    # The files detection reads, wherever they are, and anything else shipped beside them.
    files = [(path.name, path) for path in dict.fromkeys([*read, *shipped])]
    folder = brainfile_folder()
    files += [(f"brainfile/{name}", folder / name) for name in BRAINFILE_FILES]
    for name, path in files:
        try:
            data = path.read_bytes()
        except OSError:  # a brainfile that is not installed
            continue
        digest.update(f"{name}\0{len(data)}\0".encode())
        digest.update(data)
    return digest.hexdigest()


def brainfile_status() -> tuple[bool, str, int]:
    """(installed, release version, boot blocks it names) for Preferences."""
    brain = brainfile()
    if brain is None:
        return False, "", 0
    return True, brain.version, len(brain)


def install_brainfile(
    downloader: Any = None,
    progress: Callable[[int, int | None], None] | None = None,
    cancel: object | None = None,
) -> Path:
    """Download the latest Amiga Bootblock Reader release and install its brainfile.

    brainfile.xml, searchbrain.xml and catlist.xml are taken from the release
    zip into ``brainfile_folder()``, replacing any earlier copy only once the
    new one has been read successfully. Returns the folder. Raises
    BrainfileError with a sentence for the user, and lets the downloader's
    cancellation error through.
    """
    from ..online.http import DownloadCancelled, Downloader, DownloadError

    downloader = downloader or Downloader()
    target = brainfile_folder()
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        release = downloader.get_json(
            BRAINFILE_RELEASE_API,
            headers={"Accept": "application/vnd.github+json"},
            cancel=cancel,
        )
    except DownloadCancelled:
        raise
    except DownloadError as error:
        raise BrainfileError(f"The brainfile release could not be found. {error}") from error
    url = _release_zip(release)
    version = str(release.get("tag_name") or release.get("name") or "")
    with tempfile.TemporaryDirectory(prefix=".abr-", dir=target.parent) as scratch:
        staging = Path(scratch) / "abr"
        staging.mkdir()
        download = Path(scratch) / "release.zip"
        try:
            downloader.download(url, download, progress=progress, cancel=cancel)
        except DownloadCancelled:
            raise
        except DownloadError as error:
            raise BrainfileError(f"The brainfile could not be downloaded. {error}") from error
        _extract_brainfile(download, staging)
        (staging / _RELEASE_INFO).write_text(
            json.dumps(
                {
                    "version": version,
                    "published": str(release.get("published_at") or ""),
                    "url": str(release.get("html_url") or ""),
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        Brainfile.load(staging)  # refuse a release whose files cannot be read
        old = Path(scratch) / "old"
        if target.exists():
            os.replace(target, old)
        os.replace(staging, target)
    forget_brainfile()
    return target


def _release_zip(release: Any) -> str:
    if not isinstance(release, dict):
        raise BrainfileError("The brainfile release could not be read.")
    assets = [asset for asset in release.get("assets") or [] if isinstance(asset, dict)]
    for asset in assets:
        name = str(asset.get("name", "")).lower()
        url = str(asset.get("browser_download_url", ""))
        if name.endswith(".zip") and url.startswith("https://"):
            return url
    url = str(release.get("zipball_url") or "")
    if url.startswith("https://"):
        return url
    raise BrainfileError("The latest Amiga Bootblock Reader release has no zip file.")


def _extract_brainfile(download: Path, folder: Path) -> None:
    try:
        with zipfile.ZipFile(download) as archive:
            chosen: dict[str, zipfile.ZipInfo] = {}
            for info in archive.infolist():
                name = PurePosixPath(info.filename.replace("\\", "/")).name.casefold()
                if name not in BRAINFILE_FILES or info.is_dir():
                    continue
                depth = info.filename.count("/")
                if name not in chosen or depth < chosen[name].filename.count("/"):
                    chosen[name] = info
            if "brainfile.xml" not in chosen:
                raise BrainfileError("The Amiga Bootblock Reader release holds no brainfile.")
            for name, info in chosen.items():
                if info.file_size > _MAX_BRAINFILE_MEMBER:
                    raise BrainfileError(f"The release's {name} is larger than expected.")
                with archive.open(info) as source, (folder / name).open("wb") as out:
                    shutil.copyfileobj(source, out)
    except (zipfile.BadZipFile, OSError) as error:
        raise BrainfileError(f"The brainfile release could not be unpacked: {error}") from error


# Detection -------------------------------------------------------------------


def detect(
    raw: bytes, platform: Platform | str | None, *, catalogue_virus: str = ""
) -> VirusReport:
    """What the boot block of the raw sector image ``raw`` holds.

    ``catalogue_virus`` is the virus the catalogue names on the matched dump,
    if any. Never raises.
    """
    platform = _platform(raw, platform)
    try:
        report = _detect_amiga(raw) if platform is Platform.AMIGA else _detect_st(raw)
    except Exception as error:  # a detection fault must never stop a scan
        report = VirusReport(
            VirusStatus.UNKNOWN_BOOT,
            explanation=f"The boot block could not be checked: {error}",
            source=SOURCE_BUILT_IN,
        )
    if catalogue_virus.strip():
        report = with_catalogue_flag(report, catalogue_virus.strip(), platform)
    return report


def flagged(name: str, platform: Platform | str | None) -> VirusReport:
    """A report for a dump the catalogue flags, when its boot block is not at hand."""
    return with_catalogue_flag(None, name, _platform(b"", platform))


def with_catalogue_flag(
    boot: VirusReport | None, name: str, platform: Platform | None
) -> VirusReport:
    """Combine what the boot block shows with the virus the catalogue names."""
    if boot is not None and boot.status is VirusStatus.VIRUS:
        if same_virus(boot.name, name):
            return boot
        extra = f" TOSEC lists this dump as infected with {name} as well."
        return replace(boot, explanation=boot.explanation + extra)
    if (
        boot is not None
        and boot.status in (VirusStatus.ANTIVIRUS, VirusStatus.KNOWN_BOOT)
        and same_virus(boot.name, name)
    ):
        extra = f" TOSEC lists this dump as infected with {name}."
        return replace(boot, explanation=boot.explanation + extra)
    known = virus_kind(name, platform)
    kind = known.kind if known else ""
    unknown_code = boot is not None and boot.status is VirusStatus.UNKNOWN_BOOT
    text = f"TOSEC lists this dump as infected with {name}"
    if kind == "boot":
        text += ", a boot block virus."
        if unknown_code:
            text += " The boot block holds code PirateFinder cannot identify, which may be it."
            if platform is Platform.AMIGA and brainfile() is None:
                text += (
                    " Download the Amiga Bootblock Reader brainfile in Preferences so "
                    "PirateFinder can identify it."
                )
            text += " PirateFinder removes only viruses it identifies."
        elif boot is not None:
            text += " The boot block does not hold it now, so the listing may be out of date."
        else:
            text += " The image is not at hand, so its boot block has not been checked."
    elif kind:
        text += (
            f", a {kind} virus that lives outside the boot block. PirateFinder cannot remove it."
        )
    elif unknown_code:
        text += (
            ". The boot block holds code PirateFinder cannot identify, which may be the virus; "
            "PirateFinder cannot remove it."
        )
    elif boot is not None:
        text += (
            ". The boot block does not show it, so it is probably a file or link virus that "
            "lives outside the boot block, which PirateFinder cannot remove."
        )
    else:
        text += ". PirateFinder cannot remove a virus it has not identified in the boot block."
    if known and known.note:
        text += f" {known.note}"
    return VirusReport(
        VirusStatus.FLAGGED, name=name, kind=kind, explanation=text, source=SOURCE_TOSEC
    )


def _platform(raw: bytes, platform: Platform | str | None) -> Platform:
    if platform:
        with contextlib.suppress(ValueError):
            return Platform(str(platform))
    return Platform.AMIGA if raw[:3] == b"DOS" else Platform.ATARI_ST


def _clean(explanation: str, source: str = SOURCE_BUILT_IN, name: str = "") -> VirusReport:
    return VirusReport(VirusStatus.CLEAN, name=name, explanation=explanation, source=source)


def _detect_amiga(raw: bytes) -> VirusReport:
    if len(raw) < AMIGA_BOOT_SIZE:
        return _clean("The image is too short to hold a boot block.")
    block = raw[:AMIGA_BOOT_SIZE]
    if block[:3] != b"DOS":
        return _clean(
            "The disk has no AmigaDOS boot block, so the Amiga runs no boot code from it."
        )
    if not any(block[_AMIGA_CODE:]):
        return _clean("The boot block holds no code: the disk was formatted but not installed.")
    if not amiga_checksum_valid(block):
        text = "The boot block checksum is wrong, so the Amiga does not run its code."
        brain = brainfile()
        found = brain.identify(block) if brain is not None else None
        if found is not None:
            if found[0].klass == _ABR_VIRUS:
                text += f" It holds an inactive copy of the {_virus_name(found[0].name)} virus."
        else:
            built_in = amiga_identify(block)
            if built_in is not None:
                text += f" It holds an inactive copy of the {built_in[0]} virus."
        return _clean(text)
    release = standard_code(block)
    if release:
        text = f"The boot block is the standard AmigaDOS boot block of Kickstart {release}."
        code = STANDARD_13_CODE if release == "1.3" else STANDARD_20_CODE
        if any(block[_AMIGA_CODE + len(code) :]):
            text += " Leftover bytes after the code never run."
        return _clean(text)
    brain = brainfile()
    found = brain.identify(block) if brain is not None else None
    if found is not None:
        return _amiga_named(raw, found[0])
    built_in = amiga_identify(block)
    if built_in is not None:
        return _amiga_virus(raw, built_in[0], SOURCE_BUILT_IN, built_in[1])
    text = (
        "The boot block holds code PirateFinder cannot identify. Many games and crews boot "
        "their own code, so this is not a sign of a virus by itself."
    )
    if brain is None:
        text += (
            " Download the Amiga Bootblock Reader brainfile in Preferences to identify "
            "thousands of boot blocks."
        )
    text += _patterns_sentence(amiga_indicators(block))
    return VirusReport(VirusStatus.UNKNOWN_BOOT, explanation=text, source=SOURCE_BUILT_IN)


def _virus_name(name: str) -> str:
    return re.sub(r"\s+virus$", "", name.strip(), flags=re.IGNORECASE) or name.strip()


def _amiga_virus(raw: bytes, name: str, source: str, note: str = "") -> VirusReport:
    """The report for a boot block virus identified by the brainfile or built-in data."""
    known = virus_kind(name, Platform.AMIGA)
    removable = amiga_root_intact(raw)
    if removable:
        text = (
            f"The boot block holds the {name} virus, which copies itself onto other disks. "
            "PirateFinder can replace it with a standard AmigaDOS boot block. Any custom "
            "intro or loader the virus replaced is lost, and the disk then boots like a "
            "normally installed disk."
        )
    else:
        text = (
            f"The boot block holds the {name} virus. The disk has no intact AmigaDOS file "
            "system, so it probably boots its own loader, and PirateFinder does not "
            "replace its boot block; look for a clean dump instead."
        )
    for extra in (note, known.note if known else ""):
        if extra:
            text += f" {extra}"
    return VirusReport(
        VirusStatus.VIRUS,
        name=name,
        kind="boot",
        removable=removable,
        explanation=text,
        source=source,
    )


def _amiga_named(raw: bytes, entry: BootEntry) -> VirusReport:
    if entry.klass == _ABR_VIRUS:
        return _amiga_virus(raw, _virus_name(entry.name), SOURCE_ABR)
    if entry.klass == _ABR_OLD_ANTIVIRUS:
        return VirusReport(
            VirusStatus.ANTIVIRUS,
            name=entry.name,
            explanation=(
                f"The boot block is {entry.name}, an old anti-virus that copies itself onto "
                "other disks. It does no harm, but it replaces the boot block of every disk "
                "it reaches."
            ),
            source=SOURCE_ABR,
        )
    if entry.klass == _ABR_STANDARD:
        return _clean(
            f"The Amiga Bootblock Reader brainfile identifies the boot block as {entry.name}, "
            "a standard boot block.",
            SOURCE_ABR,
            entry.name,
        )
    category = f" ({entry.category})" if entry.category else ""
    return VirusReport(
        VirusStatus.KNOWN_BOOT,
        name=entry.name,
        explanation=(
            f"The Amiga Bootblock Reader brainfile identifies the boot block as "
            f"{entry.name}{category}. It is not a virus."
        ),
        source=SOURCE_ABR,
    )


def _detect_st(raw: bytes) -> VirusReport:
    if len(raw) < ST_SECTOR:
        return _clean("The image is too short to hold a boot sector.")
    sector = raw[:ST_SECTOR]
    signature = st_signature(sector)
    inactive = f" It holds an inactive copy of the {signature.name} virus." if signature else ""
    if not st_executable(sector):
        return _clean(
            "The boot sector is not executable, so the Atari ST runs no code from it." + inactive
        )
    if st_immuniser(sector):
        # The opening branch lands on a return, so nothing else in the sector runs.
        return VirusReport(
            VirusStatus.ANTIVIRUS,
            name="Immuniser",
            explanation=(
                "The boot sector is an immuniser: executable code that only returns, so boot "
                "viruses that look for an executable sector leave the disk alone. It is "
                "harmless." + inactive
            ),
            source=SOURCE_BUILT_IN,
        )
    if signature is not None:
        removable = st_parameters_fit(raw)
        if removable:
            text = (
                f"The boot sector holds the {signature.name} virus, which copies itself onto "
                "other disks. PirateFinder can clear the boot code and make the sector "
                "non-executable, keeping the disk parameters."
            )
        else:
            text = (
                f"The boot sector holds the {signature.name} virus. Its disk parameters do not "
                "describe this image, so PirateFinder does not change it; look for a clean "
                "dump instead."
            )
        if signature.note:
            text += f" {signature.note}"
        return VirusReport(
            VirusStatus.VIRUS,
            name=signature.name,
            kind="boot",
            removable=removable,
            explanation=text,
            source=SOURCE_BUILT_IN,
        )
    if sector[2:8] == _ST_LOADER_OEM:
        return VirusReport(
            VirusStatus.KNOWN_BOOT,
            name="TOS boot loader",
            explanation=(
                "The boot sector is a TOS boot loader, which loads a program when the disk "
                "starts. It is not a virus."
            ),
            source=SOURCE_BUILT_IN,
        )
    patterns = st_indicators(sector)
    marked = st_marker(sector)
    if marked is not None and patterns:
        return VirusReport(
            VirusStatus.VIRUS,
            name=f"probably {marked}",
            kind="boot",
            removable=False,
            explanation=(
                f"The boot sector is probably infected with the {marked} virus: it carries the "
                "marker that virus leaves on the disks it infects, and its code "
                f"{_joined(patterns)}. PirateFinder identifies this virus by its marker only, "
                "not by its code, so it leaves the boot sector unchanged. Write a clean dump "
                "of the disk if there is one, or write this one as it is."
            ),
            source=SOURCE_BUILT_IN,
        )
    text = (
        "The boot sector holds executable code PirateFinder cannot identify. Many games and "
        "crews boot their own code, so this is not a sign of a virus by itself."
    )
    text += _patterns_sentence(patterns)
    return VirusReport(VirusStatus.UNKNOWN_BOOT, explanation=text, source=SOURCE_BUILT_IN)


def _joined(phrases: list[str]) -> str:
    if len(phrases) < 2:
        return "".join(phrases)
    return f"{', '.join(phrases[:-1])} and {phrases[-1]}"


# Removal ---------------------------------------------------------------------


def clean(raw: bytes, platform: Platform | str | None) -> bytes:
    """``raw`` with its boot block virus removed; raises VirusError when it cannot be.

    Only an identified boot virus on a disk with an intact file system is
    removed. Everything after the boot block is left as it is.
    """
    platform = _platform(raw, platform)
    report = detect(raw, platform)
    if report.status is not VirusStatus.VIRUS:
        raise VirusError(_refusal(report))
    if not report.removable:
        if platform is Platform.ATARI_ST and st_signature(raw) is None:
            raise VirusError(
                "The boot sector is probably infected with the "
                f"{report.name.removeprefix('probably ')} virus, but PirateFinder identifies it "
                "by its marker only, so it does not change the boot sector; write a clean dump "
                "instead, or write the disk as it is."
            )
        raise VirusError(
            f"The {report.name} virus cannot be removed from this disk because its file system "
            "is not intact; look for a clean dump instead."
        )
    if platform is Platform.AMIGA:
        # The DOS type byte is kept, so the file system is read as before.
        return standard_boot_block(raw[3]) + raw[AMIGA_BOOT_SIZE:]
    sector = bytearray(raw[:ST_SECTOR])
    sector[ST_CODE_START:ST_SECTOR] = bytes(ST_SECTOR - ST_CODE_START)
    if st_word_sum(sector) == ST_EXECUTABLE_SUM:
        sector[ST_SECTOR - 1] = 1
    return bytes(sector) + raw[ST_SECTOR:]


def _refusal(report: VirusReport) -> str:
    if report.status is VirusStatus.CLEAN:
        return "The boot block holds no virus, so there is nothing to remove."
    if report.status is VirusStatus.ANTIVIRUS:
        return f"The boot block holds {report.name or 'an anti-virus'}, not a virus, so it is left alone."
    if report.status is VirusStatus.KNOWN_BOOT:
        return f"The boot block holds {report.name}, not a virus, so it is left alone."
    if report.status is VirusStatus.FLAGGED:
        return (
            f"The {report.name} virus does not live in the boot block, so PirateFinder cannot "
            "remove it."
        )
    return (
        "The boot block holds code PirateFinder cannot identify, so it is not changed; only "
        "known boot viruses are removed."
    )

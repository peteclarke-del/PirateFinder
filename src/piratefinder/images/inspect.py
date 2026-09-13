"""Identify a disk image, decode it to raw sectors and hash it.

TOSEC lists hashes of raw sector images, so an ``.msa``, ``.dms`` or ``.adz``
is decoded before hashing and matches the entry for the same disk whatever its
container. The file's own hashes are kept too, because some sources publish
the hash of the container. Library scans call :func:`inspect_bytes` on
thousands of files, so detection works on headers and sizes and the directory
listing is read lazily and never raises. The boot block of every decoded
image is checked for viruses (``images.virus``), which never raises either.
"""

from __future__ import annotations

import hashlib
import zlib
from dataclasses import dataclass, field
from pathlib import PurePosixPath

from ..models import Geometry, LocalFile, Platform, VirusReport
from .vendor import dms as vendor_dms
from .vendor import msa as vendor_msa
from .vendor import stx as vendor_stx
from .vendor.filesystems import ImageEntry, open_data
from .vendor.floppy_geometry import geometries_for_size, geometry_for_boot_sector
from .virus import detect as detect_virus

#: The largest image that is decoded in memory. Floppy images are at most a
#: few megabytes; anything larger is refused rather than read.
MAX_IMAGE_SIZE = 64 * 1024 * 1024

#: Suffixes of every disk image PirateFinder recognises.
IMAGE_SUFFIXES = frozenset({".st", ".msa", ".stx", ".adf", ".adz", ".dms", ".ipf", ".scp", ".hfe"})

SECTOR_SIZE = 512
#: What ``boot_block`` returns: the Amiga boot block; an ST boot sector is the first half.
BOOT_BLOCK_SIZE = 1024
AMIGA_DD_TRACK = 11 * SECTOR_SIZE
AMIGA_HD_TRACK = 22 * SECTOR_SIZE
AMIGA_CYLINDERS = range(80, 85)
#: The listing is for searching by file name; a damaged directory can claim
#: thousands of entries, so it is cut off here.
MAX_LISTING = 1000

#: Formats that belong to one platform. Flux images (ipf, scp, hfe) and plain
#: gzip files can hold either, so only their contents tell; see ``format_platform``.
FORMAT_PLATFORMS = {
    "st": Platform.ATARI_ST,
    "msa": Platform.ATARI_ST,
    "stx": Platform.ATARI_ST,
    "adf": Platform.AMIGA,
    "adf-ext": Platform.AMIGA,
    "dms": Platform.AMIGA,
    "adz": Platform.AMIGA,
}

_IPF_PLATFORMS = {1: Platform.AMIGA, 2: Platform.ATARI_ST}
_SCP_PLATFORMS = {0x04: Platform.AMIGA, 0x08: Platform.AMIGA}
_SCP_PLATFORMS |= {0x14: Platform.ATARI_ST, 0x15: Platform.ATARI_ST}
# HFE floppy interface modes 2 and 3 are Atari ST DD and HD, 4 and 5 Amiga.
_HFE_INTERFACES = {2: Platform.ATARI_ST, 3: Platform.ATARI_ST}
_HFE_INTERFACES |= {4: Platform.AMIGA, 5: Platform.AMIGA}
_HFE_AMIGA_ENCODING = 1


class DecodeError(ValueError):
    """An image could not be decoded to raw sectors."""


@dataclass(frozen=True, slots=True)
class Hashes:
    crc32: str  # eight lowercase hex digits
    md5: str
    sha1: str
    sha512: str

    @classmethod
    def of(cls, data: bytes) -> Hashes:
        return cls(
            f"{zlib.crc32(data) & 0xFFFFFFFF:08x}",
            hashlib.md5(data, usedforsecurity=False).hexdigest(),
            hashlib.sha1(data, usedforsecurity=False).hexdigest(),
            hashlib.sha512(data).hexdigest(),
        )


@dataclass(frozen=True, slots=True)
class Inspection:
    """What an image file is and what its sectors hash to."""

    # One of st, msa, stx, adf, adf-ext, dms, adz, gz, ipf, scp, hfe, or "".
    format: str
    platform: Platform | None
    raw: bytes | None = field(repr=False)
    geometry: Geometry | None
    volume_label: str
    listing: tuple[str, ...]
    hashes: Hashes
    raw_hashes: Hashes
    size: int = 0
    problem: str = ""  # why raw is missing, in a sentence
    # What the boot block holds, when the image decoded and its platform is known.
    virus: VirusReport | None = None

    @property
    def raw_size(self) -> int:
        return len(self.raw) if self.raw is not None else self.size


def suffix_of(name: str) -> str:
    return PurePosixPath(name.replace("\\", "/")).suffix.lower()


def sector_suffix(platform: Platform | None) -> str:
    """The suffix of a plain sector image: ".adf" for the Amiga, ".st" otherwise."""
    return ".adf" if platform is Platform.AMIGA else ".st"


def format_platform(image_format: str) -> Platform | None:
    """The platform an image format belongs to, or None when the format does not say."""
    return FORMAT_PLATFORMS.get(image_format.lower())


def local_platform(local: LocalFile) -> Platform | None:
    """The platform of a library file as its format says, or its suffix when it has no format."""
    return format_platform(local.format or suffix_of(local.member or local.path).lstrip("."))


def detect_format(data: bytes, name: str = "") -> str:
    """Name the image format from its header, then its suffix, then its size.

    Returns "" for anything that is not a recognised disk image.
    """
    head = data[:16]
    suffix = suffix_of(name)
    if head.startswith(b"DMS!"):
        return "dms"
    if head.startswith(b"\x1f\x8b"):
        return "adz" if suffix == ".adz" else "gz"
    if head.startswith(b"RSY\x00"):
        return "stx"
    if head.startswith(b"CAPS"):
        return "ipf"
    if head.startswith((b"HXCPICFE", b"HXCHFEV3")):
        return "hfe"
    if head.startswith((b"UAE-1ADF", b"UAE--ADF")):
        return "adf-ext"
    if head.startswith(b"SCP") and (suffix == ".scp" or _plausible_scp(data)):
        return "scp"
    if _plausible_msa(data):
        return "msa"
    if len(data) % SECTOR_SIZE or not data:
        return ""
    if suffix == ".adf" and amiga_geometry(len(data)) is not None:
        return "adf"
    if suffix == ".st" and st_geometry(data)[0] is not None:
        return "st"
    if amiga_geometry(len(data)) is not None:
        # 901,120 bytes is also an 80 x 2 x 11 ST image. An AmigaDOS disk
        # starts with "DOS"; an ST disk with a usable parameter block.
        if data[:3] == b"DOS" or geometry_for_boot_sector(data[:SECTOR_SIZE]) is None:
            return "adf"
        return "st"
    if st_geometry(data)[0] is not None:
        return "st"
    return ""


def _plausible_msa(data: bytes) -> bool:
    if len(data) < 12 or data[:2] != vendor_msa.MAGIC:
        return False
    spt = int.from_bytes(data[2:4], "big")
    sides = int.from_bytes(data[4:6], "big")
    start = int.from_bytes(data[6:8], "big")
    end = int.from_bytes(data[8:10], "big")
    return 1 <= spt <= 36 and sides <= 1 and start <= end <= 255


def _plausible_scp(data: bytes) -> bool:
    return len(data) >= 0x2B0 and 1 <= data[5] <= 32 and data[6] <= data[7] and data[10] <= 2


def amiga_geometry(size: int) -> Geometry | None:
    """The layout of an ADF of this size, double or high density, 80 to 84 cylinders."""
    if size % AMIGA_DD_TRACK == 0 and size // (2 * AMIGA_DD_TRACK) in AMIGA_CYLINDERS:
        return Geometry(size // (2 * AMIGA_DD_TRACK), 2, 11)
    if size % AMIGA_HD_TRACK == 0 and size // (2 * AMIGA_HD_TRACK) in AMIGA_CYLINDERS:
        return Geometry(size // (2 * AMIGA_HD_TRACK), 2, 22)
    return None


def st_geometry(raw: bytes, hint: Geometry | None = None) -> tuple[Geometry | None, str]:
    """Settle an ST sector image's layout, and say how it was settled.

    The second value is "hint" (a container header such as MSA's), "boot"
    (the BIOS parameter block agrees with the size), "size" (the size names
    exactly one known layout) or "guess" (the size fits several layouts and
    the likeliest was taken: 80 cylinders first, then double-sided, then
    up to 84 cylinders with the sides chosen by size as Hatari does). A
    layout is only returned when it accounts for every byte.
    """
    size = len(raw)
    if not size or size % SECTOR_SIZE:
        return None, ""
    if hint is not None and _size(hint) == size:
        return hint, "hint"
    declared = geometry_for_boot_sector(raw[:SECTOR_SIZE])
    if declared is not None and declared.size == size:
        return Geometry(declared.tracks, declared.sides, declared.sectors), "boot"
    candidates = geometries_for_size(size)
    if candidates:
        best = candidates[0]
        source = "size" if len(candidates) == 1 else "guess"
        return Geometry(best.tracks, best.sides, best.sectors), source
    sides = 1 if size < 500 * 1024 else 2
    for sectors in (9, 10, 11):
        for cylinders in range(80, 85):
            if cylinders * sides * sectors * SECTOR_SIZE == size:
                return Geometry(cylinders, sides, sectors), "guess"
    return None, ""


def _size(geometry: Geometry) -> int:
    return geometry.cylinders * geometry.heads * geometry.sectors * geometry.sector_size


def dms_to_adf(data: bytes) -> bytes:
    """Rebuild the ADF a DMS archive was made from.

    Only whole tracks are placed. DMS also carries a banner and a
    FILE_ID.DIZ as short pseudo-tracks, sometimes numbered 80, which are not
    part of the disk. Missing tracks are left blank and the image is padded
    to at least 80 cylinders, as xDMS does.
    """
    try:
        contents = vendor_dms.parse_dms(data)
    except vendor_dms.DMSError as error:
        raise DecodeError(str(error)) from error
    tracks = [track for track in contents.files if track.number < 200]
    full = [
        track
        for track in tracks
        if track.unpacked_length in (2 * AMIGA_DD_TRACK, 2 * AMIGA_HD_TRACK)
    ]
    if not full:
        raise DecodeError("The DMS archive holds no disk tracks.")
    broken = [track.number for track in full if not track.complete]
    if broken:
        listed = ", ".join(str(number) for number in broken[:8])
        raise DecodeError(f"{len(broken)} DMS track(s) could not be unpacked: {listed}.")
    cylinder_size = full[0].unpacked_length
    if any(track.unpacked_length != cylinder_size for track in full):
        raise DecodeError("The DMS archive mixes double and high density tracks.")
    cylinders = max(80, max(track.number for track in full) + 1)
    image = bytearray(cylinders * cylinder_size)
    for track in full:
        start = track.number * cylinder_size
        image[start : start + cylinder_size] = track.data
    return bytes(image)


def msa_to_st(data: bytes) -> tuple[bytes, Geometry]:
    try:
        parsed = vendor_msa.parse_msa(data)
    except vendor_msa.MSAError as error:
        raise DecodeError(str(error)) from error
    geometry = Geometry(parsed.end_track + 1, parsed.sides, parsed.sectors_per_track)
    return parsed.sectors(), geometry


def stx_to_st(data: bytes) -> tuple[bytes, Geometry, dict]:
    """Decode a Pasti image, returning the sectors, layout and protection report."""
    try:
        decoded = vendor_stx.decode_stx(data)
    except vendor_stx.STXError as error:
        raise DecodeError(str(error)) from error
    shape = decoded.geometry
    return decoded.image, Geometry(shape.tracks, shape.sides, shape.sectors), decoded.protection


def gunzip(data: bytes, limit: int = MAX_IMAGE_SIZE) -> bytes:
    """Decompress gzip data, refusing output larger than ``limit``."""
    decompressor = zlib.decompressobj(16 + zlib.MAX_WBITS)
    try:
        out = decompressor.decompress(data, limit + 1)
    except zlib.error as error:
        raise DecodeError(f"The gzip data is damaged: {error}.") from error
    if len(out) > limit:
        raise DecodeError(f"The gzip file expands to more than {limit // (1024 * 1024)} MB.")
    if not decompressor.eof:
        raise DecodeError("The gzip file is cut short.")
    return out


def gzip_member_name(name: str) -> str:
    """The name of the file inside ``name``: x.adz is x.adf, x.st.gz is x.st."""
    path = PurePosixPath(name.replace("\\", "/"))
    if path.suffix.lower() == ".adz":
        return path.stem + ".adf"
    if path.suffix.lower() == ".gz":
        return path.stem
    return path.name


def flux_details(data: bytes, kind: str) -> tuple[Platform | None, Geometry | None]:
    """Platform and track layout from an IPF, SCP or HFE header, when it says."""
    if kind == "ipf" and len(data) >= 88 and data[12:16] == b"INFO":

        def word(offset: int) -> int:
            return int.from_bytes(data[offset : offset + 4], "big")

        platform = next(
            (
                _IPF_PLATFORMS[p]
                for p in (word(72 + 4 * i) for i in range(4))
                if p in _IPF_PLATFORMS
            ),
            None,
        )
        low, high, low_head, high_head = word(48), word(52), word(56), word(60)
        geometry = None
        if low <= high < 256 and low_head <= high_head <= 1:
            geometry = Geometry(high + 1, high_head + 1, 0)
        return platform, geometry
    if kind == "scp" and len(data) >= 16:
        end_track, heads = data[7], data[10]
        sides = 2 if heads == 0 else 1
        cylinders = end_track // 2 + 1 if sides == 2 else end_track + 1
        return _SCP_PLATFORMS.get(data[4]), Geometry(cylinders, sides, 0)
    if kind == "hfe" and len(data) >= 20:
        platform = _HFE_INTERFACES.get(data[16])
        if platform is None and data[11] == _HFE_AMIGA_ENCODING:
            platform = Platform.AMIGA
        geometry = Geometry(data[9], data[10], 0) if data[9] and data[10] in (1, 2) else None
        return platform, geometry
    return None, None


def inspect_bytes(data: bytes, name: str) -> Inspection:
    """Identify ``data``, decode it to raw sectors when possible and hash both."""
    decoded = _decode(data, name)
    raw, platform = decoded.raw, decoded.platform
    hashes = Hashes.of(data)
    label, listing = directory(raw, platform) if raw is not None else ("", ())
    return Inspection(
        format=decoded.format,
        platform=platform,
        raw=raw,
        geometry=decoded.geometry,
        volume_label=label,
        listing=listing,
        hashes=hashes,
        raw_hashes=hashes if raw is None or raw is data else Hashes.of(raw),
        size=len(data),
        problem=decoded.problem,
        virus=detect_virus(raw, platform) if raw is not None and platform is not None else None,
    )


def boot_block(data: bytes, name: str) -> tuple[bytes, Platform] | None:
    """The first kilobyte of an image's sectors and its platform, for checking the
    boot block again: decoded as ``inspect_bytes`` decodes it, without hashing
    or listing anything. None when the image does not decode to sectors of a
    known platform."""
    decoded = _decode(data, name)
    if decoded.raw is None or decoded.platform is None:
        return None
    return decoded.raw[:BOOT_BLOCK_SIZE], decoded.platform


@dataclass(frozen=True, slots=True)
class _Decoded:
    format: str
    platform: Platform | None
    raw: bytes | None
    geometry: Geometry | None = None
    problem: str = ""


def _decode(data: bytes, name: str, *, nested: bool = False) -> _Decoded:
    """The format, platform, raw sectors and layout of an image, or why it has no sectors."""
    kind = detect_format(data, name)
    raw: bytes | None = None
    geometry: Geometry | None = None
    platform = format_platform(kind)
    problem = ""
    try:
        if kind == "st":
            raw = data
            geometry = st_geometry(data)[0]
        elif kind == "adf":
            raw = data
            geometry = amiga_geometry(len(data))
        elif kind == "msa":
            raw, geometry = msa_to_st(data)
        elif kind == "dms":
            raw = dms_to_adf(data)
            geometry = amiga_geometry(len(raw))
        elif kind == "stx":
            sectors, geometry, protection = stx_to_st(data)
            if protection.get("protected"):
                problem = "The Pasti image records copy protection a sector image cannot hold."
            else:
                raw = sectors
        elif kind in ("gz", "adz") and not nested:
            return _decode_gzip(data, name)
        elif kind in ("ipf", "scp", "hfe"):
            platform, geometry = flux_details(data, kind)
        elif kind == "adf-ext":
            problem = "Extended ADF images hold raw tracks and are not decoded."
        else:
            problem = "The file is not a recognised disk image."
    except DecodeError as error:
        raw = None
        problem = str(error)
    return _Decoded(kind, platform, raw, geometry, problem)


def _decode_gzip(data: bytes, name: str) -> _Decoded:
    named_adz = suffix_of(name) == ".adz"
    try:
        inner = gunzip(data)
    except DecodeError as error:
        kind = "adz" if named_adz else "gz"
        return _Decoded(kind, format_platform(kind), None, None, str(error))
    found = _decode(inner, gzip_member_name(name), nested=True)
    kind = "adz" if named_adz or found.format == "adf" else "gz"
    return _Decoded(kind, found.platform, found.raw, found.geometry, found.problem)


def directory(raw: bytes, platform: Platform | None) -> tuple[str, tuple[str, ...]]:
    """Volume label and file paths of a raw sector image, or empty on any error."""
    suffix = sector_suffix(platform)
    try:
        contents = open_data(raw, suffix)
    except Exception:
        return (_fat_volume_label(raw) if suffix == ".st" else ""), ()
    paths: list[str] = []
    _flatten(contents.entries, "", paths)
    # The FAT reader takes a label from bytes 43-53, which on an ST boot
    # sector hold boot code. TOS keeps the label in the root directory.
    label = _fat_volume_label(raw) if suffix == ".st" else contents.volume_label
    return label, tuple(paths)


def _flatten(entries: tuple[ImageEntry, ...], prefix: str, paths: list[str]) -> None:
    for entry in entries:
        if len(paths) >= MAX_LISTING:
            return
        path = f"{prefix}{entry.name}"
        if entry.is_directory:
            paths.append(path + "/")
            _flatten(entry.children, path + "/", paths)
        else:
            paths.append(path)


def _fat_volume_label(raw: bytes) -> str:
    """The volume label entry of a FAT12 root directory, or ""."""
    try:
        reserved = int.from_bytes(raw[14:16], "little")
        fats = raw[16]
        root_entries = int.from_bytes(raw[17:19], "little")
        per_fat = int.from_bytes(raw[22:24], "little")
        if not (1 <= reserved <= 8 and 1 <= fats <= 2 and 0 < root_entries <= 512):
            return ""
        start = (reserved + fats * per_fat) * SECTOR_SIZE
        root = raw[start : start + root_entries * 32]
        for offset in range(0, len(root) - 31, 32):
            entry = root[offset : offset + 32]
            if entry[0] == 0:
                break
            if entry[0] != 0xE5 and entry[11] & 0x08 and entry[11] != 0x0F:
                text = entry[:11].decode("latin-1").strip(" \0")
                return "".join(ch for ch in text if ch.isprintable())
    except IndexError:
        return ""
    return ""

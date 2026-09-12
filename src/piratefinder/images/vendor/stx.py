# Vendored from Atari File Forge, app/stx.py.
# Copyright (c) 2026 Pete Clarke. MIT licence, full text in vendor/__init__.py.

"""Pasti STX captures: reading the sectors out of a protection-aware image.

Pasti recorded what the WD1772 controller saw on a real disk, sector by
sector: the ID fields as written, the status the controller returned, where
on the track each sector sat, how long it took to read, and the bytes that
changed from one read to the next. That is what a copy-protected game needs
and what a plain sector image cannot hold, so an STX is opened here for what
can be read out of it, never as something to write back. There is no STX
writer in this module and there will not be one: the format is a record of a
physical read, and the workbench has no physical read to record.

The file layout, little-endian throughout:

* a 16-byte file header: ``RSY\\0``, version word (3), tool word, a reserved
  word, the track count byte, a revision byte and a reserved long;
* for each track, a 16-byte record header: record size, fuzzy-mask size,
  sector count, flags, track length, track number (bit 7 is the side) and
  record type;
* when flags bit 0 is set, a 16-byte descriptor per sector (data offset,
  bit position, read time, the ID field C H R N, the ID CRC, the FDC status
  byte and a flags byte), then the fuzzy mask, then the data area. A
  descriptor's data offset counts from the start of that data area. Flag
  bit 6 says a raw track image also sits at the front of the data area, and
  bit 7 that it is preceded by a two-byte sync offset;
* when flags bit 0 is clear, the sectors are plain 512-byte blocks in order,
  straight after the record header.

A sector whose FDC status reports record-not-found or a CRC error is
unreadable and its place in the sector image is left blank and reported.
Everything else that distinguishes the capture from an ordinary disk is
collected into a protection report so the user can see what the sector
image has lost.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field

from .floppy_geometry import (
    SECTOR_SIZE,
    FloppyGeometry,
    geometry_for_boot_sector,
    geometry_for_layout,
)


MAGIC = b"RSY\x00"
FILE_HEADER_SIZE = 16
TRACK_HEADER_SIZE = 16
SECTOR_DESCRIPTOR_SIZE = 16

TRACK_FLAG_SECTOR_DESCRIPTORS = 0x01
TRACK_FLAG_TRACK_IMAGE = 0x40
TRACK_FLAG_SYNC_OFFSET = 0x80

#: The descriptor's own flags byte: bit 7 says the fuzzy mask applies to
#: this sector, bit 0 that intra-sector timing was recorded for it.
SECTOR_FLAG_TIMING = 0x01
SECTOR_FLAG_FUZZY = 0x80

#: WD1772 status bits after a read-sector command.
FDC_LOST_DATA = 0x04
FDC_CRC_ERROR = 0x08
FDC_RECORD_NOT_FOUND = 0x10
FDC_DELETED_DATA = 0x20

#: A double-density track is about 6,250 bytes long; anything much past that
#: was written by something other than a standard formatter.
LONG_TRACK_BYTES = 6400

#: STX is read, never written. Callers that offer conversions check this
#: rather than discovering the absence of a writer at run time.
WRITABLE = False


class STXError(ValueError):
    """An STX capture could not be read."""


@dataclass(frozen=True)
class STXSector:
    track: int
    side: int
    cylinder: int
    head: int
    number: int
    size_code: int
    bit_position: int
    read_time: int
    id_crc: int
    fdc_status: int
    flags: int
    data: bytes | None

    @property
    def size(self) -> int:
        return 128 << self.size_code if self.size_code <= 7 else 0

    @property
    def record_not_found(self) -> bool:
        return bool(self.fdc_status & FDC_RECORD_NOT_FOUND)

    @property
    def crc_error(self) -> bool:
        return bool(self.fdc_status & FDC_CRC_ERROR)

    @property
    def readable(self) -> bool:
        return (
            self.data is not None
            and not self.record_not_found
            and not self.crc_error
            and len(self.data) == self.size
        )

    @property
    def fuzzy(self) -> bool:
        return bool(self.flags & SECTOR_FLAG_FUZZY)

    @property
    def timed(self) -> bool:
        return bool(self.flags & SECTOR_FLAG_TIMING) or self.read_time != 0


@dataclass(frozen=True)
class STXTrack:
    track: int
    side: int
    flags: int
    track_length: int
    fuzzy_size: int
    sectors: tuple[STXSector, ...]

    @property
    def has_descriptors(self) -> bool:
        return bool(self.flags & TRACK_FLAG_SECTOR_DESCRIPTORS)

    @property
    def has_track_image(self) -> bool:
        return bool(self.flags & TRACK_FLAG_TRACK_IMAGE)


@dataclass(frozen=True)
class STXImage:
    version: int
    tool: int
    revision: int
    tracks: tuple[STXTrack, ...]

    @property
    def sides(self) -> int:
        return 2 if any(item.side for item in self.tracks) else 1

    @property
    def track_count(self) -> int:
        """Every track the capture holds, including any beyond the data area."""
        return max((item.track for item in self.tracks), default=-1) + 1

    @property
    def data_track_count(self) -> int:
        """The tracks that carry sectors, which is what the layout is.

        A protected disk is commonly captured past the eighty tracks a drive
        formats, because the protection lives out there. Those extra tracks
        hold no sectors, so counting them would inflate the sector image and
        misplace every sector after the first.
        """
        return max((item.track for item in self.tracks if item.sectors), default=-1) + 1


@dataclass
class STXDecode:
    """A sector image read out of a capture, and what it could not hold."""

    image: bytes
    geometry: FloppyGeometry
    unreadable: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    protection: dict = field(default_factory=dict)

    @property
    def sectors_recovered(self) -> int:
        return self.protection.get("sectorsRecovered", 0)


def is_stx(data: bytes) -> bool:
    return len(data) >= FILE_HEADER_SIZE and data[:4] == MAGIC


def _word(data: bytes, offset: int) -> int:
    return int.from_bytes(data[offset : offset + 2], "little")


def _long(data: bytes, offset: int) -> int:
    return int.from_bytes(data[offset : offset + 4], "little")


def _parse_track(record: bytes, index: int) -> STXTrack:
    if len(record) < TRACK_HEADER_SIZE:
        raise STXError(f"Track record {index} is cut off.")
    fuzzy_size = _long(record, 4)
    sector_count = _word(record, 8)
    flags = _word(record, 10)
    track_length = _word(record, 12)
    number = record[14]
    track, side = number & 0x7F, number >> 7
    sectors: list[STXSector] = []
    if flags & TRACK_FLAG_SECTOR_DESCRIPTORS:
        descriptors_end = TRACK_HEADER_SIZE + sector_count * SECTOR_DESCRIPTOR_SIZE
        if descriptors_end + fuzzy_size > len(record):
            raise STXError(
                f"Track {track} side {side} promises {sector_count} sector descriptors "
                "the record does not hold."
            )
        data_area = record[descriptors_end + fuzzy_size :]
        for position in range(TRACK_HEADER_SIZE, descriptors_end, SECTOR_DESCRIPTOR_SIZE):
            descriptor = record[position : position + SECTOR_DESCRIPTOR_SIZE]
            offset = _long(descriptor, 0)
            fdc_status = descriptor[14]
            size_code = descriptor[11]
            size = 128 << size_code if size_code <= 7 else 0
            data: bytes | None = None
            if not fdc_status & FDC_RECORD_NOT_FOUND and offset + size <= len(data_area):
                data = bytes(data_area[offset : offset + size])
            sectors.append(
                STXSector(
                    track=track,
                    side=side,
                    cylinder=descriptor[8],
                    head=descriptor[9],
                    number=descriptor[10],
                    size_code=size_code,
                    bit_position=_word(descriptor, 4),
                    read_time=_word(descriptor, 6),
                    id_crc=_word(descriptor, 12),
                    fdc_status=fdc_status,
                    flags=descriptor[15],
                    data=data,
                )
            )
    else:
        needed = TRACK_HEADER_SIZE + sector_count * SECTOR_SIZE
        if needed > len(record):
            raise STXError(
                f"Track {track} side {side} promises {sector_count} plain sectors "
                "the record does not hold."
            )
        for number in range(sector_count):
            start = TRACK_HEADER_SIZE + number * SECTOR_SIZE
            sectors.append(
                STXSector(
                    track=track,
                    side=side,
                    cylinder=track,
                    head=side,
                    number=number + 1,
                    size_code=2,
                    bit_position=0,
                    read_time=0,
                    id_crc=0,
                    fdc_status=0,
                    flags=0,
                    data=bytes(record[start : start + SECTOR_SIZE]),
                )
            )
    return STXTrack(track, side, flags, track_length, fuzzy_size, tuple(sectors))


def parse_stx(data: bytes) -> STXImage:
    """Read every track record, refusing a file that does not add up."""
    if not is_stx(data):
        raise STXError("The file does not start with the Pasti identifier RSY.")
    version = _word(data, 4)
    tool = _word(data, 6)
    track_count = data[10]
    revision = data[11]
    if version != 3:
        raise STXError(f"Pasti version {version} is not one this reader understands.")
    tracks: list[STXTrack] = []
    position = FILE_HEADER_SIZE
    for index in range(track_count):
        if position + TRACK_HEADER_SIZE > len(data):
            raise STXError(
                f"The file ends after {index} of its {track_count} track records."
            )
        record_size = _long(data, position)
        if record_size < TRACK_HEADER_SIZE or position + record_size > len(data):
            raise STXError(f"Track record {index} claims {record_size:,} bytes the file does not hold.")
        tracks.append(_parse_track(data[position : position + record_size], index))
        position += record_size
    return STXImage(version, tool, revision, tuple(tracks))


def _sectors_per_track(image: STXImage) -> int:
    """Settle the layout from the capture, not from what the disk claims.

    A capture records what the head actually read, so the commonest number of
    sectors across the tracks that carry any is the layout. The parameter
    block in the boot sector is only consulted to break a tie between two
    equally common counts, and never to overrule the disk.

    That order matters here. A protected disk often carries a parameter block
    that does not describe it: the loader does not go through GEMDOS, so the
    figures in the boot sector were never required to be true. Trusting them
    produced a sector image of a size no real drive ever wrote, which no
    filing system could then read.
    """
    counts = Counter(len(track.sectors) for track in image.tracks if track.sectors)
    if not counts:
        raise STXError("The capture holds no sectors at all.")
    ranked = counts.most_common()
    if len(ranked) > 1 and ranked[0][1] == ranked[1][1]:
        for track in image.tracks:
            if track.track or track.side:
                continue
            for sector in track.sectors:
                if sector.number == 1 and sector.readable:
                    declared = geometry_for_boot_sector(sector.data or b"")
                    if declared is not None and declared.sectors in counts:
                        return declared.sectors
    return ranked[0][0]


def _declared_sectors_per_track(image: STXImage) -> int | None:
    """What the boot sector claims, so a disagreement can be reported."""
    for track in image.tracks:
        if track.track or track.side:
            continue
        for sector in track.sectors:
            if sector.number == 1 and sector.readable:
                declared = geometry_for_boot_sector(sector.data or b"")
                if declared is not None:
                    return declared.sectors
    return None


def _track_evidence(track: STXTrack, sectors_per_track: int) -> list[str]:
    evidence: list[str] = []
    if track.fuzzy_size:
        evidence.append(f"{track.fuzzy_size:,} fuzzy bytes read differently on each pass")
    if track.has_track_image:
        evidence.append("a raw track image was kept alongside the sectors")
    if track.track_length >= LONG_TRACK_BYTES:
        evidence.append(f"long track of {track.track_length:,} bytes")
    if track.sectors and len(track.sectors) != sectors_per_track:
        evidence.append(f"{len(track.sectors)} sectors where {sectors_per_track} are standard")
    numbers = Counter(sector.number for sector in track.sectors)
    duplicates = sorted(number for number, count in numbers.items() if count > 1)
    if duplicates:
        evidence.append("duplicate sector IDs " + ", ".join(map(str, duplicates)))
    if track.sectors:
        missing = sorted(set(range(1, sectors_per_track + 1)) - set(numbers))
        if missing:
            evidence.append("missing sector IDs " + ", ".join(map(str, missing)))
    for sector in track.sectors:
        if sector.size_code != 2:
            evidence.append(f"sector {sector.number} is {sector.size} bytes")
        if sector.cylinder != track.track or sector.head != track.side:
            evidence.append(
                f"sector {sector.number} claims to be on track {sector.cylinder} side {sector.head}"
            )
        if sector.timed:
            evidence.append(f"sector {sector.number} carries timing data")
        if sector.fdc_status & FDC_DELETED_DATA:
            evidence.append(f"sector {sector.number} uses a deleted-data mark")
    return evidence


def decode_stx(data: bytes) -> STXDecode:
    """Read a capture into a plain sector image, reporting what was lost."""
    image = parse_stx(data)
    sides = image.sides
    sectors_per_track = _sectors_per_track(image)
    tracks = image.data_track_count
    if tracks == 0:
        raise STXError("The capture holds no tracks.")
    geometry = geometry_for_layout(tracks, sides, sectors_per_track)
    sectors = bytearray(geometry.size)
    placed: set[int] = set()
    unreadable: list[str] = []
    report_tracks: list[dict] = []
    fuzzy_total = 0
    recovered = 0
    for track in image.tracks:
        evidence = _track_evidence(track, sectors_per_track)
        fuzzy_total += track.fuzzy_size
        for sector in track.sectors:
            name = f"track {track.track} side {track.side} sector {sector.number}"
            if sector.record_not_found:
                unreadable.append(f"{name}: record not found")
                continue
            if sector.crc_error:
                unreadable.append(f"{name}: CRC error")
                continue
            if sector.size_code != 2 or not 1 <= sector.number <= sectors_per_track:
                continue
            if sector.data is None or len(sector.data) != SECTOR_SIZE:
                unreadable.append(f"{name}: data missing from the capture")
                continue
            slot = (track.track * sides + track.side) * sectors_per_track + sector.number - 1
            if slot in placed:
                continue
            placed.add(slot)
            recovered += 1
            sectors[slot * SECTOR_SIZE : (slot + 1) * SECTOR_SIZE] = sector.data
        report_tracks.append(
            {
                "track": track.track,
                "side": track.side,
                "sectors": len(track.sectors),
                "trackLength": track.track_length,
                "fuzzyBytes": track.fuzzy_size,
                "trackImage": track.has_track_image,
                "evidence": evidence,
            }
        )
    protected_tracks = [row for row in report_tracks if row["evidence"]]
    extra_tracks = image.track_count - tracks
    declared_spt = _declared_sectors_per_track(image)
    protection = {
        "protected": bool(protected_tracks) or bool(unreadable),
        "sectorsRecovered": recovered,
        "sectorsExpected": geometry.total_sectors,
        "unreadableSectors": len(unreadable),
        "fuzzyBytes": fuzzy_total,
        "protectedTracks": len(protected_tracks),
        "extraTracks": extra_tracks,
        "declaredSectorsPerTrack": declared_spt,
        "tracks": report_tracks,
    }
    warnings = [
        f"Read from a Pasti capture (revision {image.revision}): {recovered:,} of "
        f"{geometry.total_sectors:,} sectors recovered as {geometry.label}."
    ]
    if extra_tracks:
        warnings.append(
            f"{extra_tracks} track(s) past the data area hold no sectors and were "
            "left out of the image. Protection is commonly written out there."
        )
    if declared_spt is not None and declared_spt != sectors_per_track:
        warnings.append(
            f"The parameter block claims {declared_spt} sectors per track and the "
            f"capture holds {sectors_per_track}. The capture was believed, because "
            "a disk that does not boot through GEMDOS need not describe itself."
        )
    if unreadable:
        warnings.append(
            f"{len(unreadable)} sector(s) were unreadable on the original disk and are blank here."
        )
    if protected_tracks:
        warnings.append(
            f"{len(protected_tracks)} track(s) carry protection evidence the sector image cannot hold."
        )
    return STXDecode(bytes(sectors), geometry, unreadable, warnings, protection)


def stx_to_st(data: bytes) -> bytes:
    """The plain sector image, with unreadable sectors left blank."""
    return decode_stx(data).image


def protection_report(data: bytes) -> dict:
    """Per-track evidence of what the capture holds beyond plain sectors."""
    return decode_stx(data).protection


__all__ = [
    "FDC_CRC_ERROR",
    "FDC_DELETED_DATA",
    "FDC_LOST_DATA",
    "FDC_RECORD_NOT_FOUND",
    "FILE_HEADER_SIZE",
    "MAGIC",
    "SECTOR_DESCRIPTOR_SIZE",
    "SECTOR_FLAG_FUZZY",
    "SECTOR_FLAG_TIMING",
    "TRACK_FLAG_SECTOR_DESCRIPTORS",
    "TRACK_FLAG_SYNC_OFFSET",
    "TRACK_FLAG_TRACK_IMAGE",
    "TRACK_HEADER_SIZE",
    "WRITABLE",
    "STXDecode",
    "STXError",
    "STXImage",
    "STXSector",
    "STXTrack",
    "decode_stx",
    "is_stx",
    "parse_stx",
    "protection_report",
    "stx_to_st",
]

# Vendored from Atari File Forge, app/msa.py.
# Copyright (c) 2026 Pete Clarke. MIT licence, full text in vendor/__init__.py.
# unpack_track copies literal runs with bytes.find instead of one byte at a
# time, because library scans unpack thousands of archives. Output unchanged.

"""Magic Shadow Archiver images: a whole floppy, one track at a time.

MSA is the compressed form an ST disk is most often distributed in. It is a
sector image with a ten-byte header and a run-length pass over each track,
nothing more: unpacking it gives exactly the ``.st`` file the archiver was
fed, which is why the conversion here is proved byte for byte in both
directions rather than trusted.

The layout, in big-endian words as the 68000 writes them:

* ``0x0E0F`` identifier, sectors per track, sides minus one, first track,
  last track;
* then for every track from first to last, and for every side within it, a
  16-bit length followed by that many bytes. A length equal to the track's
  raw size means the bytes are the sectors themselves; anything shorter is
  run-length data in which ``0xE5`` introduces a run of ``value`` repeated
  ``count`` times, the count being a big-endian word.

The packer follows the original tool's rule: a run of four or more identical
bytes becomes a run record, a literal ``0xE5`` must always be written as a
run of one because it cannot appear bare, and a track is stored packed only
when the packed form is actually shorter. Those rules are what make the
round trip exact.
"""

from __future__ import annotations

from dataclasses import dataclass

from .floppy_geometry import (
    SECTOR_SIZE,
    FloppyGeometry,
    geometry_for_layout,
    resolve_geometry,
)


MAGIC = b"\x0e\x0f"
HEADER_SIZE = 10
RUN_MARKER = 0xE5
#: A run shorter than this is cheaper written out than as a run record.
MINIMUM_RUN = 4
#: The longest run one record can carry.
MAXIMUM_RUN = 0xFFFF


class MSAError(ValueError):
    """An MSA image could not be read or built."""


@dataclass(frozen=True)
class MSATrack:
    """One side of one track, as the archive stores it."""

    track: int
    side: int
    packed_length: int
    unpacked_length: int
    compressed: bool
    data: bytes


@dataclass(frozen=True)
class MSAImage:
    sectors_per_track: int
    sides: int
    start_track: int
    end_track: int
    tracks: tuple[MSATrack, ...]

    @property
    def track_count(self) -> int:
        return self.end_track - self.start_track + 1

    @property
    def geometry(self) -> FloppyGeometry:
        """The shape of the disk the archive describes.

        An archive that starts after track zero is a partial copy; its
        geometry still counts from track zero because that is where the
        rebuilt image places the tracks it holds.
        """
        return geometry_for_layout(self.end_track + 1, self.sides, self.sectors_per_track)

    @property
    def track_size(self) -> int:
        return self.sectors_per_track * SECTOR_SIZE

    @property
    def size(self) -> int:
        return self.geometry.size

    @property
    def packed_size(self) -> int:
        return HEADER_SIZE + sum(2 + item.packed_length for item in self.tracks)

    def sectors(self) -> bytes:
        """The plain sector image, tracks from zero, sides interleaved."""
        image = bytearray(self.size)
        for item in self.tracks:
            offset = (item.track * self.sides + item.side) * self.track_size
            image[offset : offset + self.track_size] = item.data
        return bytes(image)


def is_msa(data: bytes) -> bool:
    return len(data) >= HEADER_SIZE and data[:2] == MAGIC


def unpack_track(packed: bytes, expected: int) -> bytes:
    """Expand one run-length track to exactly ``expected`` bytes."""
    out = bytearray()
    index = 0
    end = len(packed)
    while index < end:
        marker = packed.find(RUN_MARKER, index)
        if marker < 0:
            out += packed[index:]
            if len(out) > expected:
                raise MSAError(
                    f"A track unpacks to more than its {expected:,} bytes."
                )
            break
        out += packed[index:marker]
        index = marker + 1
        if index + 3 > end:
            raise MSAError("A run record is cut off at the end of the track.")
        fill = packed[index]
        count = int.from_bytes(packed[index + 1 : index + 3], "big")
        index += 3
        out += bytes((fill,)) * count
        if len(out) > expected:
            raise MSAError(
                f"A track unpacks to more than its {expected:,} bytes."
            )
    if len(out) != expected:
        raise MSAError(
            f"A track unpacked to {len(out):,} bytes where {expected:,} were expected."
        )
    return bytes(out)


def pack_track(raw: bytes) -> bytes:
    """Run-length encode one track the way the archiver does.

    The result may be longer than the input, which is why callers compare
    the two and keep the shorter; this function only applies the rule.
    """
    out = bytearray()
    index = 0
    end = len(raw)
    while index < end:
        value = raw[index]
        run = 1
        while index + run < end and raw[index + run] == value and run < MAXIMUM_RUN:
            run += 1
        if run >= MINIMUM_RUN or value == RUN_MARKER:
            out += bytes((RUN_MARKER, value)) + run.to_bytes(2, "big")
        else:
            out += bytes((value,)) * run
        index += run
    return bytes(out)


def parse_msa(data: bytes) -> MSAImage:
    """Read an archive completely, refusing anything that does not add up."""
    if not is_msa(data):
        raise MSAError("The file does not start with the MSA identifier 0x0E0F.")
    sectors_per_track = int.from_bytes(data[2:4], "big")
    sides = int.from_bytes(data[4:6], "big") + 1
    start_track = int.from_bytes(data[6:8], "big")
    end_track = int.from_bytes(data[8:10], "big")
    if not 1 <= sectors_per_track <= 36:
        raise MSAError(f"An MSA cannot have {sectors_per_track} sectors per track.")
    if sides not in (1, 2):
        raise MSAError(f"An MSA cannot have {sides} sides.")
    if end_track < start_track or end_track > 255:
        raise MSAError(
            f"The MSA track range {start_track}-{end_track} is not a disk."
        )
    track_size = sectors_per_track * SECTOR_SIZE
    tracks: list[MSATrack] = []
    index = HEADER_SIZE
    for track in range(start_track, end_track + 1):
        for side in range(sides):
            if index + 2 > len(data):
                raise MSAError(
                    f"The archive ends before track {track} side {side}."
                )
            length = int.from_bytes(data[index : index + 2], "big")
            index += 2
            packed = data[index : index + length]
            if len(packed) != length:
                raise MSAError(
                    f"Track {track} side {side} claims {length:,} bytes but the "
                    f"archive holds only {len(packed):,} more."
                )
            index += length
            if length > track_size:
                raise MSAError(
                    f"Track {track} side {side} is longer than a {track_size:,}-byte track."
                )
            compressed = length != track_size
            unpacked = unpack_track(packed, track_size) if compressed else packed
            tracks.append(
                MSATrack(track, side, length, track_size, compressed, bytes(unpacked))
            )
    if index != len(data):
        raise MSAError(
            f"{len(data) - index:,} bytes follow the last track of the archive."
        )
    return MSAImage(sectors_per_track, sides, start_track, end_track, tuple(tracks))


def msa_to_st(data: bytes) -> bytes:
    """Rebuild the sector image an archive was made from."""
    return parse_msa(data).sectors()


def st_to_msa(image: bytes, geometry: FloppyGeometry | None = None) -> bytes:
    """Archive a sector image, packing each track only when that is shorter.

    The geometry is read from the boot sector when the caller does not
    supply one, falling back to the size when it names exactly one shape.
    An image whose shape cannot be settled is refused rather than guessed,
    because the archive header would then describe a different disk.
    """
    if geometry is None:
        geometry = resolve_geometry(len(image), image[:SECTOR_SIZE])
    if geometry is None:
        raise MSAError(
            f"A {len(image):,}-byte image has no boot sector geometry and its size "
            "does not identify one shape; state the geometry explicitly."
        )
    if geometry.size != len(image):
        raise MSAError(
            f"The image is {len(image):,} bytes but {geometry.label} is {geometry.size:,}."
        )
    if geometry.sides not in (1, 2) or not 1 <= geometry.sectors <= 36:
        raise MSAError(f"{geometry.label} cannot be written as an MSA.")
    header = (
        MAGIC
        + geometry.sectors.to_bytes(2, "big")
        + (geometry.sides - 1).to_bytes(2, "big")
        + (0).to_bytes(2, "big")
        + (geometry.tracks - 1).to_bytes(2, "big")
    )
    body = bytearray()
    track_size = geometry.track_size
    for track in range(geometry.tracks):
        for side in range(geometry.sides):
            offset = (track * geometry.sides + side) * track_size
            raw = image[offset : offset + track_size]
            packed = pack_track(raw)
            chosen = packed if len(packed) < len(raw) else raw
            body += len(chosen).to_bytes(2, "big") + chosen
    return header + bytes(body)


def msa_project(data: bytes) -> dict:
    """Describe an archive track by track, for the inspector view."""
    parsed = parse_msa(data)
    geometry = parsed.geometry
    rows = [
        {
            "track": item.track,
            "side": item.side,
            "packedLength": item.packed_length,
            "unpackedLength": item.unpacked_length,
            "compressed": item.compressed,
        }
        for item in parsed.tracks
    ]
    return {
        "format": "msa",
        "sectorsPerTrack": parsed.sectors_per_track,
        "sides": parsed.sides,
        "startTrack": parsed.start_track,
        "endTrack": parsed.end_track,
        "geometry": {"id": geometry.identifier, "label": geometry.label},
        "size": parsed.size,
        "packedSize": parsed.packed_size,
        "compressedTracks": sum(1 for item in parsed.tracks if item.compressed),
        "tracks": rows,
    }


__all__ = [
    "HEADER_SIZE",
    "MAGIC",
    "MINIMUM_RUN",
    "RUN_MARKER",
    "MSAError",
    "MSAImage",
    "MSATrack",
    "is_msa",
    "msa_project",
    "msa_to_st",
    "pack_track",
    "parse_msa",
    "st_to_msa",
    "unpack_track",
]

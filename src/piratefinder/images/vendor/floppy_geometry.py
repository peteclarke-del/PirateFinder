# Vendored from Atari File Forge, app/floppy_geometry.py.
# Copyright (c) 2026 Pete Clarke. MIT licence, full text in vendor/__init__.py.

"""The floppy geometries an Atari ST reads, in one table.

An ST floppy is IBM-style MFM: 512-byte sectors, nine, ten or eleven of them
per track, on one or two sides, over eighty tracks or a few more when the
formatter squeezed extra ones in. That freedom is the whole problem. A plain
``.st`` sector image carries no header, so its size is the only external clue
to its shape, and the size does not always settle it: a 360 KiB file is
either eighty single-sided tracks of nine sectors or forty double-sided ones.

The boot sector does settle it. TOS writes a BIOS parameter block into the
first sector, in Intel byte order, naming the sectors per track, the side
count and the total sector count. Every caller that has the first sector must
prefer :func:`geometry_for_boot_sector` and only fall back to the size when
the boot sector is blank or damaged. This module is the single source of
truth for those shapes; the flux policy, the floppy controller adapter and
the container decoders all read from here rather than keeping their own copy.

Nothing here reads a file: the functions take bytes and integers so they can
be exercised without media.
"""

from __future__ import annotations

from dataclasses import dataclass


SECTOR_SIZE = 512

#: Where the BIOS parameter block keeps each value, and how wide it is.
#: TOS writes these little-endian, unlike everything else on the machine.
BPB_BYTES_PER_SECTOR = 0x0B
BPB_TOTAL_SECTORS = 0x13
BPB_SECTORS_PER_TRACK = 0x18
BPB_SIDES = 0x1A


@dataclass(frozen=True)
class FloppyGeometry:
    """One floppy layout: how many tracks, sides and sectors, and how big."""

    identifier: str
    label: str
    tracks: int
    sides: int
    sectors: int
    sector_size: int = SECTOR_SIZE

    @property
    def size(self) -> int:
        return self.tracks * self.sides * self.sectors * self.sector_size

    @property
    def track_size(self) -> int:
        """The bytes one side of one track holds."""
        return self.sectors * self.sector_size

    @property
    def total_sectors(self) -> int:
        return self.tracks * self.sides * self.sectors

    @property
    def extension(self) -> str:
        """The sector-image suffix for this shape.

        Every floppy an ST reads is written as ``.st`` whatever its side
        count; there is no separate suffix for single-sided media.
        """
        return ".st"

    @property
    def kibibytes(self) -> int:
        return self.size // 1024

    def describe(self) -> str:
        """A short shape description, for warnings and progress messages."""
        sides = "single-sided" if self.sides == 1 else "double-sided"
        return f"{self.tracks} tracks, {sides}, {self.sectors} sectors per track"


def _st_geometry(tracks: int, sides: int, sectors: int) -> FloppyGeometry:
    prefix = "ss" if sides == 1 else "ds"
    density = "SS/DD" if sides == 1 else "DS/DD"
    kibibytes = tracks * sides * sectors * SECTOR_SIZE // 1024
    return FloppyGeometry(
        identifier=f"{prefix}-{tracks}t-{sectors}s",
        label=f"Atari ST {density}, {tracks} tracks x {sectors} sectors ({kibibytes} KiB)",
        tracks=tracks,
        sides=sides,
        sectors=sectors,
    )


#: The shapes the workbench recognises, keyed by identifier.
#:
#: The ST family covers what TOS and the popular formatters produce: eighty
#: tracks is the drive's nominal count, and 81 to 83 are the extra tracks a
#: formatter such as FastCopy squeezes in. The two 40-track entries are PC
#: 5.25-inch media an ST reads through a suitable drive, and ``hd-1440k`` is
#: the high-density disk a Mega STE or TT formats.
GEOMETRIES: dict[str, FloppyGeometry] = {
    geometry.identifier: geometry
    for geometry in (
        *(
            _st_geometry(tracks, sides, sectors)
            for tracks in (80, 81, 82, 83)
            for sides in (1, 2)
            for sectors in (9, 10, 11)
        ),
        FloppyGeometry("pc-180k", "PC 5.25 inch SS/DD, 40 tracks x 9 sectors (180 KiB)", 40, 1, 9),
        FloppyGeometry("pc-360k", "PC 5.25 inch DS/DD, 40 tracks x 9 sectors (360 KiB)", 40, 2, 9),
        FloppyGeometry("hd-1440k", "Atari HD, 80 tracks x 18 sectors (1440 KiB)", 80, 2, 18),
    )
}

#: The everyday double-sided 720 KiB disk, used wherever one shape must be
#: picked without any other evidence, such as a blank image.
DEFAULT_GEOMETRY = GEOMETRIES["ds-80t-9s"]


def geometry(identifier: str) -> FloppyGeometry:
    """Return one geometry by identifier, or explain the accepted names."""
    found = GEOMETRIES.get(str(identifier or "").strip().lower())
    if found is None:
        raise KeyError(
            "Choose a floppy geometry: " + ", ".join(sorted(GEOMETRIES)) + "."
        )
    return found


def canonical_sizes() -> frozenset[int]:
    """Every image size the table produces."""
    return frozenset(item.size for item in GEOMETRIES.values())


def geometries_for_size(size: int, sides: int | None = None) -> list[FloppyGeometry]:
    """Every geometry that produces an image of exactly ``size`` bytes.

    The list is ordered by preference: eighty tracks before the extended
    counts, double-sided before single-sided, and the ST family before the
    PC one. A caller that has the boot sector must not use this to choose;
    it is for the case where the size is the only evidence, and a result
    longer than one item means the size does not decide.
    """
    matches = [
        item
        for item in GEOMETRIES.values()
        if item.size == size and (sides is None or item.sides == sides)
    ]
    return sorted(
        matches,
        key=lambda item: (
            item.tracks != 80,
            item.tracks < 80,
            item.tracks,
            -item.sides,
            item.identifier.startswith("pc-"),
        ),
    )


def geometry_for_size(size: int, sides: int | None = None) -> FloppyGeometry | None:
    """The geometry a size describes, when it describes exactly one."""
    matches = geometries_for_size(size, sides)
    return matches[0] if len(matches) == 1 else None


def _word(data: bytes, offset: int) -> int:
    return int.from_bytes(data[offset : offset + 2], "little")


def geometry_for_boot_sector(boot: bytes) -> FloppyGeometry | None:
    """Read the shape TOS recorded in the BIOS parameter block.

    Returns None when the block is absent or self-contradictory: a blank
    sector, a sector-size other than 512, a side count other than one or
    two, or a total that is not a whole number of tracks. A block that names
    a shape the table does not carry is returned as a geometry of its own,
    because the boot sector is the disk's word on the matter and the table
    is only the list of shapes the workbench has names for.
    """
    if len(boot) < BPB_SIDES + 2:
        return None
    bytes_per_sector = _word(boot, BPB_BYTES_PER_SECTOR)
    total = _word(boot, BPB_TOTAL_SECTORS)
    sectors = _word(boot, BPB_SECTORS_PER_TRACK)
    sides = _word(boot, BPB_SIDES)
    if bytes_per_sector != SECTOR_SIZE or sides not in (1, 2) or not 1 <= sectors <= 36:
        return None
    if total == 0 or total % (sectors * sides):
        return None
    tracks = total // (sectors * sides)
    if not 1 <= tracks <= 255:
        return None
    for item in GEOMETRIES.values():
        if (item.tracks, item.sides, item.sectors) == (tracks, sides, sectors):
            return item
    density = "single-sided" if sides == 1 else "double-sided"
    return FloppyGeometry(
        identifier=f"boot-{tracks}t-{sides}h-{sectors}s",
        label=f"{tracks} tracks, {density}, {sectors} sectors per track (from the boot sector)",
        tracks=tracks,
        sides=sides,
        sectors=sectors,
    )


def resolve_geometry(size: int, boot: bytes | None = None) -> FloppyGeometry | None:
    """Decide an image's shape from what is known about it.

    The boot sector is believed when it agrees with the size; a block whose
    total does not match the file is ignored rather than trusted, since a
    boot sector copied from another disk is a common way for the two to
    disagree. Without a usable boot sector the size decides, and only when
    it names exactly one shape.
    """
    if boot:
        declared = geometry_for_boot_sector(boot)
        if declared is not None and declared.size == size:
            return declared
    return geometry_for_size(size)


def geometry_for_layout(tracks: int, sides: int, sectors: int) -> FloppyGeometry:
    """A geometry for an explicit layout, from the table when it has one."""
    for item in GEOMETRIES.values():
        if (item.tracks, item.sides, item.sectors) == (tracks, sides, sectors):
            return item
    density = "single-sided" if sides == 1 else "double-sided"
    return FloppyGeometry(
        identifier=f"layout-{tracks}t-{sides}h-{sectors}s",
        label=f"{tracks} tracks, {density}, {sectors} sectors per track",
        tracks=tracks,
        sides=sides,
        sectors=sectors,
    )


__all__ = [
    "BPB_BYTES_PER_SECTOR",
    "BPB_SECTORS_PER_TRACK",
    "BPB_SIDES",
    "BPB_TOTAL_SECTORS",
    "DEFAULT_GEOMETRY",
    "GEOMETRIES",
    "SECTOR_SIZE",
    "FloppyGeometry",
    "canonical_sizes",
    "geometries_for_size",
    "geometry",
    "geometry_for_boot_sector",
    "geometry_for_layout",
    "geometry_for_size",
    "resolve_geometry",
]

"""Disk definitions for layouts gw has no built-in format for.

gw writes exactly the cylinders its format names. With a built-in format an
82-cylinder ST image written as ``atarist.800`` silently loses cylinders 80
and 81, so any layout other than the standard 80 cylinders gets a generated
definition with the real count. The track parameters are copied from gw
1.23's own ``atarist.*``, ``ibm.1440`` and ``amiga.amigados`` definitions
(``data/diskdefs_*.cfg``), so a generated format differs from the built-in
one only in cylinders, heads and sectors.

A ``--diskdefs`` file replaces gw's built-in set for that invocation, so a
command that uses one names only the format defined in it.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ..models import Geometry, Platform

#: gw's built-in formats, keyed by (platform, cylinders, heads, sectors).
BUILTIN_FORMATS: dict[tuple[Platform, int, int, int], str] = {
    (Platform.ATARI_ST, 80, 1, 9): "atarist.360",
    (Platform.ATARI_ST, 80, 1, 10): "atarist.400",
    (Platform.ATARI_ST, 80, 1, 11): "atarist.440",
    (Platform.ATARI_ST, 80, 2, 9): "atarist.720",
    (Platform.ATARI_ST, 80, 2, 10): "atarist.800",
    (Platform.ATARI_ST, 80, 2, 11): "atarist.880",
    (Platform.ATARI_ST, 80, 2, 18): "ibm.1440",
    (Platform.AMIGA, 80, 2, 11): "amiga.amigados",
    (Platform.AMIGA, 80, 2, 22): "amiga.amigados_hd",
}

#: ibm.mfm track parameters of gw's atarist.* definitions by sectors per
#: track, and of ibm.1440 for high density. Nine-sector tracks also carry a
#: skew that depends on the head count; see ``_st_track``.
_ST_TRACKS: dict[int, tuple[tuple[str, str], ...]] = {
    9: (("bps", "512"), ("gap3", "84"), ("rate", "250"), ("iam", "no")),
    10: (("bps", "512"), ("gap3", "30"), ("rate", "250"), ("iam", "no")),
    11: (("bps", "512"), ("gap3", "3"), ("rate", "261"), ("iam", "no")),
    18: (("bps", "512"), ("gap3", "108"), ("rate", "500")),
}
_AMIGA_SECTORS = (11, 22)
#: Drives step a few cylinders past 80; beyond this the head hits its stop.
MAX_CYLINDERS = 86


class DiskDefError(ValueError):
    """A layout has no track parameters to build a definition from."""


@dataclass(frozen=True, slots=True)
class DiskDefinition:
    name: str  # the value for --format
    text: str  # the contents of the --diskdefs file


def builtin_format(geometry: Geometry, platform: Platform) -> str | None:
    """gw's own format name for a layout, or None when it has none."""
    return BUILTIN_FORMATS.get((platform, geometry.cylinders, geometry.heads, geometry.sectors))


def custom_definition(geometry: Geometry, platform: Platform) -> DiskDefinition:
    """A disk definition for ``geometry``, with gw's track parameters for it."""
    cylinders, heads, sectors = geometry.cylinders, geometry.heads, geometry.sectors
    if not 1 <= cylinders <= MAX_CYLINDERS or heads not in (1, 2):
        raise DiskDefError(
            f"{cylinders} cylinders on {heads} side(s) is not a layout a floppy drive can write."
        )
    if geometry.sector_size != 512:
        raise DiskDefError(f"Sectors of {geometry.sector_size} bytes are not supported.")
    if platform is Platform.AMIGA:
        if sectors not in _AMIGA_SECTORS:
            raise DiskDefError(f"An Amiga track holds 11 or 22 sectors, not {sectors}.")
        name = f"amiga_{cylinders}_{heads}_{sectors}"
        track = ["    tracks * amiga.amigados", f"        secs = {sectors}", "    end"]
    else:
        if sectors not in _ST_TRACKS:
            supported = ", ".join(str(count) for count in _ST_TRACKS)
            raise DiskDefError(
                f"Greaseweazle has no Atari ST track layout with {sectors} sectors per "
                f"track; the supported counts are {supported}."
            )
        name = f"st_{cylinders}_{heads}_{sectors}"
        track = _st_track(heads, sectors)
    lines = [f"disk {name}", f"    cyls = {cylinders}", f"    heads = {heads}", *track, "end"]
    return DiskDefinition(name, "\n".join(lines) + "\n")


def _st_track(heads: int, sectors: int) -> list[str]:
    parameters = list(_ST_TRACKS[sectors])
    if sectors == 9:
        # atarist.360 uses cskew 2; atarist.720 uses cskew 4 with hskew 2.
        parameters += [("cskew", "2")] if heads == 1 else [("cskew", "4"), ("hskew", "2")]
    body = [f"        {key} = {value}" for key, value in parameters]
    return ["    tracks * ibm.mfm", f"        secs = {sectors}", *body, "    end"]


def write_definition(definition: DiskDefinition, path: str | Path) -> Path:
    """Write the definition to ``path`` and return it."""
    target = Path(path)
    target.write_text(definition.text, encoding="ascii")
    return target

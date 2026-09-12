"""Where a downloaded disk is filed: ``<platform>/<type>/<crew>/<file>``.

Downloads are kept for archiving, so disks from different machines, of
different kinds and from different crews never share a folder:
``Atari ST/Games/Automation/Automation Menu Disk 250 (1990)(Automation).st``
or ``Amiga/Demos/Effect/Prevail Pack 147 (1993)(Effect).adf``.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable

from .models import Content, ContentKind, Disk, DiskKind, Platform

PLATFORM_FOLDERS = {Platform.AMIGA: "Amiga", Platform.ATARI_ST: "Atari ST"}
UNKNOWN_PLATFORM = "Other"

GAMES = "Games"
APPLICATIONS = "Applications"
DEMOS = "Demos"
MUSIC = "Music"
UNKNOWN_CREW = "Unknown crew"
DEFAULT_FOLDERS = (GAMES, UNKNOWN_CREW)

# What a disk is mainly made of decides its type. Intros, documents, cheats
# and trainers accompany the programs on a menu disk, so they only decide the
# type of a disk that holds nothing else.
_MAIN_TYPES = {
    ContentKind.GAME: GAMES,
    ContentKind.UTILITY: APPLICATIONS,
    ContentKind.DEMO: DEMOS,
    ContentKind.MUSIC: MUSIC,
}
_ACCOMPANYING_TYPES = {
    ContentKind.INTRO: DEMOS,
    ContentKind.DOC: GAMES,
    ContentKind.CHEAT: GAMES,
    ContentKind.TRAINER: GAMES,
}
_TYPE_ORDER = (GAMES, APPLICATIONS, DEMOS, MUSIC)
# A disk with no contents listed is filed by what kind of release it is.
_KIND_TYPES = {
    DiskKind.MENU: GAMES,
    DiskKind.SINGLE: GAMES,
    DiskKind.COMPILATION: GAMES,
    DiskKind.PACK: DEMOS,
}


def platform_folder(platform: Platform | str | None) -> str:
    """The folder for a platform: "Amiga" or "Atari ST"."""
    if platform is None:
        return UNKNOWN_PLATFORM
    try:
        return PLATFORM_FOLDERS[Platform(platform)]
    except ValueError:
        return str(platform)


def archive_type(disk: Disk, contents: Iterable[Content]) -> str:
    """Games, Applications, Demos or Music, from what the disk holds."""
    contents = list(contents)
    for table in (_MAIN_TYPES, _ACCOMPANYING_TYPES):
        counts = Counter(table[c.kind] for c in contents if c.kind in table)
        if counts:
            return max(_TYPE_ORDER, key=lambda name: (counts[name], -_TYPE_ORDER.index(name)))
    return _KIND_TYPES.get(disk.kind, GAMES)


def archive_crew(disk: Disk, series_group: str = "") -> str:
    """The crew a disk belongs to.

    A numbered series belongs to its crew (Skid Row for Skid Row Compact).
    A single disk belongs to whoever cracked it, or its publisher when it
    was never cracked.
    """
    if disk.series_id:
        return series_group or disk.series_name or UNKNOWN_CREW
    return disk.cracker or disk.publisher or UNKNOWN_CREW


def archive_folders(
    disk: Disk, contents: Iterable[Content], series_group: str = ""
) -> tuple[str, str]:
    """The type and crew folders a disk is filed under, below its platform."""
    return archive_type(disk, contents), archive_crew(disk, series_group)

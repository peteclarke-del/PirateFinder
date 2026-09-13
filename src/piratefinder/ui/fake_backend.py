"""A backend with synthetic discs, for tests and screenshots.

Nothing here reads the catalogue, the user database, the network or a
Greaseweazle. The discs are invented examples in the shape of real catalogue
entries, so the window can be driven and photographed on any machine. The
pictures are drawn by this module when they are first asked for, and are
never real screenshots. Run the application with ``PIRATEFINDER_FAKE_BACKEND=1``
to try it by hand.
"""

from __future__ import annotations

import colorsys
import random
import re
import shutil
import struct
import tempfile
import threading
import time
import zlib
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path

from ..app_update import AppRelease, PackageTarget
from ..archive_layout import archive_crew, archive_type
from ..greaseweazle.caps import CapsState, CapsStatus, builds
from ..jobs.queue import clamp_copies, copy_labels, queue_key
from ..jobs.session import with_notes
from ..library.corrections import (
    EDITABLE_FIELDS,
    Correction,
    apply_contents,
    apply_disk,
    corrected_titles,
    correction_from_form,
)
from ..models import (
    Availability,
    BootRecheck,
    Content,
    ContentKind,
    CrewInfo,
    DeviceStatus,
    Disk,
    DiskDetail,
    DiskKind,
    Facets,
    ImageRecord,
    Link,
    LocalFile,
    Location,
    MediaItem,
    Platform,
    Query,
    QueueItem,
    ResultMode,
    ResultPage,
    ResultRow,
    ScanSummary,
    Series,
    SessionSummary,
    SortOrder,
    TriviaItem,
    VirusReport,
    VirusStatus,
    WriteOutcome,
    WriteProgress,
    WriteStatus,
)
from ..online.releases import UpdateCancelled, UpdateError
from ..settings import Settings
from .backend import (
    Backend,
    BrainfileStatus,
    CatalogueInfo,
    Downloaded,
    LibraryStats,
    ProgressCallback,
    ScanProgress,
    UpdateOffer,
)
from .formatting import KIND_NAMES, platform_name

ST = Platform.ATARI_ST
AMIGA = Platform.AMIGA
G = ContentKind.GAME

LIBRARY = "/home/user/Floppy Images"
#: The host tools the package bundles (packaging/greaseweazle-version.txt; a test checks).
HOST_TOOLS = "1.23"
SYNTHETIC = "synthetic"  # the source id of every invented picture and fact
#: The pinned SPS Decoder Library build the fake computer, a 64-bit PC, is offered.
CAPS_BUILD = next(build for build in builds() if "x86_64" in build.machines)

SERIES = (
    Series("automation", "Automation", ST, DiskKind.MENU, "Automation", ("auto",), 83),
    Series("pompey", "Pompey Pirates", ST, DiskKind.MENU, "Pompey Pirates", ("pp",), 3),
    Series("medway", "Medway Boys", ST, DiskKind.MENU, "Medway Boys", ("mb",), 13),
    Series("dbug", "D-Bug", ST, DiskKind.MENU, "D-Bug", (), 11),
    Series("skidrow-compact", "Skid Row Compact", AMIGA, DiskKind.MENU, "Skid Row", ("src",), 12),
    Series("demo-pack", "Demo Pack", AMIGA, DiskKind.PACK, "Demo Pack", (), 6),
)
SERIES_BY_ID = {item.id: item for item in SERIES}

PROVIDERS = (
    ("internet-archive", "Internet Archive"),
    ("atari-legend", "Atari Legend"),
    ("d-bug", "D-Bug"),
)
SOURCES = (*PROVIDERS, ("tosec", "TOSEC"), (SYNTHETIC, "PirateFinder test data"))

# Hand-made discs: (id, label, platform, kind, series, number, date, extra fields)
_HAND_MADE = (
    (1, "Automation 250", ST, DiskKind.MENU, "automation", 250, "1990-03", {}),
    (2, "Automation 251", ST, DiskKind.MENU, "automation", 251, "1990-04", {}),
    (3, "Automation 252", ST, DiskKind.MENU, "automation", 252, "1990", {}),
    (4, "Pompey Pirates 51", ST, DiskKind.MENU, "pompey", 51, "1991-02", {}),
    (5, "Pompey Pirates 52", ST, DiskKind.MENU, "pompey", 52, "1991", {}),
    (6, "Medway Boys 60", ST, DiskKind.MENU, "medway", 60, "1990-11", {}),
    (7, "D-Bug 100 B", ST, DiskKind.MENU, "dbug", 100, "1993", {"part": "B"}),
    (8, "Skid Row Compact 12", AMIGA, DiskKind.MENU, "skidrow-compact", 12, "1991-05", {}),
    (9, "Skid Row Compact 13", AMIGA, DiskKind.MENU, "skidrow-compact", 13, "1991-06", {}),
    (10, "Demo Pack 41", AMIGA, DiskKind.PACK, "demo-pack", 41, "1992", {}),
    (
        11,
        "Speedball 2",
        AMIGA,
        DiskKind.SINGLE,
        None,
        None,
        "1990",
        {"publisher": "Image Works", "cracker": "Skid Row"},
    ),
    (
        12,
        "Arcade Collection Disk 1",
        ST,
        DiskKind.COMPILATION,
        None,
        None,
        "1991",
        {"part": "1 of 2"},
    ),
    (13, "Pompey Pirates 1", ST, DiskKind.MENU, "pompey", 1, "1989-06-17", {}),
)

CONTENTS: dict[int, tuple[tuple[str, ContentKind, str], ...]] = {
    1: (("Necron", G, "+2 trainer"), ("Boulderdash CK", G, ""), ("Docs", ContentKind.DOC, "")),
    2: (("Xenon 2 Megablast", G, ""), ("Oids", G, "")),
    3: (("Stunt Car Racer", G, ""), ("Blood Money", G, "STE only")),
    4: (("Rick Dangerous", G, "+ trainer"), ("Menace", G, "")),
    5: (("Carrier Command", G, ""), ("Chrono Quest", G, "[doc]")),
    6: (("Kick Off 2", G, ""), ("Populous", G, ""), ("Intro", ContentKind.INTRO, "")),
    7: (("Dungeon Master", G, "+ hints"), ("Sound Tracker", ContentKind.MUSIC, "")),
    8: (("Lemmings", G, ""), ("Rick Dangerous", G, "")),
    9: (("Xenon 2 Megablast", G, ""), ("Menace", G, "")),
    10: (("Rink Demo", ContentKind.DEMO, ""), ("Tracker Tunes", ContentKind.MUSIC, "")),
    11: (("Speedball 2", G, ""),),
    12: (("Blood Money", G, ""), ("Oids", G, ""), ("Kick Off 2", G, "")),
    13: (("Rick Dangerous", G, ""), ("Xenon", G, ""), ("Menu Music", ContentKind.MUSIC, "")),
}

# Invented titles for the generated discs, so the pager has pages to show.
_FIRST_WORDS = (
    "Astro",
    "Blaster",
    "Crystal",
    "Dragon",
    "Echo",
    "Fury",
    "Galaxy",
    "Hyper",
    "Iron",
    "Jet",
    "Laser",
    "Mega",
    "Nova",
    "Omega",
    "Power",
    "Quantum",
    "Rocket",
    "Star",
    "Thunder",
    "Turbo",
    "Vortex",
    "Zenith",
)
_SECOND_WORDS = (
    "Attack",
    "Blade",
    "Caverns",
    "Drift",
    "Force",
    "Hunter",
    "Knights",
    "Patrol",
    "Quest",
    "Racer",
    "Runner",
    "Strike",
    "Warrior",
    "Zone",
)


def synthetic_title(number: int) -> str:
    first = _FIRST_WORDS[number % len(_FIRST_WORDS)]
    second = _SECOND_WORDS[(number * 5 + number // len(_FIRST_WORDS)) % len(_SECOND_WORDS)]
    return f"{first} {second}"


# Generated series: (series, first number, count, first id, year, platform, content kinds)
_GENERATED = (
    ("automation", 253, 80, 100, 1990, ST, (G, G, G)),
    ("medway", 61, 12, 200, 1991, ST, (G, G)),
    ("dbug", 101, 10, 300, 1992, ST, (G, ContentKind.UTILITY)),
    ("skidrow-compact", 14, 10, 400, 1991, AMIGA, (G, G)),
    ("demo-pack", 42, 5, 500, 1992, AMIGA, (ContentKind.DEMO, ContentKind.MUSIC)),
)


def _build_discs() -> tuple[tuple[Disk, ...], dict[int, tuple[Content, ...]]]:
    raw: list[tuple[int, str, Platform, DiskKind, str | None, int | None, str, dict]] = list(
        _HAND_MADE
    )
    contents = dict(CONTENTS)
    counter = 0
    for series_id, first, count, first_id, year, platform, kinds in _GENERATED:
        series = SERIES_BY_ID[series_id]
        for offset in range(count):
            disk_id = first_id + offset
            number = first + offset
            month = offset % 12 + 1
            date = f"{year + offset // 12}-{month:02d}" if offset % 4 else str(year + offset // 12)
            extra: dict = {}
            if series_id == "dbug" and offset == 4:
                kinds_here: tuple[ContentKind, ...] = (ContentKind.UTILITY, ContentKind.UTILITY)
            else:
                kinds_here = kinds
            titles = []
            for kind in kinds_here:
                counter += 1
                name = synthetic_title(counter)
                if kind == ContentKind.UTILITY:
                    name = f"{name} Editor"
                elif kind == ContentKind.MUSIC:
                    name = f"{name} Tunes"
                elif kind == ContentKind.DEMO:
                    name = f"{name} Demo"
                titles.append((name, kind, ""))
            contents[disk_id] = tuple(titles)
            raw.append(
                (
                    disk_id,
                    f"{series.name} {number}",
                    platform,
                    series.kind,
                    series_id,
                    number,
                    date,
                    extra,
                )
            )
    discs = []
    built_contents: dict[int, tuple[Content, ...]] = {}
    for disk_id, label, platform, kind, series_id, number, date, extra in raw:
        series = SERIES_BY_ID.get(series_id or "")
        year_text = date[:4]
        group = f"({series.group})" if series and series.group else ""
        disk = Disk(
            id=disk_id,
            label=label,
            platform=platform,
            kind=kind,
            series_id=series_id,
            series_name=series.name if series else "",
            number=number,
            title=f"{label} ({year_text}){group}",
            date=date,
            year=int(year_text),
            month=int(date[5:7]) if len(date) >= 7 else None,
            day=int(date[8:10]) if len(date) >= 10 else None,
            **extra,
        )
        items = tuple(
            Content(
                disk_id,
                title,
                content_kind,
                position,
                extra=note,
                source=SYNTHETIC,
                id=disk_id * 100 + position + 1,
            )
            for position, (title, content_kind, note) in enumerate(contents.get(disk_id, ()))
        )
        disk = replace(
            disk,
            category=archive_type(disk, items),
            crew=archive_crew(disk, series.group if series else ""),
        )
        discs.append(disk)
        built_contents[disk_id] = items
    return tuple(discs), built_contents


DISKS, _CONTENTS = _build_discs()
DISKS_BY_ID = {disk.id: disk for disk in DISKS}


def _contents(disk_id: int) -> tuple[Content, ...]:
    return _CONTENTS.get(disk_id, ())


CONDITIONS = {1: "intact", 2: "intact", 3: "damaged", 4: "intact"}
NOTES = {
    1: (
        "Menu with a scrolling message and chip music.",
        "Packed by the crew. Menu code and music are credited on the scroller.",
    ),
    3: ("The second game does not load from this dump.", ""),
}

# Viruses: a removable boot block virus on a library file, and a dump that
# the catalogue flags with a file virus, which has a clean alternate.
BOOT_VIRUS_DISK = 9
FLAGGED_DISK = 10
BOOT_VIRUS = VirusReport(
    VirusStatus.VIRUS,
    name="SCA",
    kind="boot",
    removable=True,
    explanation=(
        "The boot block holds the SCA virus, which copies itself onto other disks. "
        "PirateFinder can replace it with a standard AmigaDOS boot block."
    ),
    source="built-in",
)
FILE_VIRUS = VirusReport(
    VirusStatus.FLAGGED,
    name="Saddam",
    kind="file",
    removable=False,
    explanation=(
        "TOSEC lists this dump as infected with Saddam, a file virus that lives outside the "
        "boot block. PirateFinder cannot remove it."
    ),
    source="TOSEC",
)
UNMATCHED_VIRUS = VirusReport(
    VirusStatus.VIRUS,
    name="Byte Bandit",
    kind="boot",
    removable=True,
    explanation=(
        "The boot block holds the Byte Bandit virus, which copies itself onto other disks. "
        "PirateFinder can replace it with a standard AmigaDOS boot block."
    ),
    source="built-in",
)
# Boot blocks that are not viruses: on the library copy of Pompey Pirates 51,
# and on the first unmatched file.
TOS_LOADER = VirusReport(
    VirusStatus.KNOWN_BOOT,
    name="TOS boot loader",
    explanation=(
        "The boot sector is a TOS boot loader, which loads a program when the disk starts. "
        "It is not a virus."
    ),
    source="built-in",
)
BOOT_BLOCK_DISK = 4
IMMUNISER = VirusReport(
    VirusStatus.ANTIVIRUS,
    name="Immuniser",
    explanation=(
        "The boot sector is an immuniser: executable code that only returns, so boot viruses "
        "that look for an executable sector leave the disk alone. It is harmless."
    ),
    source="built-in",
)


def _images(disk: Disk) -> tuple[ImageRecord, ...]:
    extension = "adf" if disk.platform == AMIGA else "st"
    base = disk.title or disk.label
    flagged = disk.id == FLAGGED_DISK
    records = [
        ImageRecord(
            id=disk.id * 10,
            disk_id=disk.id,
            name=f"{base}[v Saddam].{extension}" if flagged else f"{base}.{extension}",
            format=extension,
            flags="[v Saddam]" if flagged else "",
            size=901120 if extension == "adf" else 737280,
            md5=f"{disk.id:032x}",
            rank=0,
            source="tosec",
            virus="Saddam" if flagged else "",
        )
    ]
    if disk.id in (1, 4, 8, FLAGGED_DISK):
        records.append(
            ImageRecord(
                id=disk.id * 10 + 1,
                disk_id=disk.id,
                name=f"{base}[a].{extension}",
                format=extension,
                flags="[a]",
                size=records[0].size,
                md5=f"{disk.id + 100:032x}",
                rank=1,
                source="tosec",
            )
        )
    if disk.id == 3:
        records.append(
            ImageRecord(
                id=31,
                disk_id=3,
                name=f"{base}[b].msa",
                format="msa",
                flags="[b]",
                size=412000,
                bad=True,
                rank=9,
                source="atari-legend",
            )
        )
    return tuple(records)


# Which discs are in the library, and which can be downloaded from where.
LOCAL_DISKS = {
    1: "Automation/Automation 250.st",
    4: "Pompey/PP51.msa",
    9: "Amiga/src13.zip",
    13: "Pompey/PP01.st",
    **{disk.id: f"Automation/{disk.label}.st" for disk in DISKS if disk.id in range(100, 140, 7)},
}
ONLINE_DISKS = {
    1: "atari-legend",
    2: "atari-legend",
    3: "atari-legend",
    5: "d-bug",
    7: "d-bug",
    8: "internet-archive",
    9: "internet-archive",
    10: "internet-archive",
    11: "internet-archive",
    13: "atari-legend",
    **{
        disk.id: "internet-archive" if disk.platform == AMIGA else "atari-legend"
        for disk in DISKS
        if disk.id >= 100 and disk.id % 3
    },
}

UNMATCHED = (
    LocalFile(
        path=f"{LIBRARY}/Unsorted/menu17.st",
        format="st",
        size=737280,
        md5="f" * 32,
        volume_label="MENU17",
        listing=("MENU.PRG", "GAME1.PRG", "GAME2.PRG"),
        display_name="menu17.st",
    ),
    LocalFile(
        path=f"{LIBRARY}/Unsorted/amiga-games.zip",
        member="DISK2.ADF",
        format="adf",
        size=901120,
        md5="e" * 32,
        volume_label="GamesDisk2",
        listing=("c", "s", "Menu", "Game"),
        display_name="DISK2.ADF",
        virus="Byte Bandit",
    ),
    LocalFile(
        path=f"{LIBRARY}/Unsorted/copy of disk.msa",
        format="msa",
        size=398112,
        md5="d" * 32,
        display_name="copy of disk.msa",
    ),
)

CREWS = {
    "Automation": CrewInfo(
        "Automation",
        notes=(
            "Automation was an Atari ST cracking crew from the United Kingdom. Its numbered "
            "compact disks each put several cracked games behind one menu."
        ),
        source=SYNTHETIC,
        url="https://example.invalid/crews/automation",
    ),
    "Pompey Pirates": CrewInfo(
        "Pompey Pirates",
        notes=(
            "The Pompey Pirates were an Atari ST crew from Portsmouth, England, known for "
            "their numbered menu disks."
        ),
        source=SYNTHETIC,
        url="https://example.invalid/crews/pompey-pirates",
    ),
    "Medway Boys": CrewInfo(
        "Medway Boys",
        notes="The Medway Boys were an Atari ST crew from Kent, England.",
        source=SYNTHETIC,
        url="https://example.invalid/crews/medway-boys",
    ),
    "Skid Row": CrewInfo(
        "Skid Row",
        notes="Skid Row was a cracking group on the Amiga, known for its compact disks.",
        source=SYNTHETIC,
        url="https://example.invalid/crews/skid-row",
    ),
}

# Background for a few titles, in the shape of Wikipedia summaries.
SUMMARIES = {
    "Rick Dangerous": (
        "Rick Dangerous is a platform game by Core Design, published in 1989 for the Amiga, "
        "Atari ST and other home computers."
    ),
    "Dungeon Master": (
        "Dungeon Master is a real-time role-playing game by FTL Games, first released for "
        "the Atari ST in 1987."
    ),
    "Lemmings": "Lemmings is a puzzle game by DMA Design, first released for the Amiga in 1991.",
    "Speedball 2": (
        "Speedball 2: Brutal Deluxe is a sports game by The Bitmap Brothers, released in 1990."
    ),
}

DISC_TRIVIA = {
    4: (("fact", "The menu plays a chip tune and each game is started with a function key."),),
    13: (
        ("fact", "The first disc of the series; the menu shows a pirate ship over the scroller."),
        # A menu screen as a source typed it, which needs a fixed-width font.
        (
            "note",
            "   THE POMPEY PIRATES\n   ------------------\n"
            "   F1 ...... GAME ONE\n   F2 ...... GAME TWO\n   F3 ...... DESKTOP",
        ),
    ),
    1: (("note", "The crew's scroller greets other groups and lists the games on the disc."),),
}
# Scroll text captured from a menu, shown in the details pane in a fixed-width font.
MENU_TEXTS = {
    1: (
        "WELCOME TO AUTOMATION MENU 250 ...     NECRON AND BOULDERDASH CONSTRUCTION KIT "
        "PACKED AND TRAINED BY THE CREW ...     GREETINGS TO THE POMPEY PIRATES, THE MEDWAY "
        "BOYS AND EVERYONE WHO SENT US DISKS ...     LET'S WRAP ....     "
    ),
}
TITLE_TRIVIA = {
    "Rick Dangerous": ("note", "This copy starts with a trainer menu for infinite lives."),
    "Xenon 2 Megablast": ("fact", "The music plays through the Atari ST sound chip on this menu."),
}


def synthetic_png(seed: int, kind: str, width: int = 320, height: int = 200) -> bytes:
    """A small picture in the style of a menu or game screen, drawn from nothing."""
    rnd = random.Random(seed)
    hue = rnd.random()

    def colour(h: float, lightness: float, saturation: float = 0.8) -> bytes:
        r, g, b = colorsys.hls_to_rgb(h % 1.0, lightness, saturation)
        return bytes((int(r * 255), int(g * 255), int(b * 255)))

    black = b"\x00\x00\x00"
    rows: list[bytes] = []
    logo_top, logo_bottom = height // 5, height // 5 + 44
    blocks = [rnd.randrange(2) for _ in range(12 * 5)]
    for y in range(height):
        if kind in ("menu", "intro", "demo"):
            bar = y if y < 22 else y - (height - 22)
            if 0 <= bar < 22:
                # Copper bars at the top and bottom, light in the middle of each.
                row = colour(hue + bar / 60, 0.15 + 0.6 * (1 - abs(bar - 11) / 11)) * width
            elif logo_top <= y < logo_bottom:
                cell_y = (y - logo_top) * 5 // (logo_bottom - logo_top)
                row = b"".join(
                    (
                        colour(hue + 0.5, 0.55)
                        if 40 <= x < width - 40
                        and blocks[cell_y * 12 + (x - 40) * 12 // (width - 80)]
                        else black
                    )
                    for x in range(width)
                )
            elif height // 2 < y < height - 34 and y % 12 < 7:
                row = b"".join(
                    colour(hue + 0.15, 0.7) if (x // 6 + y // 12) % 3 else black
                    for x in range(48, width - 48)
                )
                row = black * 48 + row + black * 48
            else:
                row = black * width
        else:
            horizon = height * 2 // 3
            if y < horizon:
                row = colour(hue, 0.15 + 0.45 * y / horizon, 0.6) * width
            else:
                row = colour(hue + 0.3, 0.3 + 0.2 * ((y // 6) % 2), 0.5) * width
            if horizon - 30 <= y < horizon:
                start = 60 + (seed % 120)
                row = row[: start * 3] + colour(hue + 0.6, 0.6) * 24 + row[(start + 24) * 3 :]
        rows.append(b"\x00" + row)

    def chunk(tag: bytes, data: bytes) -> bytes:
        crc = zlib.crc32(tag + data) & 0xFFFFFFFF
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", crc)

    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(b"".join(rows), 6))
        + chunk(b"IEND", b"")
    )


def _media(disk: Disk) -> tuple[MediaItem, ...]:
    if disk.id == 3:
        return ()  # a disc with no pictures, for the placeholder
    return synthetic_media(disk, _contents(disk.id))


def synthetic_media(disk: Disk, contents: Sequence[Content]) -> tuple[MediaItem, ...]:
    """Invented pictures for a disc and its games and demos, drawn by ``media_file``."""
    base = f"https://example.invalid/media/{disk.id}"
    kind = "demo" if disk.kind == DiskKind.PACK else "menu"
    items = [
        MediaItem(
            kind,
            f"{base}/disc.png",
            SYNTHETIC,
            page_url=f"https://example.invalid/discs/{disk.id}",
            width=320,
            height=200,
        )
    ]
    for content in contents:
        if content.kind not in (G, ContentKind.DEMO):
            continue
        items.append(
            MediaItem(
                "snap",
                f"{base}/{content.id}.png",
                SYNTHETIC,
                page_url=f"https://example.invalid/titles/{content.id}",
                width=320,
                height=200,
                content_id=content.id,
            )
        )
        if content.title in SUMMARIES:
            items.append(
                MediaItem(
                    "title",
                    f"{base}/{content.id}-title.png",
                    SYNTHETIC,
                    width=320,
                    height=200,
                    content_id=content.id,
                )
            )
    return tuple(items)


def _trivia(disk: Disk) -> tuple[TriviaItem, ...]:
    items = []
    for kind, text in DISC_TRIVIA.get(disk.id, ()):
        items.append(
            TriviaItem(kind, text, SYNTHETIC, url=f"https://example.invalid/discs/{disk.id}")
        )
    for content in _contents(disk.id):
        if content.title in TITLE_TRIVIA:
            kind, text = TITLE_TRIVIA[content.title]
            items.append(TriviaItem(kind, text, SYNTHETIC, content_id=content.id))
    return tuple(items)


def _words(*texts: str) -> list[str]:
    return [word for text in texts for word in re.findall(r"\w+", (text or "").lower())]


def _matches(words: Sequence[str], tokens: Sequence[str]) -> bool:
    return all(any(token.startswith(word) for token in tokens) for word in words)


def _disc_order(disk: Disk) -> tuple:
    return (disk.series_name or disk.label, disk.number or 0, disk.part, disk.version, disk.id)


@dataclass
class FakeScript:
    """How the fake write session behaves, set by tests."""

    track_delay: float = 0.0  # seconds per track side written
    tracks: int = 160
    progress_every: int = 8  # report progress every N track sides
    fail_labels: set[str] = field(default_factory=set)
    protect_once: set[str] = field(default_factory=set)  # write-protected on first try
    download_steps: int = 5


class FakeWriteSession:
    """A write session that pretends to write, with the real event sequence."""

    def __init__(self, backend: FakeBackend, items: Sequence[QueueItem], events) -> None:
        self._backend = backend
        self._items = list(items)
        self._events = events
        self._cancelled = threading.Event()

    def cancel(self) -> None:
        self._cancelled.set()

    def run(self) -> SessionSummary:
        """The same order of events as ``jobs.session.WriteSession``; ``index`` counts from 0."""
        script = self._backend.script
        settings = self._backend.settings
        started = datetime.now().astimezone().isoformat(timespec="seconds")
        results: list[tuple[str, WriteOutcome, str]] = []
        total = len(self._items)
        stopped = False
        prompted = False
        protected = set(script.protect_once)
        for index, item in enumerate(self._items):
            item.notes = []
            if stopped or self._cancelled.is_set():
                outcome = WriteOutcome(
                    WriteStatus.SKIPPED, "The session was stopped before this disk."
                )
                self._finish(item, outcome, results, "")
                continue
            self._events.on_stage(item, index, total, "resolve", f"Looking for {item.label}")
            source = self._source(item)
            if not source:
                outcome = WriteOutcome(
                    WriteStatus.UNAVAILABLE,
                    "No image of this disk is in the library and no enabled provider has one.",
                )
                self._finish(item, outcome, results, "")
                continue
            if source.startswith("Downloaded"):
                self._events.on_stage(item, index, total, "download", f"Downloading {item.label}")
                for step in range(script.download_steps + 1):
                    self._events.on_download(item, step * 100_000, script.download_steps * 100_000)
                    time.sleep(script.track_delay)
            self._events.on_stage(item, index, total, "prepare", f"Preparing {item.label}")
            item.source_used = source
            item.notes = (
                ["Unpacked the MSA archive to a plain sector image."]
                if "msa" in source.lower()
                else []
            )
            for label in copy_labels(item):
                reason = ""
                while True:
                    if reason or settings.prompt_between_disks or not prompted:
                        self._events.on_stage(
                            item,
                            index,
                            total,
                            "insert",
                            reason or f"Insert a disk for {item.label}",
                        )
                        answer = self._events.ask_insert(item, index, total, reason)
                        if self._cancelled.is_set():
                            outcome = WriteOutcome(
                                WriteStatus.CANCELLED, "The session was cancelled."
                            )
                            break
                        if answer == "skip":
                            outcome = WriteOutcome(
                                WriteStatus.SKIPPED, "Skipped at the insert-disk prompt."
                            )
                            break
                        if answer != "write":
                            stopped = True
                            outcome = WriteOutcome(
                                WriteStatus.SKIPPED,
                                "The session was stopped at the insert-disk prompt.",
                            )
                            break
                    prompted = True
                    if item.label in protected:
                        protected.discard(item.label)
                        reason = "The disk is write-protected. Slide the tab to cover the hole, or insert another disk."
                        continue
                    outcome = self._write(item, index, total, script)
                    break
                self._finish(item, outcome, results, source, label)
                if stopped or self._cancelled.is_set():
                    break
        finished = datetime.now().astimezone().isoformat(timespec="seconds")
        summary = SessionSummary(started, finished, settings.drive, tuple(results))
        self._backend.sessions.insert(0, summary)
        return summary

    def _write(self, item, index, total, script: FakeScript) -> WriteOutcome:
        self._events.on_stage(item, index, total, "write", f"Writing {item.label}")
        retries = 0
        for number in range(1, script.tracks + 1):
            if self._cancelled.is_set():
                return WriteOutcome(WriteStatus.CANCELLED, "Cancelled while writing.")
            if number % script.progress_every == 0 or number == script.tracks:
                cylinder, head = divmod(number - 1, 2)
                retry = 1 if number == 72 else 0
                retries += retry
                self._events.on_write_progress(
                    item,
                    WriteProgress(
                        number / script.tracks,
                        cylinder,
                        head,
                        number,
                        script.tracks,
                        retry,
                        f"Writing track {cylinder}.{head}",
                    ),
                )
            time.sleep(script.track_delay)
        if item.label in script.fail_labels:
            return WriteOutcome(
                WriteStatus.FAILED,
                "Track 34.1 did not verify after 3 retries.",
                diagnostic="T34.1: Verify failure: 9 of 10 sectors",
                retries=3,
                failed_tracks=("34.1",),
            )
        return WriteOutcome(WriteStatus.VERIFIED, "Every track verified.", retries=retries)

    def _source(self, item: QueueItem) -> str:
        if item.local is not None:
            return f"Local file {item.local.path}"
        if item.disk_id in self._backend.local_disks:
            return f"Local file {self._backend.local_disks[item.disk_id]}"
        provider = ONLINE_DISKS.get(item.disk_id or 0, "")
        if provider:
            return f"Downloaded from {dict(PROVIDERS)[provider]}"
        return ""

    def _finish(self, item, outcome, results, source, label: str = "") -> None:
        outcome = with_notes(outcome, item.notes)
        item.outcome = outcome
        results.append((label or item.label, outcome, source))
        self._events.on_item_finished(item, outcome)


class FakeBackend(Backend):
    """An in-memory backend. Every public attribute can be changed by tests."""

    def __init__(self, settings: Settings | None = None, *, catalogue: bool = True) -> None:
        super().__init__(settings or Settings(path=None))
        self.has_catalogue = catalogue
        self.built_at = "2026-09-01"
        self.device = DeviceStatus(
            True, "Greaseweazle F7 on /dev/ttyACM0", "F7", "1.6", "/dev/ttyACM0", HOST_TOOLS
        )
        self.present = True  # what the file-only check reports
        self.probes = 0  # how many times gw info would have run
        self.script = FakeScript()
        self.queue: list[QueueItem] = []
        self.sessions: list[SessionSummary] = []
        self.local_disks: dict[int, str] = {
            disk_id: f"{LIBRARY}/{name}" for disk_id, name in LOCAL_DISKS.items()
        }
        self.update_offer: UpdateOffer | None = None
        self.update_error = ""  # when set, the update check fails with this reason
        self.download_notes: list[str] = []  # what Download Only reports with its file
        # The application update: an installed Ubuntu package, the newest release
        # (None for up to date), and the sentences the check or install fail with.
        self.app_target: PackageTarget | None = PackageTarget("ubuntu-24.04", "amd64")
        self.app_release: AppRelease | None = None
        self.app_check_error = ""
        self.app_install_error = ""
        self.app_install_dismissed = False
        self.app_checks = 0
        self.app_installed: list[Path] = []
        self.search_delay = 0.0
        self.probe_delay = 0.0
        self.media_delay = 0.0
        self.summary_delay = 0.0
        self.saved = 0
        self.last_scan = "2026-09-10T20:15:00"
        self.crews: dict[str, CrewInfo] = dict(CREWS)
        self.queries: list[Query] = []
        self.media_requests: list[str] = []
        self.summary_requests: list[tuple[int, int | None]] = []
        self.cleaned: list[LocalFile] = []
        self.corrections: dict[int, Correction] = {}  # Edit Details, by disc id
        self.brainfile = BrainfileStatus(False)
        # What the next boot block check finds; None while the virus data is unchanged.
        self.boot_recheck: BootRecheck | None = None
        self.boot_rechecks = 0  # how many times the window asked for one
        # IPF support on an invented 64-bit PC; install_caps never touches the network.
        self.caps = CapsStatus(CapsState.MISSING, machine="x86_64", build=CAPS_BUILD)
        self.caps_error = ""  # when set, install_caps fails with this sentence
        self._infected: dict[tuple[str, str], VirusReport] = {
            (f"{LIBRARY}/{LOCAL_DISKS[BOOT_VIRUS_DISK]}", ""): BOOT_VIRUS,
            (UNMATCHED[1].path, UNMATCHED[1].member): UNMATCHED_VIRUS,
        }
        # Library files whose boot block is not a virus: (path, member) -> report.
        self.boot_blocks: dict[tuple[str, str], VirusReport] = {
            (f"{LIBRARY}/{LOCAL_DISKS[BOOT_BLOCK_DISK]}", ""): IMMUNISER,
            (UNMATCHED[0].path, ""): TOS_LOADER,
        }
        self._media_folder: Path | None = None
        self._closed = False
        self._lock = threading.Lock()

    # Settings

    def save_settings(self) -> None:
        self.saved += 1
        if self.settings.path is not None:
            self.settings.save()

    # Catalogue

    def catalogue_info(self) -> CatalogueInfo:
        if not self.has_catalogue:
            return CatalogueInfo(False, error="No catalogue file was found.")
        return CatalogueInfo(
            True,
            built_at=self.built_at,
            path="/usr/share/piratefinder/catalogue.sqlite",
            stats={
                "disks": len(DISKS),
                "series": len(SERIES),
                "contents": sum(len(entries) for entries in _CONTENTS.values()),
                "images": sum(len(_images(disk)) for disk in DISKS),
            },
            series=SERIES,
            providers=PROVIDERS,
            sources=tuple({"id": key, "name": name} for key, name in SOURCES),
        )

    def _availability(self, disk_id: int) -> Availability:
        if disk_id in self.local_disks:
            return Availability.LOCAL
        provider = ONLINE_DISKS.get(disk_id)
        if provider and provider in self.settings.enabled_providers([provider]):
            return Availability.ONLINE
        return Availability.MISSING

    def set_infected(self, reports: dict[tuple[str, str], VirusReport]) -> None:
        """Make these library files, by (path, member), the only ones with a boot block virus."""
        with self._lock:
            self._infected = dict(reports)

    def _disc_virus(self, disk_id: int) -> VirusReport | None:
        if disk_id == FLAGGED_DISK:
            return FILE_VIRUS
        path = self.local_disks.get(disk_id)
        if path is not None:
            return self._infected.get((path, "")) or self.boot_blocks.get((path, ""))
        return None

    def _disc_tokens(self, disk: Disk) -> list[str]:
        names = [record.name for record in _images(disk)]
        people = [disk.cracker, disk.publisher]
        people += [c.cracker for c in _contents(disk.id)] + [
            c.publisher for c in _contents(disk.id)
        ]
        facets = [
            platform_name(disk.platform),
            disk.category,
            KIND_NAMES.get(disk.kind, ""),
            str(disk.year or ""),
        ]
        return _words(disk.label, disk.series_name, disk.crew, *people, *facets, *names, disk.notes)

    def search_page(self, query: Query) -> ResultPage:
        if self.search_delay:
            time.sleep(self.search_delay)
        self.queries.append(query)
        if not self.has_catalogue:
            return ResultPage(query, (), 0)
        words = _words(query.text)
        scored: list[tuple[float, tuple, ResultRow]] = []
        for catalogue_disk in DISKS:
            # The search sees the user's corrections, as the real one does.
            correction = self.corrections.get(catalogue_disk.id)
            disk = apply_disk(catalogue_disk, correction)
            if query.platform is not None and disk.platform != query.platform:
                continue
            if query.category and disk.category != query.category:
                continue
            if query.kinds and disk.kind not in query.kinds:
                continue
            if query.crew and disk.crew != query.crew:
                continue
            if query.year and disk.year != query.year:
                continue
            availability = self._availability(disk.id)
            if query.available_only and availability == Availability.MISSING:
                continue
            disc_tokens = self._disc_tokens(disk)
            contents = apply_contents(_contents(disk.id), correction)
            virus = self._disc_virus(disk.id)
            virus_name = virus.name if virus is not None and virus.infected else ""
            titles = [content.title for content in contents]
            if query.mode == ResultMode.DISCS:
                title_tokens = _words(*titles)
                if not _matches(words, disc_tokens + title_tokens):
                    continue
                matched = tuple(t for t in titles if words and _any_word(words, _words(t)))
                score = sum(2 for word in words if _any_word([word], _words(disk.label)))
                score += len(matched)
                row = ResultRow(
                    disk,
                    availability,
                    summary=", ".join(titles),
                    matched=matched,
                    virus=virus_name,
                )
                scored.append((score, _disc_order(disk), row))
                continue
            for content in contents or (None,):
                title = content.title if content else disk.label
                title_tokens = _words(title)
                if not _matches(words, disc_tokens + title_tokens):
                    continue
                score = sum(2 for word in words if _any_word([word], title_tokens))
                row = ResultRow(
                    disk,
                    availability,
                    title=title,
                    content_id=content.id if content else None,
                    content_kind=content.kind if content else None,
                    matched=(title,) if score else (),
                    virus=virus_name,
                )
                scored.append(
                    (score, (*_disc_order(disk), content.position if content else 0), row)
                )
        rows = self._sorted(scored, query)
        start = query.page * query.page_size
        return ResultPage(query, tuple(rows[start : start + query.page_size]), len(rows))

    @staticmethod
    def _sorted(scored, query: Query) -> list[ResultRow]:
        def title(row: ResultRow) -> str:
            return (row.title or row.disk.label).casefold()

        orders = {
            SortOrder.TITLE: lambda item: (title(item[2]), item[1]),
            SortOrder.YEAR: lambda item: (
                item[2].disk.year is None,
                item[2].disk.year or 0,
                item[2].disk.month or 0,
                item[1],
            ),
            SortOrder.DISC: lambda item: item[1],
            SortOrder.CREW: lambda item: (item[2].disk.crew.casefold(), item[1]),
            SortOrder.PLATFORM: lambda item: (platform_name(item[2].disk.platform), item[1]),
        }
        if query.sort == SortOrder.TITLE_DESC:
            scored.sort(key=orders[SortOrder.TITLE], reverse=True)
        elif query.sort == SortOrder.YEAR_DESC:
            scored.sort(key=lambda item: item[1])
            scored.sort(
                key=lambda item: (item[2].disk.year or 0, item[2].disk.month or 0), reverse=True
            )
            scored.sort(key=lambda item: item[2].disk.year is None)
        elif query.sort in orders:
            scored.sort(key=orders[query.sort])
        else:
            scored.sort(key=lambda item: (-item[0], item[1]))
        return [row for _score, _order, row in scored]

    def facets(self) -> Facets:
        if not self.has_catalogue:
            return Facets()
        disks = [apply_disk(disk, self.corrections.get(disk.id)) for disk in DISKS]
        crews = Counter(disk.crew for disk in disks if disk.crew)
        years = Counter(disk.year for disk in disks if disk.year)
        categories = Counter(disk.category for disk in DISKS if disk.category)
        return Facets(
            crews=tuple(sorted(crews.items(), key=lambda item: item[0].casefold())),
            years=tuple(sorted(years.items())),
            categories=tuple(sorted(categories.items(), key=lambda item: -item[1])),
        )

    def detail(self, disk_id: int) -> DiskDetail:
        disk = DISKS_BY_ID.get(disk_id)
        if disk is None:
            raise LookupError(f"Disk {disk_id} is not in the catalogue.")
        disk = self._catalogue_disk(disk)
        original, contents = disk, _contents(disk_id)
        correction = self.corrections.get(disk_id, Correction())
        disk = apply_disk(disk, correction)
        images = _images(disk)
        locations: tuple[Location, ...] = ()
        provider = ONLINE_DISKS.get(disk_id)
        if provider:
            locations = (
                Location(
                    id=disk_id,
                    disk_id=disk_id,
                    provider=provider,
                    url=f"https://example.invalid/{provider}/{disk_id}.zip",
                    image_id=images[0].id,
                    container="zip",
                    hash_kind="md5",
                    hash_value=images[0].md5,
                ),
            )
        local_files: tuple[LocalFile, ...] = ()
        virus = self._disc_virus(disk_id)
        if disk_id in self.local_disks:
            path = self.local_disks[disk_id]
            local_files = (
                LocalFile(
                    path=path,
                    format=Path(path).suffix.lstrip("."),
                    size=images[0].size or 0,
                    md5=images[0].md5,
                    image_id=images[0].id,
                    disk_id=disk_id,
                    virus=virus.name if virus is not None and virus.kind == "boot" else "",
                ),
            )
        links: tuple[Link, ...] = ()
        if disk.platform == ST and disk.kind == DiskKind.MENU:
            links = (Link(disk_id, "Atari Legend", f"https://example.invalid/menus/{disk_id}"),)
        return DiskDetail(
            disk,
            apply_contents(contents, correction),
            images,
            locations,
            links,
            local_files,
            self._availability(disk_id),
            media=_media(disk),
            trivia=_trivia(disk),
            crew=self.crews.get(disk.crew),
            virus=virus,
            write_local=local_files[0] if local_files else None,
            edited=tuple(name for name in EDITABLE_FIELDS if name in correction.fields),
            original=original if correction else None,
            edited_titles=tuple(
                (content_id, old)
                for content_id, (old, _new) in corrected_titles(contents, correction).items()
            ),
        )

    @staticmethod
    def _catalogue_disk(disk: Disk) -> Disk:
        """A disc with the details only its details pane shows."""
        notes, credits = NOTES.get(disk.id, ("", ""))
        return replace(
            disk,
            condition=CONDITIONS.get(disk.id, ""),
            notes=notes,
            credits=credits,
            menu_text=MENU_TEXTS.get(disk.id, ""),
        )

    def save_details(
        self, disk_id: int, values: dict[str, str], titles: dict[int, str] | None = None
    ) -> None:
        disk = DISKS_BY_ID.get(disk_id)
        if disk is None:
            raise LookupError(f"Disk {disk_id} is not in the catalogue.")
        correction = correction_from_form(
            self._catalogue_disk(disk), _contents(disk_id), values, titles
        )
        with self._lock:
            if correction:
                self.corrections[disk_id] = correction
            else:
                self.corrections.pop(disk_id, None)

    def revert_details(self, disk_id: int) -> None:
        with self._lock:
            self.corrections.pop(disk_id, None)

    def summaries(self, disk_id: int, content_id: int | None = None) -> list[TriviaItem]:
        self.summary_requests.append((disk_id, content_id))
        if self.summary_delay:
            time.sleep(self.summary_delay)
        if not self.media_enabled:
            return []
        content = next((c for c in _contents(disk_id) if c.id == content_id), None)
        if content is None or content.title not in SUMMARIES:
            return []
        article = content.title
        return [
            TriviaItem(
                "summary",
                SUMMARIES[article],
                "wikipedia",
                url=f"https://en.wikipedia.org/wiki/{article.replace(' ', '_')}",
                licence="CC BY-SA 4.0",
                title=article,
                content_id=content_id,
            )
        ]

    def media_file(self, item: MediaItem) -> Path | None:
        self.media_requests.append(item.url)
        if self.media_delay:
            time.sleep(self.media_delay)
        if not self.media_enabled:
            return None
        with self._lock:
            if self._closed:
                return None
            if self._media_folder is None:
                self._media_folder = Path(tempfile.mkdtemp(prefix="piratefinder-fake-media-"))
            folder = self._media_folder
        seed = zlib.crc32(item.url.encode("utf-8"))
        path = folder / f"{seed:08x}.png"
        if not path.exists():
            data = synthetic_png(seed, item.kind, item.width or 320, item.height or 200)
            with self._lock:
                if self._closed:
                    return None
                partial = path.with_name(f"{path.name}.{threading.get_ident()}.part")
                partial.write_bytes(data)
                partial.replace(path)
        return path

    def clean_alternates(self, disk_id: int) -> list[ImageRecord]:
        """Every good dump of the disc the catalogue does not flag, best first, as the
        real finder answers; the dump being written may be among them."""
        disk = DISKS_BY_ID.get(disk_id)
        if disk is None:
            return []
        return [image for image in _images(disk) if not image.virus and not image.bad]

    def check_for_update(self) -> UpdateOffer | None:
        if self.update_error:
            raise RuntimeError(self.update_error)
        return self.update_offer

    def install_update(self, offer: UpdateOffer, progress: ProgressCallback, cancel) -> None:
        for step in range(11):
            if getattr(cancel, "cancelled", False):
                raise RuntimeError("The update was cancelled.")
            progress(step * 1000, 10_000)
            time.sleep(self.script.track_delay)
        self.built_at = offer.built_at
        self.update_offer = None

    # Application updates

    def app_update_target(self) -> PackageTarget | None:
        return self.app_target

    def check_app_update(self) -> AppRelease | None:
        self.app_checks += 1
        if self.app_check_error:
            raise UpdateError(self.app_check_error)
        return self.app_release

    def download_app_update(self, release: AppRelease, progress: ProgressCallback, cancel) -> Path:
        for step in range(11):
            if getattr(cancel, "cancelled", False):
                raise UpdateCancelled("The update was cancelled.")
            progress(step * 1000, 10_000)
            time.sleep(self.script.track_delay)
        return Path(tempfile.gettempdir()) / release.package_name

    def install_app_update(self, package: Path) -> None:
        if self.app_install_dismissed:
            raise UpdateCancelled("The password prompt was dismissed, so nothing was installed.")
        if self.app_install_error:
            raise UpdateError(self.app_install_error)
        self.app_installed.append(package)

    # Library

    def library_stats(self) -> LibraryStats:
        local = len(self.local_disks)
        return LibraryStats(
            images=local + len(UNMATCHED) + 1,
            matched=local,
            unmatched=len(UNMATCHED),
            duplicates=1,
            last_scan=self.last_scan,
            infected=len(self._infected),
        )

    def unmatched_files(self, limit: int = 200, text: str = "") -> list[LocalFile]:
        words = _words(text)
        found = []
        for local in UNMATCHED:
            local = self._current(local)
            tokens = _words(local.display_name, local.volume_label, *local.listing)
            if _matches(words, tokens):
                found.append(local)
        return found[:limit]

    def _current(self, local: LocalFile) -> LocalFile:
        infected = (local.path, local.member) in self._infected
        return local if infected or not local.virus else replace(local, virus="")

    def infected_files(self, limit: int = 200) -> list[LocalFile]:
        files = []
        for (path, member), report in self._infected.items():
            unmatched = next((f for f in UNMATCHED if (f.path, f.member) == (path, member)), None)
            if unmatched is not None:
                files.append(unmatched)
                continue
            disk_id = next((d for d, p in self.local_disks.items() if p == path), None)
            files.append(
                LocalFile(
                    path=path,
                    member=member,
                    format=Path(member or path).suffix.lstrip("."),
                    disk_id=disk_id,
                    image_id=disk_id * 10 if disk_id else None,
                    virus=report.name,
                    display_name=Path(member or path).name,
                )
            )
        return files[:limit]

    def virus_report(self, local: LocalFile) -> VirusReport | None:
        key = (local.path, local.member)
        report = self._infected.get(key) or self.boot_blocks.get(key)
        return report or VirusReport(VirusStatus.CLEAN)

    def clean_file(self, local: LocalFile) -> LocalFile:
        report = self._infected.get((local.path, local.member))
        if report is None:
            raise RuntimeError("No virus was found on this file.")
        if not report.removable:
            raise RuntimeError("This virus cannot be removed.")
        del self._infected[(local.path, local.member)]
        cleaned = replace(local, virus="")
        self.cleaned.append(cleaned)
        return cleaned

    def brainfile_status(self) -> BrainfileStatus:
        return self.brainfile

    def install_brainfile(self, progress: ProgressCallback, cancel) -> BrainfileStatus:
        for step in range(6):
            if getattr(cancel, "cancelled", False):
                raise RuntimeError("The download was cancelled.")
            progress(step * 20_000, 100_000)
            time.sleep(self.script.track_delay)
        self.brainfile = BrainfileStatus(
            True, version="1.0", entries=120, path="/home/user/.local/share/piratefinder/brainfile"
        )
        # A new brainfile is new virus data: the library's boot blocks are checked again.
        self.boot_recheck = BootRecheck(checked=len(self.local_disks))
        return self.brainfile

    def recheck_boot_blocks(
        self, progress: Callable[[ScanProgress], None], cancel
    ) -> BootRecheck | None:
        with self._lock:
            self.boot_rechecks += 1
            result, self.boot_recheck = self.boot_recheck, None
        if result is not None:
            for done in range(result.checked):
                if getattr(cancel, "cancelled", False):
                    self.boot_recheck = result
                    return replace(result, checked=done, cancelled=True)
                progress(
                    ScanProgress(
                        done + 1, result.checked, f"Checking the boot block of file{done}.st"
                    )
                )
                time.sleep(self.script.track_delay)
        return result

    def scan_library(self, progress: Callable[[ScanProgress], None], cancel) -> ScanSummary:
        total = 40
        for done in range(total + 1):
            if getattr(cancel, "cancelled", False):
                return ScanSummary(
                    tuple(self.settings.library_folders), done, done, 0, 0, 0, 0, cancelled=True
                )
            progress(ScanProgress(done, total, f"Reading file{done}.st"))
            time.sleep(self.script.track_delay)
        self.last_scan = datetime.now().isoformat(timespec="seconds")
        stats = self.library_stats()
        return ScanSummary(
            tuple(self.settings.library_folders),
            files_seen=total,
            images_found=stats.images,
            matched=stats.matched,
            unmatched=stats.unmatched,
            new=0,
            removed=0,
            seconds=0.5,
        )

    def download(self, item: QueueItem, progress: ProgressCallback, cancel) -> Downloaded:
        if item.disk_id not in ONLINE_DISKS:
            raise RuntimeError("No enabled provider has an image of this disk.")
        steps = self.script.download_steps
        for step in range(steps + 1):
            if getattr(cancel, "cancelled", False):
                raise RuntimeError("The download was cancelled.")
            progress(step * 100_000, steps * 100_000)
            time.sleep(self.script.track_delay)
        path = f"{self.settings.download_folder}/{item.label}.st"
        self.local_disks[item.disk_id] = path
        return Downloaded(path, tuple(self.download_notes))

    # Queue

    def queue_items(self) -> list[QueueItem]:
        with self._lock:
            return list(self.queue)

    def queue_add(self, items: Sequence[QueueItem]) -> None:
        with self._lock:
            keys = {queue_key(item) for item in self.queue}
            for item in items:
                if queue_key(item) not in keys:
                    keys.add(queue_key(item))
                    self.queue.append(item)

    def queue_remove(self, item_ids: Sequence[str]) -> None:
        with self._lock:
            self.queue = [item for item in self.queue if item.id not in set(item_ids)]

    def queue_move(self, item_id: str, offset: int) -> None:
        with self._lock:
            ids = [item.id for item in self.queue]
            if item_id not in ids:
                return
            index = ids.index(item_id)
            target = max(0, min(len(self.queue) - 1, index + offset))
            item = self.queue.pop(index)
            self.queue.insert(target, item)

    def queue_set_copies(self, item_id: str, copies: int) -> None:
        with self._lock:
            self.queue = [
                replace(item, copies=clamp_copies(copies)) if item.id == item_id else item
                for item in self.queue
            ]

    def queue_clear(self) -> None:
        with self._lock:
            self.queue.clear()

    # Writing

    def device_present(self) -> bool:
        return self.present

    def probe(self) -> DeviceStatus:
        self.probes += 1
        if self.probe_delay:
            time.sleep(self.probe_delay)
        return self.device

    def caps_status(self) -> CapsStatus:
        return self.caps

    def install_caps(self, progress: ProgressCallback, cancel) -> CapsStatus:
        for step in range(6):
            if getattr(cancel, "cancelled", False):
                raise RuntimeError("The download was cancelled.")
            progress(step * CAPS_BUILD.size // 5, CAPS_BUILD.size)
            time.sleep(self.script.track_delay)
        if self.caps_error:
            raise RuntimeError(self.caps_error)
        self.caps = replace(
            self.caps,
            state=CapsState.INSTALLED,
            version=CAPS_BUILD.version,
            path="/home/user/.local/share/piratefinder/caps/libcapsimage.so.5",
        )
        return self.caps

    def remove_caps(self) -> CapsStatus:
        self.caps = replace(self.caps, state=CapsState.MISSING, version="", path="")
        return self.caps

    def create_session(self, items: Sequence[QueueItem], events) -> FakeWriteSession:
        return FakeWriteSession(self, items, events)

    def history(self) -> list[SessionSummary]:
        return list(self.sessions)

    def close(self) -> None:
        with self._lock:
            self._closed = True
            if self._media_folder is not None:
                shutil.rmtree(self._media_folder, ignore_errors=True)
                self._media_folder = None


def _any_word(words: Sequence[str], tokens: Sequence[str]) -> bool:
    return any(token.startswith(word) for word in words for token in tokens)

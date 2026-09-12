"""A backend with a handful of synthetic disks, for tests and screenshots.

Nothing here reads the catalogue, the user database, the network or a
Greaseweazle. The disks are invented examples in the shape of real catalogue
entries, so the window can be driven and photographed on any machine. Run
the application with ``PIRATEFINDER_FAKE_BACKEND=1`` to try it by hand.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path

from ..models import (
    Availability,
    Content,
    ContentKind,
    DeviceStatus,
    Disk,
    DiskDetail,
    DiskKind,
    ImageRecord,
    Link,
    LocalFile,
    Location,
    Platform,
    QueueItem,
    ScanSummary,
    SearchFilters,
    SearchResult,
    Series,
    SessionSummary,
    WriteOutcome,
    WriteProgress,
    WriteStatus,
)
from ..settings import Settings
from .backend import (
    Backend,
    CatalogueInfo,
    LibraryStats,
    ProgressCallback,
    ScanProgress,
    UpdateOffer,
)
from .formatting import queue_key

ST = Platform.ATARI_ST
AMIGA = Platform.AMIGA
G = ContentKind.GAME

LIBRARY = "/home/user/Floppy Images"

SERIES = (
    Series("automation", "Automation", ST, DiskKind.MENU, "Automation", ("auto",), 3),
    Series("pompey", "Pompey Pirates", ST, DiskKind.MENU, "Pompey Pirates", ("pp",), 2),
    Series("medway", "Medway Boys", ST, DiskKind.MENU, "Medway Boys", ("mb",), 1),
    Series("dbug", "D-Bug", ST, DiskKind.MENU, "D-Bug", (), 1),
    Series("skidrow-compact", "Skid Row Compact", AMIGA, DiskKind.MENU, "Skid Row", ("src",), 2),
    Series("demo-pack", "Demo Pack", AMIGA, DiskKind.PACK, "", (), 1),
)

PROVIDERS = (
    ("internet-archive", "Internet Archive"),
    ("atari-legend", "Atari Legend"),
    ("d-bug", "D-Bug"),
)


def _disk(disk_id, label, platform, kind, series=None, number=None, **extra) -> Disk:
    names = {item.id: item.name for item in SERIES}
    return Disk(
        id=disk_id,
        label=label,
        platform=platform,
        kind=kind,
        series_id=series,
        series_name=names.get(series, ""),
        number=number,
        **extra,
    )


DISKS = (
    _disk(
        1,
        "Automation 250",
        ST,
        DiskKind.MENU,
        "automation",
        250,
        title="Automation 250 (1990)(Automation)",
        date="1990",
        condition="intact",
        notes="Menu with a scrolling message and chip music.",
        credits="Packed by the crew. Menu code and music are credited on the scroller.",
    ),
    _disk(
        2,
        "Automation 251",
        ST,
        DiskKind.MENU,
        "automation",
        251,
        title="Automation 251 (1990)(Automation)",
        date="1990",
        condition="intact",
    ),
    _disk(
        3,
        "Automation 252",
        ST,
        DiskKind.MENU,
        "automation",
        252,
        title="Automation 252 (1990)(Automation)",
        date="1990",
        condition="damaged",
        notes="The second game does not load from this dump.",
    ),
    _disk(
        4,
        "Pompey Pirates 51",
        ST,
        DiskKind.MENU,
        "pompey",
        51,
        title="Pompey Pirates 51 (1991)(Pompey Pirates)",
        date="1991",
        condition="intact",
    ),
    _disk(
        5,
        "Pompey Pirates 52",
        ST,
        DiskKind.MENU,
        "pompey",
        52,
        title="Pompey Pirates 52 (1991)(Pompey Pirates)",
        date="1991",
    ),
    _disk(
        6,
        "Medway Boys 60",
        ST,
        DiskKind.MENU,
        "medway",
        60,
        title="Medway Boys 60 (1990)(Medway Boys)",
        date="1990",
    ),
    _disk(
        7,
        "D-Bug 100 B",
        ST,
        DiskKind.MENU,
        "dbug",
        100,
        part="B",
        title="D-Bug 100 B (1993)(D-Bug)",
        date="1993",
    ),
    _disk(
        8,
        "Skid Row Compact 12",
        AMIGA,
        DiskKind.MENU,
        "skidrow-compact",
        12,
        title="Skid Row Compact 12 (1991)(Skid Row)",
        date="1991",
    ),
    _disk(
        9,
        "Skid Row Compact 13",
        AMIGA,
        DiskKind.MENU,
        "skidrow-compact",
        13,
        title="Skid Row Compact 13 (1991)(Skid Row)",
        date="1991",
    ),
    _disk(
        10,
        "Demo Pack 41",
        AMIGA,
        DiskKind.PACK,
        "demo-pack",
        41,
        title="Demo Pack 41 (1992)",
        date="1992",
    ),
    _disk(
        11,
        "Speedball 2",
        AMIGA,
        DiskKind.SINGLE,
        title="Speedball 2 (1990)(Image Works)[cr Skid Row]",
        date="1990",
        publisher="Image Works",
        cracker="Skid Row",
    ),
    _disk(
        12,
        "Arcade Collection Disk 1",
        ST,
        DiskKind.COMPILATION,
        title="Arcade Collection (1991)(Compilation)(Disk 1 of 2)",
        date="1991",
        part="1 of 2",
    ),
)

CONTENTS = {
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
}


def _contents(disk_id: int) -> tuple[Content, ...]:
    return tuple(
        Content(disk_id, title, kind, position, extra=extra, source="synthetic")
        for position, (title, kind, extra) in enumerate(CONTENTS.get(disk_id, ()))
    )


def _images(disk: Disk) -> tuple[ImageRecord, ...]:
    extension = "adf" if disk.platform == AMIGA else "st"
    base = disk.title or disk.label
    records = [
        ImageRecord(
            id=disk.id * 10,
            disk_id=disk.id,
            name=f"{base}.{extension}",
            format=extension,
            size=901120 if extension == "adf" else 737280,
            md5=f"{disk.id:032x}",
            rank=0,
            source="tosec",
        )
    ]
    if disk.id in (1, 4, 8):
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


# Which disks are in the library, and which can be downloaded from where.
LOCAL_DISKS = {1: "Automation/Automation 250.st", 4: "Pompey/PP51.msa", 9: "Amiga/src13.zip"}
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
    ),
    LocalFile(
        path=f"{LIBRARY}/Unsorted/copy of disk.msa",
        format="msa",
        size=398112,
        md5="d" * 32,
        display_name="copy of disk.msa",
    ),
)


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
                ["Converted from MSA to ST for writing."] if "msa" in source.lower() else []
            )
            for copy in range(max(1, item.copies)):
                label = (
                    item.label
                    if item.copies <= 1
                    else f"{item.label} (copy {copy + 1} of {item.copies})"
                )
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
            True, "Greaseweazle F7 on /dev/ttyACM0", "F7", "1.6", "/dev/ttyACM0", "1.22"
        )
        self.script = FakeScript()
        self.queue: list[QueueItem] = []
        self.sessions: list[SessionSummary] = []
        self.local_disks: dict[int, str] = {
            disk_id: f"{LIBRARY}/{name}" for disk_id, name in LOCAL_DISKS.items()
        }
        self.update_offer: UpdateOffer | None = None
        self.search_delay = 0.0
        self.saved = 0
        self.last_scan = "2026-09-10T20:15:00"
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
                "contents": sum(len(entries) for entries in CONTENTS.values()),
                "images": sum(len(_images(disk)) for disk in DISKS),
            },
            series=SERIES,
            providers=PROVIDERS,
            sources=tuple(
                {"id": key, "name": name} for key, name in (*PROVIDERS, ("tosec", "TOSEC"))
            ),
        )

    def _availability(self, disk_id: int) -> Availability:
        if disk_id in self.local_disks:
            return Availability.LOCAL
        provider = ONLINE_DISKS.get(disk_id)
        if provider and provider in self.settings.enabled_providers([provider]):
            return Availability.ONLINE
        return Availability.MISSING

    def search(self, text: str, filters: SearchFilters) -> list[SearchResult]:
        if self.search_delay:
            time.sleep(self.search_delay)
        words = [word for word in text.lower().split() if word]
        if not words or not self.has_catalogue:
            return []
        results: list[SearchResult] = []
        for disk in DISKS:
            if filters.platform is not None and disk.platform != filters.platform:
                continue
            if filters.kinds and disk.kind not in filters.kinds:
                continue
            availability = self._availability(disk.id)
            if filters.available_only and availability == Availability.MISSING:
                continue
            titles = [content.title for content in _contents(disk.id)]
            haystack = " ".join([disk.label, disk.series_name, disk.cracker, *titles]).lower()
            if not all(word in haystack for word in words):
                continue
            matched = tuple(
                title for title in titles if any(word in title.lower() for word in words)
            )
            results.append(
                SearchResult(
                    availability,
                    disk=disk,
                    matched=matched,
                    summary=", ".join(titles),
                    score=float(len(matched)),
                )
            )
        if filters.platform is None and not filters.kinds:
            for local in UNMATCHED:
                haystack = " ".join([local.display_name, local.volume_label, *local.listing])
                if all(word in haystack.lower() for word in words):
                    results.append(
                        SearchResult(
                            Availability.LOCAL,
                            local=local,
                            summary=", ".join(local.listing),
                        )
                    )
        results.sort(key=lambda result: -result.score)
        return results

    def detail(self, disk_id: int) -> DiskDetail:
        disk = next(disk for disk in DISKS if disk.id == disk_id)
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
                ),
            )
        links = ()
        if disk.platform == ST and disk.kind == DiskKind.MENU:
            links = (Link(disk_id, "Atari Legend", f"https://example.invalid/menus/{disk_id}"),)
        return DiskDetail(
            disk,
            _contents(disk_id),
            images,
            locations,
            links,
            local_files,
            self._availability(disk_id),
        )

    def check_for_update(self) -> UpdateOffer | None:
        return self.update_offer

    def install_update(self, offer: UpdateOffer, progress: ProgressCallback, cancel) -> None:
        for step in range(11):
            if getattr(cancel, "cancelled", False):
                raise RuntimeError("The update was cancelled.")
            progress(step * 1000, 10_000)
            time.sleep(self.script.track_delay)
        self.built_at = offer.built_at
        self.update_offer = None

    # Library

    def library_stats(self) -> LibraryStats:
        local = len(self.local_disks)
        return LibraryStats(
            images=local + len(UNMATCHED) + 1,
            matched=local,
            unmatched=len(UNMATCHED),
            duplicates=1,
            last_scan=self.last_scan,
        )

    def unmatched_files(self, limit: int = 200) -> list[LocalFile]:
        return list(UNMATCHED[:limit])

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

    def download(self, item: QueueItem, progress: ProgressCallback, cancel) -> str:
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
        return path

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
                replace(item, copies=max(1, copies)) if item.id == item_id else item
                for item in self.queue
            ]

    def queue_clear(self) -> None:
        with self._lock:
            self.queue.clear()

    # Writing

    def probe(self) -> DeviceStatus:
        return self.device

    def create_session(self, items: Sequence[QueueItem], events) -> FakeWriteSession:
        return FakeWriteSession(self, items, events)

    def history(self) -> list[SessionSummary]:
        return list(self.sessions)

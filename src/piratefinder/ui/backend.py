"""Everything the window needs from the rest of the application, in one object.

The window never imports the catalogue, library, writer or job modules
itself. It talks to a ``Backend``: ``Backend.create()`` builds the real one
from those modules, and tests and screenshot runs pass a fake with a few
synthetic disks (``ui.fake_backend``). Calls that may block are made from
worker threads; a backend must not touch GTK.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from ..models import (
    DeviceStatus,
    DiskDetail,
    LocalFile,
    QueueItem,
    ScanSummary,
    SearchFilters,
    SearchResult,
    Series,
    SessionSummary,
)
from ..settings import Settings


@dataclass(frozen=True, slots=True)
class CatalogueInfo:
    """What the interface shows about the installed catalogue."""

    available: bool
    built_at: str = ""
    path: str = ""
    stats: dict[str, int] = field(default_factory=dict)
    series: tuple[Series, ...] = ()
    providers: tuple[tuple[str, str], ...] = ()  # (provider id, display name)
    sources: tuple[dict[str, str], ...] = ()
    error: str = ""  # why the catalogue could not be opened


@dataclass(frozen=True, slots=True)
class LibraryStats:
    images: int = 0
    matched: int = 0
    unmatched: int = 0
    duplicates: int = 0
    last_scan: str = ""


@dataclass(frozen=True, slots=True)
class ScanProgress:
    """One step of a library scan, normalised for the progress bar."""

    done: int
    total: int | None
    message: str = ""  # "Reading Automation 250.st"


@dataclass(frozen=True, slots=True)
class UpdateOffer:
    """A newer catalogue that can be installed."""

    built_at: str
    size: int | None = None
    notes: str = ""
    handle: Any = None  # whatever the update module needs to install it


class Session(Protocol):
    def run(self) -> SessionSummary: ...
    def cancel(self) -> None: ...


ProgressCallback = Callable[[int, int | None], None]


class Backend:
    """The interface's view of the application. Subclasses fill it in."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    @classmethod
    def create(cls) -> Backend:
        """Build the real backend: locate the catalogue, open the user database."""
        from .real_backend import RealBackend

        return RealBackend.open()

    # Settings

    def save_settings(self) -> None:
        self.settings.save()

    def settings_changed(self) -> None:
        """Called after the user changed a setting that affects the backend."""

    # Catalogue

    def catalogue_info(self) -> CatalogueInfo:
        raise NotImplementedError

    def search(self, text: str, filters: SearchFilters) -> list[SearchResult]:
        raise NotImplementedError

    def detail(self, disk_id: int) -> DiskDetail:
        raise NotImplementedError

    def check_for_update(self) -> UpdateOffer | None:
        """A newer catalogue, or None when the installed one is current."""
        raise NotImplementedError

    def install_update(self, offer: UpdateOffer, progress: ProgressCallback, cancel) -> None:
        """Download and install ``offer``, then reopen the catalogue."""
        raise NotImplementedError

    # Library

    def library_stats(self) -> LibraryStats:
        raise NotImplementedError

    def unmatched_files(self, limit: int = 200) -> list[LocalFile]:
        raise NotImplementedError

    def scan_library(self, progress: Callable[[ScanProgress], None], cancel) -> ScanSummary:
        raise NotImplementedError

    def download(self, item: QueueItem, progress: ProgressCallback, cancel) -> str:
        """Fetch the image for ``item`` into the download folder; return its path."""
        raise NotImplementedError

    # Queue

    def queue_items(self) -> list[QueueItem]:
        raise NotImplementedError

    def queue_add(self, items: Sequence[QueueItem]) -> None:
        raise NotImplementedError

    def queue_remove(self, item_ids: Sequence[str]) -> None:
        raise NotImplementedError

    def queue_move(self, item_id: str, offset: int) -> None:
        raise NotImplementedError

    def queue_set_copies(self, item_id: str, copies: int) -> None:
        raise NotImplementedError

    def queue_clear(self) -> None:
        raise NotImplementedError

    # Writing

    def probe(self) -> DeviceStatus:
        raise NotImplementedError

    def create_session(self, items: Sequence[QueueItem], events) -> Session:
        raise NotImplementedError

    def history(self) -> list[SessionSummary]:
        raise NotImplementedError

    def report_text(self, summary: SessionSummary) -> str:
        from .formatting import report_text

        return report_text(summary)

    def close(self) -> None:
        """Release database connections when the window closes."""


class UnavailableBackend(Backend):
    """Used when the real backend could not be built.

    The window still opens, shows why on the Find page and offers to download
    the catalogue. The queue lives in memory; nothing can be searched.
    """

    def __init__(self, error: str, settings: Settings | None = None) -> None:
        if settings is None:
            try:
                settings = Settings.load()
            except Exception:  # noqa: BLE001 - defaults are good enough here
                settings = Settings()
        super().__init__(settings)
        self.error = error
        self._queue: list[QueueItem] = []

    def catalogue_info(self) -> CatalogueInfo:
        return CatalogueInfo(False, error=self.error)

    def search(self, text: str, filters: SearchFilters) -> list[SearchResult]:
        return []

    def detail(self, disk_id: int) -> DiskDetail:
        raise LookupError("No catalogue is installed.")

    def check_for_update(self) -> UpdateOffer | None:
        from .real_backend import check_for_update

        return check_for_update(self.settings)

    def install_update(self, offer: UpdateOffer, progress: ProgressCallback, cancel) -> None:
        from .real_backend import install_update

        install_update(offer, progress, cancel)

    def library_stats(self) -> LibraryStats:
        return LibraryStats()

    def unmatched_files(self, limit: int = 200) -> list[LocalFile]:
        return []

    def scan_library(self, progress, cancel) -> ScanSummary:
        raise RuntimeError(f"The library cannot be scanned: {self.error}")

    def download(self, item: QueueItem, progress: ProgressCallback, cancel) -> str:
        raise RuntimeError(f"Nothing can be downloaded: {self.error}")

    def queue_items(self) -> list[QueueItem]:
        return list(self._queue)

    def queue_add(self, items: Sequence[QueueItem]) -> None:
        self._queue.extend(items)

    def queue_remove(self, item_ids: Sequence[str]) -> None:
        self._queue = [item for item in self._queue if item.id not in set(item_ids)]

    def queue_move(self, item_id: str, offset: int) -> None:
        ids = [item.id for item in self._queue]
        if item_id in ids:
            index = ids.index(item_id)
            item = self._queue.pop(index)
            self._queue.insert(max(0, min(len(self._queue), index + offset)), item)

    def queue_set_copies(self, item_id: str, copies: int) -> None:
        for item in self._queue:
            if item.id == item_id:
                item.copies = max(1, copies)

    def queue_clear(self) -> None:
        self._queue.clear()

    def probe(self) -> DeviceStatus:
        from .real_backend import probe_device

        return probe_device(self.settings)

    def create_session(self, items: Sequence[QueueItem], events) -> Session:
        raise RuntimeError(f"Disks cannot be written: {self.error}")

    def history(self) -> list[SessionSummary]:
        return []

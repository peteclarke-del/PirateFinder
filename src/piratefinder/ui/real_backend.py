"""The backend built from the real catalogue, user database, queue and writer.

This is the only interface module that imports the application's other
layers. When the catalogue is missing the backend still works for the
library, the queue and the history, and the window offers to download a
catalogue.
"""

from __future__ import annotations

import contextlib
import logging
import os
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from ..models import (
    DeviceStatus,
    DiskDetail,
    LocalFile,
    QueueItem,
    ScanSummary,
    SearchFilters,
    SearchResult,
    SessionSummary,
)
from ..settings import Settings
from . import formatting as fmt
from .backend import (
    Backend,
    CatalogueInfo,
    LibraryStats,
    ProgressCallback,
    ScanProgress,
    Session,
    UpdateOffer,
)

logger = logging.getLogger("piratefinder.ui")

NO_CATALOGUE = (
    "No catalogue file was found. Download one with Update Catalogue, or build it with "
    "python3 -m catalogue_builder."
)
PROBE_TIMEOUT = 15
HISTORY_LIMIT = 200


def check_for_update(settings: Settings, current_built_at: str = "") -> UpdateOffer | None:
    from ..catalogue import update

    info = update.check_for_update(current_built_at, settings.catalogue_feed_url)
    if info is None:
        return None
    return UpdateOffer(info.built_at, info.size, info.notes, info)


def install_update(offer: UpdateOffer, progress: ProgressCallback, cancel) -> Path:
    from ..catalogue import update

    return update.install_update(offer.handle, progress, cancel)


def probe_device(settings: Settings) -> DeviceStatus:
    from ..greaseweazle import client

    return client.probe(timeout=PROBE_TIMEOUT)


def _open_catalogue() -> tuple[Any, str]:
    """The newest usable catalogue, or None and the reason."""
    try:
        from ..catalogue.store import Catalogue, locate_catalogue
    except ImportError as error:
        return None, f"The catalogue reader could not be loaded: {error}"
    path = locate_catalogue()
    if path is None:
        return None, NO_CATALOGUE
    try:
        return Catalogue.open(path), ""
    except Exception as error:  # noqa: BLE001 - shown on the Find page
        logger.exception("Could not open the catalogue %s", path)
        return None, f"The catalogue {path} could not be opened: {error}"


class RealBackend(Backend):
    def __init__(
        self,
        settings: Settings,
        userdb: Any,
        catalogue: Any,
        catalogue_error: str = "",
    ) -> None:
        from ..finder import Finder
        from ..jobs.history import History
        from ..jobs.queue import WriteQueue
        from ..library.library import Library

        super().__init__(settings)
        self.userdb = userdb
        self.catalogue = catalogue
        self.catalogue_error = catalogue_error
        self.library = Library(userdb, catalogue)
        self.finder = Finder(catalogue, self.library, settings)
        self.queue = WriteQueue()
        self.history_store = History(userdb)
        self._info: CatalogueInfo | None = None

    @classmethod
    def open(cls) -> Backend:
        from .. import paths
        from ..library.userdb import UserDatabase

        settings = Settings.load()
        userdb = UserDatabase.open(paths.user_database_path())
        catalogue, error = _open_catalogue()
        return cls(settings, userdb, catalogue, error)

    # Catalogue

    def catalogue_info(self) -> CatalogueInfo:
        if self._info is not None:
            return self._info
        if self.catalogue is None:
            self._info = CatalogueInfo(False, error=self.catalogue_error or NO_CATALOGUE)
            return self._info
        catalogue = self.catalogue
        names = self.finder.provider_names()
        providers = tuple(
            (provider, name if name != provider else fmt.provider_name(provider))
            for provider, name in names.items()
        )
        sources: tuple[dict[str, str], ...] = ()
        with contextlib.suppress(Exception):
            sources = tuple(catalogue.sources())
        self._info = CatalogueInfo(
            True,
            built_at=str(getattr(catalogue, "built_at", "")),
            path=str(getattr(catalogue, "path", "")),
            stats=dict(catalogue.stats()),
            series=tuple(catalogue.series()),
            providers=providers,
            sources=sources,
        )
        return self._info

    def search(self, text: str, filters: SearchFilters) -> list[SearchResult]:
        if self.catalogue is None:
            return []
        return self.finder.search(text, filters)

    def detail(self, disk_id: int) -> DiskDetail:
        if self.catalogue is None:
            raise LookupError("No catalogue is installed.")
        return self.finder.detail(disk_id)

    def check_for_update(self) -> UpdateOffer | None:
        built = str(getattr(self.catalogue, "built_at", "")) if self.catalogue else ""
        return check_for_update(self.settings, built)

    def install_update(self, offer: UpdateOffer, progress: ProgressCallback, cancel) -> None:
        install_update(offer, progress, cancel)
        self.reload_catalogue()

    def reload_catalogue(self) -> None:
        """Switch to the newest catalogue and match the library against it again."""
        catalogue, error = _open_catalogue()
        if catalogue is None:
            raise RuntimeError(error)
        old = self.catalogue
        self.catalogue = catalogue
        self.catalogue_error = ""
        self.finder.set_catalogue(catalogue)
        self._info = None
        self.library.rematch()
        close = getattr(old, "close", None)
        if callable(close):
            with contextlib.suppress(Exception):
                close()

    # Library

    def library_stats(self) -> LibraryStats:
        counts = self.library.stats()
        return LibraryStats(
            images=int(counts.get("images", 0)),
            matched=int(counts.get("matched", 0)),
            unmatched=int(counts.get("unmatched", 0)),
            duplicates=int(counts.get("duplicates", 0)),
            last_scan=self.library.last_scan(),
        )

    def unmatched_files(self, limit: int = 200) -> list[LocalFile]:
        return self.library.search_unmatched("", limit)

    def _folders(self) -> list[str]:
        folders = list(self.settings.library_folders)
        download = self.settings.download_folder
        if download and os.path.isdir(download) and download not in folders:
            folders.append(download)
        return folders

    def scan_library(self, progress: Callable[[ScanProgress], None], cancel) -> ScanSummary:
        folders = self._folders()
        self.library.forget_outside(folders)

        def report(message: str, current: int, total: int) -> None:
            progress(ScanProgress(current, total or None, message))

        return self.library.scan(folders, report, cancel)

    def download(self, item: QueueItem, progress: ProgressCallback, cancel) -> str:
        from ..jobs.session import download_for_item

        return str(
            download_for_item(
                self.finder, self.library, self.settings, item, progress=progress, cancel=cancel
            )
        )

    # Queue

    def queue_items(self) -> list[QueueItem]:
        return self.queue.items()

    def queue_add(self, items: Sequence[QueueItem]) -> None:
        self.queue.extend(items)

    def queue_remove(self, item_ids: Sequence[str]) -> None:
        for item_id in item_ids:
            self.queue.remove(item_id)

    def queue_move(self, item_id: str, offset: int) -> None:
        self.queue.move(item_id, offset)

    def queue_set_copies(self, item_id: str, copies: int) -> None:
        self.queue.set_copies(item_id, copies)

    def queue_clear(self) -> None:
        self.queue.clear()

    # Writing

    def probe(self) -> DeviceStatus:
        return probe_device(self.settings)

    def create_session(self, items: Sequence[QueueItem], events) -> Session:
        from ..jobs.session import WriteSession

        return WriteSession(
            self.finder, self.library, self.settings, items, events, history=self.history_store
        )

    def history(self) -> list[SessionSummary]:
        return self.history_store.sessions(limit=HISTORY_LIMIT)

    def report_text(self, summary: SessionSummary) -> str:
        from ..jobs.history import report_text

        return report_text(summary)

    def close(self) -> None:
        with contextlib.suppress(Exception):
            self.queue.save()
        with contextlib.suppress(Exception):
            self.userdb.close()
        close = getattr(self.catalogue, "close", None)
        if callable(close):
            with contextlib.suppress(Exception):
                close()

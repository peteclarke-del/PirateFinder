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
from dataclasses import replace
from pathlib import Path
from typing import Any

from ..greaseweazle.caps import CapsStatus
from ..models import (
    BootRecheck,
    DeviceStatus,
    DiskDetail,
    Facets,
    ImageRecord,
    LocalFile,
    MediaItem,
    Platform,
    Query,
    QueueItem,
    ResultPage,
    ScanSummary,
    SessionSummary,
    TriviaItem,
    VirusReport,
)
from ..online.prefetch import PictureCount, PictureProgress, PictureSummary
from ..settings import Settings
from . import formatting as fmt
from .backend import (
    Backend,
    BrainfileStatus,
    CatalogueInfo,
    Downloaded,
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


def device_present(settings: Settings) -> bool:
    from ..greaseweazle import client

    return client.device_present(settings.device)


def probe_device(settings: Settings) -> DeviceStatus:
    from ..greaseweazle import client

    return client.probe(
        timeout=PROBE_TIMEOUT, device=settings.device, online=settings.online_enabled
    )


def caps_status() -> CapsStatus:
    from ..greaseweazle import caps

    return caps.status()


def install_caps(progress: ProgressCallback, cancel) -> CapsStatus:
    from ..greaseweazle import caps
    from ..online.http import Downloader

    def report(done: int = 0, total: int | None = None, *_rest: Any) -> None:
        progress(int(done or 0), int(total) if total else None)

    return caps.install(downloader=Downloader(), progress=report, cancel=cancel)


def remove_caps() -> CapsStatus:
    from ..greaseweazle import caps

    return caps.remove()


NO_VIRUS_MODULE = "Virus detection is not part of this version of PirateFinder."


def _field(value: Any, name: str, default: Any = None) -> Any:
    if isinstance(value, dict):
        return value.get(name, default)
    return getattr(value, name, default)


def as_brainfile_status(value: Any) -> BrainfileStatus:
    """Whatever ``images.virus.brainfile_status()`` returns, as the interface shows it."""
    if isinstance(value, BrainfileStatus):
        return value
    if value is None or value is False:
        return BrainfileStatus(False)
    if isinstance(value, str | Path):
        path = Path(value)
        return BrainfileStatus(path.exists(), path=str(path))
    if isinstance(value, tuple) and not hasattr(value, "_fields"):
        # (installed, version, boot blocks named), as images.virus gives it
        installed, version, entries = (*value, *(False, "", 0)[len(value) :])[:3]
        return BrainfileStatus(
            bool(installed), version=str(version or ""), entries=int(entries or 0)
        )
    path = str(_field(value, "path", "") or "")
    entries = _field(value, "entries", None)
    if entries is None:
        entries = _field(value, "count", 0)
    installed = _field(value, "installed", None)
    if installed is None:
        installed = bool(path and Path(path).is_file()) or bool(entries)
    return BrainfileStatus(
        bool(installed),
        version=str(_field(value, "version", "") or ""),
        entries=int(entries or 0),
        path=path,
        message=str(_field(value, "message", "") or _field(value, "error", "") or ""),
    )


def brainfile_status() -> BrainfileStatus:
    try:
        from ..images import virus
    except ImportError:
        return BrainfileStatus(False, message=NO_VIRUS_MODULE)
    status = getattr(virus, "brainfile_status", None)
    if not callable(status):
        return BrainfileStatus(False, message=NO_VIRUS_MODULE)
    return _with_folder(as_brainfile_status(status()), virus)


def _with_folder(status: BrainfileStatus, virus: Any) -> BrainfileStatus:
    folder = getattr(virus, "brainfile_folder", None)
    if status.path or not status.installed or not callable(folder):
        return status
    with contextlib.suppress(Exception):
        return replace(status, path=str(folder()))
    return status


def install_brainfile(progress: ProgressCallback, cancel) -> BrainfileStatus:
    try:
        from ..images import virus
    except ImportError as error:
        raise RuntimeError(NO_VIRUS_MODULE) from error
    install = getattr(virus, "install_brainfile", None)
    if not callable(install):
        raise RuntimeError(NO_VIRUS_MODULE)
    from ..online.http import Downloader

    def report(done: int = 0, total: int | None = None, *_rest: Any) -> None:
        progress(int(done or 0), int(total) if total else None)

    install(Downloader(), report, cancel)
    return brainfile_status()


def _open_catalogue() -> tuple[Any, str]:
    """The newest usable catalogue, or None and the reason."""
    try:
        from ..catalogue.store import Catalogue, CatalogueError, locate_catalogue
    except ImportError as error:
        return None, f"The catalogue reader could not be loaded: {error}"
    path = locate_catalogue()
    if path is None:
        return None, NO_CATALOGUE
    try:
        return Catalogue.open(path), ""
    except CatalogueError as error:  # its message starts with the path
        return None, f"The catalogue {error}"
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

    def search_page(self, query: Query) -> ResultPage:
        if self.catalogue is None:
            return ResultPage(query, (), 0)
        return self.finder.search_page(query)

    def facets(self) -> Facets:
        # The catalogue's counts are cached by the search; corrections are added each time.
        if self.catalogue is None:
            return Facets()
        return self.finder.facets()

    def detail(self, disk_id: int) -> DiskDetail:
        if self.catalogue is None:
            raise LookupError("No catalogue is installed.")
        return self.finder.detail(disk_id)

    def summaries(self, disk_id: int, content_id: int | None = None) -> list[TriviaItem]:
        if self.catalogue is None or not self.media_enabled:
            return []
        return list(self.finder.summaries(disk_id, content_id))

    def media_file(self, item: MediaItem) -> Path | None:
        if not self.media_enabled:
            return None
        path = self.finder.media_file(item)
        return Path(path) if path else None

    def picture_count(self, platforms: Sequence[Platform] = ()) -> PictureCount:
        if self.catalogue is None:
            return PictureCount(0, 0, 0)
        return self.finder.picture_count(platforms)

    def download_pictures(
        self,
        platforms: Sequence[Platform],
        progress: Callable[[PictureProgress], None],
        cancel,
    ) -> PictureSummary:
        if self.catalogue is None or not self.media_enabled:
            return PictureSummary(0, 0, 0, 0, stopped=True)
        return self.finder.download_pictures(platforms, progress, cancel)

    def clean_alternates(self, disk_id: int) -> list[ImageRecord]:
        if self.catalogue is None:
            return []
        return list(self.finder.clean_alternates(disk_id))

    def save_details(
        self, disk_id: int, values: dict[str, str], titles: dict[int, str] | None = None
    ) -> None:
        if self.catalogue is None:
            raise LookupError("No catalogue is installed.")
        self.finder.save_details(disk_id, values, titles)

    def revert_details(self, disk_id: int) -> None:
        if self.catalogue is None:
            raise LookupError("No catalogue is installed.")
        self.finder.revert_details(disk_id)

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
            infected=int(counts.get("infected", 0)),
        )

    def unmatched_files(self, limit: int = 200, text: str = "") -> list[LocalFile]:
        return self.library.search_unmatched(text, limit)

    def infected_files(self, limit: int = 200) -> list[LocalFile]:
        return self.library.infected_files(limit)

    def virus_report(self, local: LocalFile) -> VirusReport | None:
        # Works without a catalogue too; the finder then has no flag to add.
        return self.finder.local_virus_report(local)

    def clean_file(self, local: LocalFile) -> LocalFile:
        # The finder files a cleaned copy of an archive member in the download folder.
        return self.finder.clean_file(local)

    def brainfile_status(self) -> BrainfileStatus:
        return brainfile_status()

    def install_brainfile(self, progress: ProgressCallback, cancel) -> BrainfileStatus:
        # The new brainfile changes the virus data fingerprint, so the window's
        # recheck_boot_blocks then checks the library's boot blocks again.
        return install_brainfile(progress, cancel)

    def recheck_boot_blocks(
        self, progress: Callable[[ScanProgress], None], cancel
    ) -> BootRecheck | None:
        def report(message: str, current: int, total: int) -> None:
            progress(ScanProgress(current, total or None, message))

        return self.library.recheck_boot_blocks(report, cancel)

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

    def download(self, item: QueueItem, progress: ProgressCallback, cancel) -> Downloaded:
        from ..jobs.session import download_for_item

        notes: list[str] = []
        path = download_for_item(
            self.finder,
            self.library,
            self.settings,
            item,
            progress=progress,
            cancel=cancel,
            notes=notes,
        )
        return Downloaded(str(path), tuple(notes))

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

    def device_present(self) -> bool:
        return device_present(self.settings)

    def probe(self) -> DeviceStatus:
        return probe_device(self.settings)

    def caps_status(self) -> CapsStatus:
        return caps_status()

    def install_caps(self, progress: ProgressCallback, cancel) -> CapsStatus:
        return install_caps(progress, cancel)

    def remove_caps(self) -> CapsStatus:
        return remove_caps()

    def create_session(self, items: Sequence[QueueItem], events) -> Session:
        from ..jobs.session import WriteSession

        return WriteSession(
            self.finder, self.library, self.settings, items, events, history=self.history_store
        )

    def history(self) -> list[SessionSummary]:
        return self.history_store.sessions(limit=HISTORY_LIMIT)

    def close(self) -> None:
        with contextlib.suppress(Exception):
            self.queue.save()
        with contextlib.suppress(Exception):
            self.userdb.close()
        close = getattr(self.catalogue, "close", None)
        if callable(close):
            with contextlib.suppress(Exception):
                close()

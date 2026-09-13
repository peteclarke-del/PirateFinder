"""Everything the window needs from the rest of the application, in one object.

The window never drives the catalogue, library, writer or job modules
itself, and borrows only their pure helpers, such as file names and the
copies rule. It talks to a ``Backend``: ``Backend.create()`` builds the real one
from those modules, and tests and screenshot runs pass a fake with a few
synthetic disks (``ui.fake_backend``). Calls that may block are made from
worker threads; a backend must not touch GTK.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any, Protocol

from .. import app_update
from ..app_update import AppRelease, PackageTarget
from ..greaseweazle.caps import CapsStatus
from ..jobs.queue import clamp_copies
from ..models import (
    BootRecheck,
    DeviceStatus,
    DiskDetail,
    Facets,
    ImageRecord,
    LocalFile,
    MediaItem,
    Query,
    QueueItem,
    ResultPage,
    ScanSummary,
    Series,
    SessionSummary,
    TriviaItem,
    VirusReport,
)
from ..settings import Settings

FETCH_MEDIA = "fetch_media"


def fetch_media(settings: Settings) -> bool:
    """Whether pictures and background information may be downloaded."""
    if FETCH_MEDIA in {item.name for item in fields(settings)}:
        return bool(getattr(settings, FETCH_MEDIA))
    return bool(settings.extra.get(FETCH_MEDIA, True))


def set_fetch_media(settings: Settings, value: bool) -> None:
    """Store the choice; the key is the same whether or not Settings has the field yet."""
    if FETCH_MEDIA in {item.name for item in fields(settings)}:
        setattr(settings, FETCH_MEDIA, bool(value))
    else:
        settings.extra[FETCH_MEDIA] = bool(value)


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
    infected: int = 0  # images whose boot block held a virus when it was last checked


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


@dataclass(frozen=True, slots=True)
class Downloaded:
    """A disk fetched by Download Only: where it was saved, and what to know about it."""

    path: str
    notes: tuple[str, ...] = ()  # such as a download that could not be checked


@dataclass(frozen=True, slots=True)
class BrainfileStatus:
    """The Amiga Bootblock Reader brainfile used to name Amiga boot blocks."""

    installed: bool
    version: str = ""
    entries: int = 0
    path: str = ""
    message: str = ""  # why it cannot be used or installed, when that is known


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

    def search_page(self, query: Query) -> ResultPage:
        """One page of titles or discs for the Find screen."""
        raise NotImplementedError

    def facets(self) -> Facets:
        """Crews, years and types with their disc counts, for the filter drop-downs."""
        raise NotImplementedError

    def detail(self, disk_id: int) -> DiskDetail:
        raise NotImplementedError

    def summaries(self, disk_id: int, content_id: int | None = None) -> list[TriviaItem]:
        """Background articles for a disc or one title on it; may use the network."""
        raise NotImplementedError

    def media_file(self, item: MediaItem) -> Path | None:
        """A local copy of a picture, downloaded and cached on first use; may block."""
        raise NotImplementedError

    def clean_alternates(self, disk_id: int) -> list[ImageRecord]:
        """Other dumps of a disc that carry no known virus, best first."""
        raise NotImplementedError

    def save_details(
        self, disk_id: int, values: dict[str, str], titles: dict[int, str] | None = None
    ) -> None:
        """Keep the user's corrections of a disc (Edit Details): what differs from the catalogue.

        ``values`` holds the editable fields by name (``library.corrections``),
        ``titles`` the title names by content id. Raises ``CorrectionError``
        with a sentence for the user when a value cannot be stored.
        """
        raise NotImplementedError

    def revert_details(self, disk_id: int) -> None:
        """Forget the user's corrections of a disc, so the catalogue's values show again."""
        raise NotImplementedError

    @property
    def media_enabled(self) -> bool:
        """Pictures and summaries may be fetched: their own switch and online use are on."""
        return fetch_media(self.settings) and self.settings.online_enabled

    def check_for_update(self) -> UpdateOffer | None:
        """A newer catalogue, or None when the installed one is current."""
        raise NotImplementedError

    def install_update(self, offer: UpdateOffer, progress: ProgressCallback, cancel) -> None:
        """Download and install ``offer``, then reopen the catalogue."""
        raise NotImplementedError

    # Application updates, the same for every backend but the fake

    def app_update_target(self) -> PackageTarget | None:
        """The system this package was built for, or None when run from the source tree."""
        return app_update.installed_target()

    def check_app_update(self) -> AppRelease | None:
        """A newer PirateFinder release, or None when this is the newest."""
        return app_update.check(self.app_update_target())

    def download_app_update(self, release: AppRelease, progress: ProgressCallback, cancel) -> Path:
        """Download the release's package for this system and check its checksum."""
        return app_update.download(release, progress, cancel)

    def install_app_update(self, package: Path) -> None:
        """Install the downloaded package; the system asks for the user's password."""
        app_update.install(package)

    # Library

    def library_stats(self) -> LibraryStats:
        raise NotImplementedError

    def unmatched_files(self, limit: int = 200, text: str = "") -> list[LocalFile]:
        """Library files that match no catalogue disc, optionally those matching ``text``."""
        raise NotImplementedError

    def infected_files(self, limit: int = 200) -> list[LocalFile]:
        """Library files whose boot block held a virus when it was last checked."""
        raise NotImplementedError

    def virus_report(self, local: LocalFile) -> VirusReport | None:
        """What is on the boot block of a library file; reads the file."""
        raise NotImplementedError

    def clean_file(self, local: LocalFile) -> LocalFile:
        """Remove a boot block virus from a library file, keeping the original as a backup."""
        raise NotImplementedError

    def brainfile_status(self) -> BrainfileStatus:
        raise NotImplementedError

    def install_brainfile(self, progress: ProgressCallback, cancel) -> BrainfileStatus:
        """Download the Amiga Bootblock Reader brainfile into the data folder."""
        raise NotImplementedError

    def scan_library(self, progress: Callable[[ScanProgress], None], cancel) -> ScanSummary:
        raise NotImplementedError

    def recheck_boot_blocks(
        self, progress: Callable[[ScanProgress], None], cancel
    ) -> BootRecheck | None:
        """Check the library's boot blocks again when the virus data changed since they
        were checked (a new PirateFinder or brainfile); None when it has not."""
        return None

    def download(self, item: QueueItem, progress: ProgressCallback, cancel) -> Downloaded:
        """Fetch the image for ``item`` into the download folder."""
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

    def device_present(self) -> bool:
        """Whether a Greaseweazle looks plugged in, read from files: quick, no gw, no network."""
        raise NotImplementedError

    def probe(self) -> DeviceStatus:
        """Identify the Greaseweazle with ``gw info``, which also looks up firmware online."""
        raise NotImplementedError

    def caps_status(self) -> CapsStatus:
        """Whether gw can write IPF images: the SPS Decoder Library and where it comes from."""
        raise NotImplementedError

    def install_caps(self, progress: ProgressCallback, cancel) -> CapsStatus:
        """Download the SPS Decoder Library for this computer into the data folder."""
        raise NotImplementedError

    def remove_caps(self) -> CapsStatus:
        """Remove the SPS Decoder Library PirateFinder installed."""
        raise NotImplementedError

    def caps_licence(self) -> str:
        """The licence shown, and accepted, before the SPS Decoder Library is downloaded."""
        from ..greaseweazle.caps import licence_text

        return licence_text()

    def create_session(self, items: Sequence[QueueItem], events) -> Session:
        raise NotImplementedError

    def history(self) -> list[SessionSummary]:
        raise NotImplementedError

    def report_text(self, summary: SessionSummary) -> str:
        from ..jobs.history import report_text

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

    def search_page(self, query: Query) -> ResultPage:
        return ResultPage(query, (), 0)

    def facets(self) -> Facets:
        return Facets()

    def detail(self, disk_id: int) -> DiskDetail:
        raise LookupError("No catalogue is installed.")

    def summaries(self, disk_id: int, content_id: int | None = None) -> list[TriviaItem]:
        return []

    def media_file(self, item: MediaItem) -> Path | None:
        return None

    def clean_alternates(self, disk_id: int) -> list[ImageRecord]:
        return []

    def save_details(
        self, disk_id: int, values: dict[str, str], titles: dict[int, str] | None = None
    ) -> None:
        raise RuntimeError(f"Details cannot be edited: {self.error}")

    def revert_details(self, disk_id: int) -> None:
        raise RuntimeError(f"Details cannot be edited: {self.error}")

    def check_for_update(self) -> UpdateOffer | None:
        from .real_backend import check_for_update

        return check_for_update(self.settings)

    def install_update(self, offer: UpdateOffer, progress: ProgressCallback, cancel) -> None:
        from .real_backend import install_update

        install_update(offer, progress, cancel)

    def library_stats(self) -> LibraryStats:
        return LibraryStats()

    def unmatched_files(self, limit: int = 200, text: str = "") -> list[LocalFile]:
        return []

    def infected_files(self, limit: int = 200) -> list[LocalFile]:
        return []

    def virus_report(self, local: LocalFile) -> VirusReport | None:
        return None

    def clean_file(self, local: LocalFile) -> LocalFile:
        raise RuntimeError(f"The file cannot be cleaned: {self.error}")

    def brainfile_status(self) -> BrainfileStatus:
        from .real_backend import brainfile_status

        return brainfile_status()

    def install_brainfile(self, progress: ProgressCallback, cancel) -> BrainfileStatus:
        from .real_backend import install_brainfile

        return install_brainfile(progress, cancel)

    def scan_library(self, progress, cancel) -> ScanSummary:
        raise RuntimeError(f"The library cannot be scanned: {self.error}")

    def download(self, item: QueueItem, progress: ProgressCallback, cancel) -> Downloaded:
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
                item.copies = clamp_copies(copies)

    def queue_clear(self) -> None:
        self._queue.clear()

    def device_present(self) -> bool:
        from .real_backend import device_present

        return device_present(self.settings)

    def probe(self) -> DeviceStatus:
        from .real_backend import probe_device

        return probe_device(self.settings)

    def caps_status(self) -> CapsStatus:
        from .real_backend import caps_status

        return caps_status()

    def install_caps(self, progress: ProgressCallback, cancel) -> CapsStatus:
        from .real_backend import install_caps

        return install_caps(progress, cancel)

    def remove_caps(self) -> CapsStatus:
        from .real_backend import remove_caps

        return remove_caps()

    def create_session(self, items: Sequence[QueueItem], events) -> Session:
        raise RuntimeError(f"Disks cannot be written: {self.error}")

    def history(self) -> list[SessionSummary]:
        return []

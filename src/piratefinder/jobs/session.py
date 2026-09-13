"""Run a write queue: resolve, download, prepare, prompt and write each disk.

For every item the session asks the finder where an image can come from and
tries those sources best first: a local file is read, an online location is
downloaded into the download folder and added to the library. The first
image that prepares cleanly is written after the user confirms that a floppy
is in the drive. A write-protected disk or an empty drive leads back to the
prompt with the reason. While one disk is being written, the next one is
downloaded in the background when it is only available online. A boot block
virus that can be removed is removed from the written copy when the item asks
for it (``QueueItem.clean_virus``, the default). The notes made on the way
(conversions, a virus removed, a download that could not be checked) go into
each result (``WriteOutcome.notes``), and so into the summary, the history and
the report.

``run`` blocks and is meant for a worker thread. Every event callback is
called on that thread; ``ask_insert`` blocks until the user answers.
"""

from __future__ import annotations

import contextlib
import shutil
import tempfile
import threading
import traceback
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol

from ..archive_layout import DEFAULT_FOLDERS
from ..images.archives import image_file_name
from ..models import (
    ImageSource,
    PreparedImage,
    QueueItem,
    SessionSummary,
    WriteOutcome,
    WriteProgress,
    WriteStatus,
)
from ..online.http import DownloadCancelled
from .cancellation import Cancellation
from .queue import copy_labels

STAGE_RESOLVE = "resolve"
STAGE_DOWNLOAD = "download"
STAGE_PREPARE = "prepare"
STAGE_INSERT = "insert"
STAGE_WRITE = "write"

ANSWER_WRITE = "write"
ANSWER_SKIP = "skip"
ANSWER_STOP = "stop"

_RETRY_AT_PROMPT = (WriteStatus.WRITE_PROTECTED, WriteStatus.NO_DISK)
_PROMPT_REASONS = {
    WriteStatus.WRITE_PROTECTED: "The disk is write-protected. Slide the tab to cover the hole, "
    "or insert another disk.",
    WriteStatus.NO_DISK: "There is no disk in the drive. Insert a disk.",
}


class SessionEvents(Protocol):
    """What a session reports to the interface, on the session's thread."""

    def on_stage(self, item: QueueItem, index: int, total: int, stage: str, message: str) -> None:
        """A new stage started for one item."""

    def on_download(self, item: QueueItem, done: int, total: int | None) -> None:
        """Bytes downloaded so far for one item."""

    def on_write_progress(self, item: QueueItem, progress: WriteProgress) -> None:
        """Track progress reported by the writer."""

    def ask_insert(self, item: QueueItem, index: int, total: int, reason: str = "") -> str:
        """Block until the user answers "write", "skip" or "stop"."""
        ...

    def on_item_finished(self, item: QueueItem, outcome: WriteOutcome) -> None:
        """One copy of one item is done."""


class QuietEvents:
    """Events that ignore everything and always answer "write"; a base for fakes."""

    def on_stage(self, item: QueueItem, index: int, total: int, stage: str, message: str) -> None:
        """Ignore a stage change."""

    def on_download(self, item: QueueItem, done: int, total: int | None) -> None:
        """Ignore download progress."""

    def on_write_progress(self, item: QueueItem, progress: WriteProgress) -> None:
        """Ignore write progress."""

    def ask_insert(self, item: QueueItem, index: int, total: int, reason: str = "") -> str:
        """Answer "write" at once."""
        return ANSWER_WRITE

    def on_item_finished(self, item: QueueItem, outcome: WriteOutcome) -> None:
        """Ignore a finished item."""


def with_notes(outcome: WriteOutcome, notes: Sequence[str]) -> WriteOutcome:
    """``outcome`` carrying the notes made for its disk, when it has none of its own."""
    return replace(outcome, notes=tuple(notes)) if notes and not outcome.notes else outcome


def _default_writer() -> Callable[..., WriteOutcome]:
    from ..greaseweazle.client import write

    return write


def _default_preparer() -> Callable[..., PreparedImage]:
    from ..images.prepare import prepare

    return prepare


def _default_fetcher() -> Callable[..., Path]:
    from ..online.fetch import fetch_location

    return fetch_location


def _new_controller() -> Any:
    try:
        from ..greaseweazle.runner import OperationController
    except ImportError:
        return Cancellation()
    return OperationController()


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _archive_folders(finder: Any, item: QueueItem) -> tuple[str, str]:
    """The type and crew folders a download for ``item`` is filed under."""
    if item.disk_id is not None:
        with contextlib.suppress(Exception):
            return finder.archive_folders(item.disk_id)
    return DEFAULT_FOLDERS


def _source_name(source: ImageSource) -> str:
    local = source.local
    return image_file_name(local.path, local.member) if local is not None else ""


def fetch_source(
    finder: Any,
    library: Any,
    settings: Any,
    item: QueueItem,
    source: ImageSource,
    *,
    fetcher: Callable[..., Path] | None = None,
    downloader: Any = None,
    progress: Callable[[int, int | None], None] | None = None,
    cancel: object | None = None,
    notes: list[str] | None = None,
) -> Path:
    """Download one online source into the download folder and add it to the library.

    The fetcher checks the download against the location's checksum, its dump,
    or else every dump of the disc (``source.dumps``); a download that fails
    raises FetchError, so ``download_for_item`` and the session try the next
    source.
    """
    assert source.location is not None
    fetch = fetcher or _default_fetcher()
    path = Path(
        fetch(
            source.location,
            source.image,
            download_folder=settings.download_folder,
            folders=_archive_folders(finder, item),
            platform=source.platform or item.platform,
            downloader=downloader,
            progress=progress,
            cancel=cancel,
            notes=notes,
            dumps=source.dumps,
        )
    )
    with contextlib.suppress(Exception):  # a failure to index must not lose the download
        library.add_file(path)
    return path


def download_for_item(
    finder: Any,
    library: Any,
    settings: Any,
    item: QueueItem,
    *,
    fetcher: Callable[..., Path] | None = None,
    downloader: Any = None,
    progress: Callable[[int, int | None], None] | None = None,
    cancel: object | None = None,
    notes: list[str] | None = None,
) -> Path:
    """Download the best online image for ``item``; raise FetchError when none works."""
    from ..online.fetch import FetchError

    problems: list[str] = []
    for source in finder.sources_for(item):
        if source.location is None:
            continue
        try:
            return fetch_source(
                finder,
                library,
                settings,
                item,
                source,
                fetcher=fetcher,
                downloader=downloader,
                progress=progress,
                cancel=cancel,
                notes=notes,
            )
        except DownloadCancelled:
            raise
        except Exception as error:  # try the next provider
            problems.append(f"{source.label}: {error}")
    if not problems:
        raise FetchError(f"No enabled provider has an image of {item.label}.")
    raise FetchError(" ".join(problems))


@dataclass(slots=True)
class _Ready:
    """An image prepared for writing and where it came from."""

    source: ImageSource
    prepared: PreparedImage
    workdir: Path
    notes: list[str] = field(default_factory=list)


@dataclass(slots=True)
class _Prefetch:
    item_id: str
    done: threading.Event = field(default_factory=threading.Event)
    thread: threading.Thread | None = None
    path: Path | None = None
    source: ImageSource | None = None
    notes: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    tried: set[int] = field(default_factory=set)


class WriteSession:
    """Writes a list of queue items, one floppy at a time."""

    def __init__(
        self,
        finder: Any,
        library: Any,
        settings: Any,
        items: Sequence[QueueItem],
        events: SessionEvents,
        *,
        writer: Callable[..., WriteOutcome] | None = None,
        preparer: Callable[..., PreparedImage] | None = None,
        fetcher: Callable[..., Path] | None = None,
        downloader: Any = None,
        history: Any = None,
    ) -> None:
        self.finder = finder
        self.library = library
        self.settings = settings
        self.items = list(items)
        self.events = events
        self._writer = writer
        self._preparer = preparer
        self._fetcher = fetcher
        self._downloader = downloader
        self._history = history
        self._cancel = Cancellation()
        # Background downloads stop when the session is cancelled or ends.
        self._prefetch_cancel = Cancellation()
        self._controller: Any = None
        self._controller_lock = threading.Lock()
        self._stopped = False
        self._prompted = False
        self._prefetches: dict[str, _Prefetch] = {}
        self._workdirs: set[Path] = set()

    @property
    def cancelled(self) -> bool:
        """True once ``cancel`` has been called."""
        return self._cancel.cancelled

    def cancel(self) -> None:
        """Stop the running write or download and end the session."""
        self._cancel.cancel()
        self._prefetch_cancel.cancel()
        with self._controller_lock:
            controller = self._controller
        if controller is not None:
            controller.cancel()

    def run(self) -> SessionSummary:
        """Write every item and return the summary, which is also recorded in history."""
        started = _now()
        results: list[tuple[str, WriteOutcome, str]] = []
        total = len(self.items)
        try:
            for index, item in enumerate(self.items):
                item.notes = []  # notes describe this session's attempt only
                if self._stopped or self._cancel.cancelled:
                    reason = (
                        "The session was cancelled before this disk."
                        if self._cancel.cancelled
                        else "The session was stopped before this disk."
                    )
                    self._finish(item, WriteOutcome(WriteStatus.SKIPPED, reason), "", results)
                    continue
                try:
                    self._run_item(item, index, total, results)
                except Exception as error:  # one faulty disk does not end the session
                    outcome = WriteOutcome(
                        WriteStatus.FAILED,
                        f"An unexpected error stopped this disk: {error}",
                        diagnostic=traceback.format_exc(),
                    )
                    self._finish(item, outcome, item.source_used, results)
        finally:
            self._cleanup()
        summary = SessionSummary(started, _now(), self.settings.drive, tuple(results))
        self._record(summary)
        return summary

    # One item ----------------------------------------------------------------

    def _run_item(
        self,
        item: QueueItem,
        index: int,
        total: int,
        results: list[tuple[str, WriteOutcome, str]],
    ) -> None:
        self.events.on_stage(item, index, total, STAGE_RESOLVE, f"Looking for {item.label}")
        ready = self._obtain(item, index, total)
        if isinstance(ready, WriteOutcome):
            self._finish(item, ready, "", results)
            return
        try:
            item.source_used = ready.source.label
            for note in [*ready.notes, *ready.prepared.notes]:
                if note not in item.notes:
                    item.notes.append(note)
            for label in copy_labels(item):
                outcome = self._write_copy(item, index, total, ready)
                self._finish(item, outcome, ready.source.label, results, label)
                if self._stopped or self._cancel.cancelled:
                    break
        finally:
            self._remove_workdir(ready.workdir)

    def _finish(
        self,
        item: QueueItem,
        outcome: WriteOutcome,
        source: str,
        results: list[tuple[str, WriteOutcome, str]],
        label: str | None = None,
    ) -> None:
        outcome = with_notes(outcome, item.notes)
        item.outcome = outcome
        results.append((label or item.label, outcome, source))
        self.events.on_item_finished(item, outcome)

    def _obtain(self, item: QueueItem, index: int, total: int) -> _Ready | WriteOutcome:
        """Find, fetch and prepare the first usable image for ``item``."""
        problems: list[str] = []
        tried: set[int] = set()
        prefetch = self._prefetches.pop(item.id, None)
        if prefetch is not None:
            if not prefetch.done.is_set():
                self.events.on_stage(
                    item, index, total, STAGE_DOWNLOAD, f"Finishing the download of {item.label}"
                )
                prefetch.done.wait()
            tried = set(prefetch.tried)
            problems += prefetch.problems
            if prefetch.path is not None and prefetch.source is not None:
                ready = self._prepare_path(item, index, total, prefetch, problems)
                if ready is not None:
                    return ready
        if self._cancel.cancelled:
            return WriteOutcome(WriteStatus.CANCELLED, "The session was cancelled.")
        sources = self.finder.sources_for(item)
        for source in sources:
            if self._cancel.cancelled:
                return WriteOutcome(WriteStatus.CANCELLED, "The session was cancelled.")
            if source.location is not None and source.location.id in tried:
                continue
            notes: list[str] = []
            try:
                data, name = self._read_source(item, index, total, source, notes)
            except DownloadCancelled:
                return WriteOutcome(WriteStatus.CANCELLED, "The download was cancelled.")
            except Exception as error:  # an unreadable file or failed download: next source
                problems.append(f"{source.label}: {error}")
                continue
            ready = self._prepare(item, index, total, source, data, name, notes, problems)
            if ready is not None:
                return ready
        if not sources:
            summary = "No image of this disk is in the library and no enabled provider has one."
        elif problems:
            summary = "None of the images found for this disk could be used."
        else:
            summary = "No usable image of this disk was found."
        return WriteOutcome(WriteStatus.UNAVAILABLE, summary, diagnostic="\n".join(problems))

    def _read_source(
        self,
        item: QueueItem,
        index: int,
        total: int,
        source: ImageSource,
        notes: list[str],
    ) -> tuple[bytes, str]:
        if source.local is not None:
            return self.library.read_bytes(source.local), _source_name(source)
        self.events.on_stage(
            item, index, total, STAGE_DOWNLOAD, f"Downloading {item.label} from {source.label}"
        )
        path = fetch_source(
            self.finder,
            self.library,
            self.settings,
            item,
            source,
            fetcher=self._fetcher,
            downloader=self._downloader,
            progress=lambda done, size: self.events.on_download(item, done, size),
            cancel=self._cancel,
            notes=notes,
        )
        return path.read_bytes(), path.name

    def _prepare_path(
        self,
        item: QueueItem,
        index: int,
        total: int,
        prefetch: _Prefetch,
        problems: list[str],
    ) -> _Ready | None:
        assert prefetch.path is not None and prefetch.source is not None
        try:
            data = prefetch.path.read_bytes()
        except OSError as error:
            problems.append(f"{prefetch.source.label}: {error}")
            return None
        return self._prepare(
            item, index, total, prefetch.source, data, prefetch.path.name, prefetch.notes, problems
        )

    def _prepare(
        self,
        item: QueueItem,
        index: int,
        total: int,
        source: ImageSource,
        data: bytes,
        name: str,
        notes: list[str],
        problems: list[str],
    ) -> _Ready | None:
        self.events.on_stage(item, index, total, STAGE_PREPARE, f"Preparing {name}")
        workdir = Path(tempfile.mkdtemp(prefix="piratefinder-write-"))
        self._workdirs.add(workdir)
        preparer = self._preparer or _default_preparer()
        try:
            prepared = preparer(
                data,
                name,
                workdir,
                label=item.label,
                platform=item.platform or source.platform,
                clean_virus=item.clean_virus,
            )
        except Exception as error:  # PrepareError carries a sentence for the user
            problems.append(f"{source.label}: {error}")
            self._remove_workdir(workdir)
            return None
        return _Ready(source, prepared, workdir, list(notes))

    def _write_copy(self, item: QueueItem, index: int, total: int, ready: _Ready) -> WriteOutcome:
        reason = ""
        while True:
            if reason or self.settings.prompt_between_disks or not self._prompted:
                self.events.on_stage(
                    item, index, total, STAGE_INSERT, reason or f"Insert a disk for {item.label}"
                )
                answer = self.events.ask_insert(item, index, total, reason)
                if self._cancel.cancelled:
                    return WriteOutcome(WriteStatus.CANCELLED, "The session was cancelled.")
                if answer == ANSWER_SKIP:
                    return WriteOutcome(WriteStatus.SKIPPED, "Skipped at the insert-disk prompt.")
                if answer != ANSWER_WRITE:
                    self._stopped = True
                    return WriteOutcome(
                        WriteStatus.SKIPPED, "The session was stopped at the insert-disk prompt."
                    )
            self._prompted = True
            self._start_prefetch(index + 1)
            self.events.on_stage(item, index, total, STAGE_WRITE, f"Writing {item.label}")
            outcome = self._write(item, ready.prepared)
            if self._cancel.cancelled and not outcome.succeeded:
                if outcome.status is not WriteStatus.CANCELLED:
                    outcome = replace(outcome, status=WriteStatus.CANCELLED)
                return outcome
            if outcome.status in _RETRY_AT_PROMPT:
                reason = outcome.summary or _PROMPT_REASONS[outcome.status]
                continue
            return outcome

    def _write(self, item: QueueItem, prepared: PreparedImage) -> WriteOutcome:
        writer = self._writer or _default_writer()
        controller = _new_controller()
        with self._controller_lock:
            self._controller = controller
        if self._cancel.cancelled:
            controller.cancel()
        try:
            return writer(
                prepared,
                drive=self.settings.drive,
                device=self.settings.device,
                retries=self.settings.retries,
                pre_erase=self.settings.pre_erase,
                progress=lambda progress: self.events.on_write_progress(item, progress),
                controller=controller,
            )
        except Exception as error:  # the writer should not raise, but a fault is still a result
            return WriteOutcome(
                WriteStatus.FAILED,
                f"Greaseweazle could not write the disk: {error}",
                diagnostic=traceback.format_exc(),
            )
        finally:
            with self._controller_lock:
                self._controller = None

    # Prefetch ----------------------------------------------------------------

    def _start_prefetch(self, index: int) -> None:
        """Download the item at ``index`` in the background when it is online only."""
        if index >= len(self.items) or self._cancel.cancelled or self._stopped:
            return
        item = self.items[index]
        if item.id in self._prefetches:
            return
        try:
            sources = self.finder.sources_for(item)
        except Exception:  # the item will be resolved again in the foreground
            return
        if any(source.local is not None for source in sources):
            return
        online = [source for source in sources if source.location is not None]
        if not online:
            return
        prefetch = _Prefetch(item.id)
        prefetch.thread = threading.Thread(
            target=self._prefetch, args=(item, online, prefetch), daemon=True
        )
        self._prefetches[item.id] = prefetch
        prefetch.thread.start()

    def _prefetch(self, item: QueueItem, sources: list[ImageSource], prefetch: _Prefetch) -> None:
        try:
            for source in sources:
                if self._prefetch_cancel.cancelled:
                    return
                assert source.location is not None
                prefetch.tried.add(source.location.id)
                notes: list[str] = []
                try:
                    path = fetch_source(
                        self.finder,
                        self.library,
                        self.settings,
                        item,
                        source,
                        fetcher=self._fetcher,
                        downloader=self._downloader,
                        cancel=self._prefetch_cancel,
                        notes=notes,
                    )
                except DownloadCancelled:
                    prefetch.tried.discard(source.location.id)
                    return
                except Exception as error:  # the foreground tries the remaining sources
                    prefetch.problems.append(f"{source.label}: {error}")
                    continue
                prefetch.path, prefetch.source, prefetch.notes = path, source, notes
                return
        finally:
            prefetch.done.set()

    # Housekeeping ------------------------------------------------------------

    def _remove_workdir(self, workdir: Path) -> None:
        shutil.rmtree(workdir, ignore_errors=True)
        self._workdirs.discard(workdir)

    def _cleanup(self) -> None:
        self._prefetch_cancel.cancel()
        for prefetch in list(self._prefetches.values()):
            if prefetch.thread is not None:
                prefetch.thread.join()
        self._prefetches.clear()
        for workdir in list(self._workdirs):
            self._remove_workdir(workdir)

    def _record(self, summary: SessionSummary) -> None:
        history = self._history
        if history is None:
            userdb = getattr(self.library, "userdb", None)
            if userdb is None:
                return
            from .history import History

            history = History(userdb)
        with contextlib.suppress(Exception):  # history is a record, not part of the write
            history.record(summary)

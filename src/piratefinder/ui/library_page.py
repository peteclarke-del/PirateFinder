"""The Library page: folders scanned for images, the download folder and scans."""

from __future__ import annotations

from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gtk  # noqa: E402

from ..jobs.cancellation import Cancellation  # noqa: E402
from ..models import LocalFile, ScanSummary  # noqa: E402
from . import formatting as fmt  # noqa: E402
from .backend import LibraryStats, ScanProgress  # noqa: E402
from .bridge import Latest, run_in_thread  # noqa: E402
from .detail_pane import platform_for_file  # noqa: E402
from .widgets import (  # noqa: E402
    GroupRows,
    choose_folder,
    icon_button,
    plain_row,
    progress_bar,
    set_fraction,
    show_in_files,
    text_button,
)

UNMATCHED_LIMIT = 200


def _row(title: str, subtitle: str = "") -> Adw.ActionRow:
    return plain_row(title=title, subtitle=subtitle)


def _value_row(title: str) -> tuple[Adw.ActionRow, Gtk.Label]:
    row = _row(title)
    value = Gtk.Label(xalign=1)
    value.add_css_class("numeric")
    value.add_css_class("dim-label")
    row.add_suffix(value)
    return row, value


class LibraryPage(Adw.PreferencesPage):
    """``host`` is the main window: ``backend``, ``add_to_queue``, ``toast``,
    ``library_changed`` and ``settings_changed``.
    """

    def __init__(self, host) -> None:
        super().__init__()
        self._host = host
        self._cancel: Cancellation | None = None
        self.last_summary: ScanSummary | None = None

        self.folders_group = Adw.PreferencesGroup(
            title="Library Folders",
            description=(
                "PirateFinder looks for disk images in these folders and in the zip and 7z "
                "archives inside them. Folders on a NAS work while they are mounted."
            ),
        )
        self.folders_group.set_header_suffix(
            text_button("_Add Folder…", self._on_add_folder, style="flat")
        )
        self.folder_rows = GroupRows(self.folders_group)
        self.add(self.folders_group)

        downloads = Adw.PreferencesGroup(title="Downloads")
        self.download_row = _row("Download Folder")
        self.download_row.set_subtitle_selectable(True)
        self.download_row.add_suffix(
            text_button("_Choose…", self._on_choose_download, tooltip="Choose a folder")
        )
        self.download_row.add_suffix(
            icon_button(
                "folder-open-symbolic",
                "Open the Download Folder",
                lambda _b: self._open_download_folder(),
            )
        )
        downloads.add(self.download_row)
        self.add(downloads)

        self.scan_group = Adw.PreferencesGroup(title="Scan")
        self.scan_button = text_button("_Scan Now", self._on_scan, style="flat")
        self.scan_group.set_header_suffix(self.scan_button)
        self.progress_row = _row("Scanning")
        self.progress_row.set_subtitle_lines(1)
        self.scan_progress = progress_bar()
        self.scan_progress.set_size_request(160, -1)
        self.progress_row.add_suffix(self.scan_progress)
        self.progress_row.add_suffix(
            icon_button("process-stop-symbolic", "Cancel Scan", lambda _b: self.cancel_scan())
        )
        self.progress_row.set_visible(False)
        self.scan_group.add(self.progress_row)
        self.counts: dict[str, Gtk.Label] = {}
        for key, title in (
            ("images", "Images"),
            ("matched", "Matched the Catalogue"),
            ("unmatched", "Not Matched"),
            ("duplicates", "Duplicates"),
            ("last_scan", "Last Scan"),
        ):
            row, value = _value_row(title)
            self.counts[key] = value
            self.scan_group.add(row)
        self.add(self.scan_group)

        self.unmatched_group = Adw.PreferencesGroup(
            title="Unmatched Files",
            description=(
                "Images that match no disk in the catalogue. They are still searched by file "
                "name, volume label and the files on the disk."
            ),
        )
        self.unmatched_rows = GroupRows(self.unmatched_group)
        self.add(self.unmatched_group)

        self.connect("map", lambda _page: self.refresh())

    # Refreshing

    @property
    def scanning(self) -> bool:
        return self._cancel is not None

    def refresh(self) -> None:
        settings = self._host.backend.settings
        self.folder_rows.clear()
        for folder in settings.library_folders:
            row = _row(Path(folder).name or folder, folder)
            row.add_suffix(
                icon_button(
                    "folder-open-symbolic", "Show in Files", lambda _b, f=folder: self._open(f)
                )
            )
            row.add_suffix(
                icon_button(
                    "user-trash-symbolic",
                    "Remove Folder",
                    lambda _b, f=folder: self.remove_folder(f),
                )
            )
            self.folder_rows.add(row)
        if not settings.library_folders:
            empty = _row(
                "No folders yet", "Add the folders that hold your Amiga and Atari ST images."
            )
            empty.add_css_class("dim-label")
            self.folder_rows.add(empty)
        self.download_row.set_subtitle(settings.download_folder)
        can_scan = self.has_folders()
        self.scan_button.set_sensitive(can_scan and not self.scanning)
        self.scan_button.set_tooltip_text(
            "Look for new and changed images in every folder"
            if can_scan
            else "Add a library folder to scan"
        )

        backend = self._host.backend

        def work() -> tuple[LibraryStats, list[LocalFile]]:
            return backend.library_stats(), backend.unmatched_files(UNMATCHED_LIMIT)

        run_in_thread(work, self._show_stats, lambda _error: None, name="library-stats")

    def _show_stats(self, result: tuple[LibraryStats, list[LocalFile]]) -> None:
        stats, unmatched = result
        self.counts["images"].set_text(f"{stats.images:,}")
        self.counts["matched"].set_text(f"{stats.matched:,}")
        self.counts["unmatched"].set_text(f"{stats.unmatched:,}")
        self.counts["duplicates"].set_text(f"{stats.duplicates:,}")
        self.counts["last_scan"].set_text(fmt.format_timestamp(stats.last_scan) or "Never")
        self.unmatched_rows.clear()
        for local in unmatched:
            row = _row(fmt.local_file_name(local), fmt.local_file_location(local))
            row.set_subtitle_lines(2)
            button = text_button("Add to Queue", None, style="flat")
            button.set_tooltip_text(f"Add {fmt.local_file_name(local)} to the queue")
            button.connect("clicked", lambda _b, file=local: self._queue_file(file))
            row.add_suffix(button)
            self.unmatched_rows.add(row)
        if stats.unmatched > len(unmatched):
            note = _row(f"Showing the first {len(unmatched):,} of {stats.unmatched:,}")
            note.set_subtitle("Search on the Find page to reach the others.")
            self.unmatched_rows.add(note)
        self.unmatched_group.set_visible(bool(unmatched))

    # Folders

    def has_folders(self) -> bool:
        """Whether a scan has anywhere to look: a library folder, or downloads so far."""
        settings = self._host.backend.settings
        download = settings.download_folder
        return bool(settings.library_folders) or bool(download and Path(download).is_dir())

    def _on_add_folder(self, _button) -> None:
        folders = self._host.backend.settings.library_folders
        initial = folders[-1] if folders else str(Path.home())
        choose_folder(self, "Add a Library Folder", initial, self.add_folder)

    def add_folder(self, folder: str) -> None:
        settings = self._host.backend.settings
        if folder in settings.library_folders:
            self._host.toast(f"{folder} is already a library folder")
            return
        settings.library_folders = [*settings.library_folders, folder]
        self._host.backend.save_settings()
        self._host.settings_changed("library_folders")
        self.refresh()
        self.scan()

    def remove_folder(self, folder: str) -> None:
        settings = self._host.backend.settings
        settings.library_folders = [item for item in settings.library_folders if item != folder]
        self._host.backend.save_settings()
        self._host.settings_changed("library_folders")
        self._host.toast(f"Removed {Path(folder).name or folder}. The files are not touched.")
        self.refresh()
        self.scan()

    def _on_choose_download(self, _button) -> None:
        current = self._host.backend.settings.download_folder
        choose_folder(self, "Choose the Download Folder", current, self.set_download_folder)

    def set_download_folder(self, folder: str) -> None:
        settings = self._host.backend.settings
        if settings.download_folder == folder:
            return
        settings.download_folder = folder
        self._host.backend.save_settings()
        self._host.settings_changed("download_folder")
        self.refresh()

    def _open_download_folder(self) -> None:
        folder = Path(self._host.backend.settings.download_folder)
        try:
            folder.mkdir(parents=True, exist_ok=True)
        except OSError as error:
            self._host.toast(f"The download folder could not be created: {error.strerror}")
            return
        self._open(str(folder))

    def _open(self, folder: str) -> None:
        # Show the folder itself selected in its parent.
        show_in_files(self, folder)

    def _queue_file(self, local: LocalFile) -> None:
        item = fmt.new_queue_item(fmt.local_file_name(local), platform_for_file(local), local=local)
        self._host.add_to_queue([item])

    # Scanning

    def _on_scan(self, _button) -> None:
        self.scan()

    def scan(self) -> None:
        if self.scanning:
            return
        if not self.has_folders():
            self._host.toast("Add a library folder first")
            return
        self._cancel = Cancellation()
        cancel = self._cancel
        self.scan_button.set_sensitive(False)
        self.progress_row.set_visible(True)
        self.progress_row.set_title("Scanning")
        self.progress_row.set_subtitle("Looking for images")
        set_fraction(self.scan_progress, None, "")
        latest = Latest(self._show_progress)
        backend = self._host.backend
        run_in_thread(
            lambda: backend.scan_library(latest.post, cancel),
            self._scan_done,
            self._scan_failed,
            name="library-scan",
        )

    def _show_progress(self, progress: ScanProgress) -> None:
        if self._cancel is None:
            return
        fraction = progress.done / progress.total if progress.total else None
        text = (
            f"{progress.done:,} of {progress.total:,}" if progress.total else f"{progress.done:,}"
        )
        set_fraction(self.scan_progress, fraction, text)
        self.progress_row.set_subtitle(progress.message or "Looking for images")

    def cancel_scan(self) -> None:
        if self._cancel is not None:
            self._cancel.cancel()
            self.progress_row.set_title("Stopping")

    def _scan_done(self, summary: ScanSummary) -> None:
        self._cancel = None
        self.last_summary = summary
        self.progress_row.set_visible(False)
        self._host.toast(fmt.scan_toast(summary))
        self._host.library_changed()
        self.refresh()

    def _scan_failed(self, error: BaseException) -> None:
        self._cancel = None
        self.progress_row.set_visible(False)
        self._host.toast(f"The scan failed: {error}")
        self.refresh()

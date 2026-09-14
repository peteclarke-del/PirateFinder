"""The Library page: folders scanned for images, the download folder and scans."""

from __future__ import annotations

from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gtk  # noqa: E402

from ..jobs.cancellation import Cancellation  # noqa: E402
from ..jobs.queue import item_from_local  # noqa: E402
from ..library.library import local_name  # noqa: E402
from ..models import BootRecheck, LocalFile, ScanSummary, VirusReport  # noqa: E402
from . import formatting as fmt  # noqa: E402
from .backend import ScanProgress  # noqa: E402
from .bridge import Latest, run_in_thread  # noqa: E402
from .log import LOG  # noqa: E402
from .widgets import (  # noqa: E402
    GroupRows,
    alert,
    choose_folder,
    icon_button,
    plain_row,
    progress_bar,
    set_fraction,
    show_in_files,
    status_icon,
    text_button,
)

UNMATCHED_LIMIT = 200
INFECTED_LIMIT = 50


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
        self._checking = False  # boot blocks are being checked again, not scanned
        self._scan_after_check = False  # a scan asked for while boot blocks are checked
        self.last_summary: ScanSummary | None = None

        self.folders_group = Adw.PreferencesGroup(
            title="Library Folders",
            description=(
                "PirateFinder looks for disk images in these folders and in the zip, 7z and LZH "
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
            ("infected", "With a Virus"),
            ("last_scan", "Last Scan"),
        ):
            row, value = _value_row(title)
            self.counts[key] = value
            self.scan_group.add(row)
        self.add(self.scan_group)

        self.virus_group = Adw.PreferencesGroup(
            title="Viruses Found",
            description=(
                "Boot block viruses found when the library was last checked. Cleaning rewrites the "
                "file with a standard boot block and keeps the original beside it as a backup."
            ),
            visible=False,
        )
        self.virus_rows = GroupRows(self.virus_group)
        self.infected: list[tuple[LocalFile, VirusReport | None]] = []
        self.clean_buttons: dict[tuple[str, str], Gtk.Button] = {}
        self.add(self.virus_group)

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

        def work():
            infected = []
            for local in backend.infected_files(INFECTED_LIMIT):
                try:
                    report = backend.virus_report(local)
                except Exception:  # noqa: BLE001 - the file may have gone; still list it
                    report = None
                infected.append((local, report))
            return backend.library_stats(), backend.unmatched_files(UNMATCHED_LIMIT), infected

        run_in_thread(work, self._show_stats, lambda _error: None, name="library-stats")

    def _show_stats(self, result) -> None:
        stats, unmatched, infected = result
        self._show_infected(infected)
        self.counts["images"].set_text(f"{stats.images:,}")
        self.counts["matched"].set_text(f"{stats.matched:,}")
        self.counts["unmatched"].set_text(f"{stats.unmatched:,}")
        self.counts["duplicates"].set_text(f"{stats.duplicates:,}")
        self.counts["infected"].set_text(f"{max(stats.infected, len(infected)):,}")
        self.counts["last_scan"].set_text(fmt.format_timestamp(stats.last_scan) or "Never")
        self.unmatched_rows.clear()
        for local in unmatched:
            row = _row(local_name(local), fmt.local_file_location(local))
            row.set_subtitle_lines(2)
            if local.virus:
                row.add_prefix(
                    status_icon("dialog-warning-symbolic", "error", f"Virus: {local.virus}")
                )
            button = text_button("Add to Queue", None, style="flat")
            button.set_tooltip_text(f"Add {local_name(local)} to the queue")
            button.connect("clicked", lambda _b, file=local: self._queue_file(file))
            row.add_suffix(button)
            self.unmatched_rows.add(row)
        if stats.unmatched > len(unmatched):
            note = _row(f"Showing the first {len(unmatched):,} of {stats.unmatched:,}")
            note.set_subtitle("Search on the Find page to reach the others.")
            self.unmatched_rows.add(note)
        self.unmatched_group.set_visible(bool(unmatched))

    # Viruses

    def _show_infected(self, infected: list[tuple[LocalFile, VirusReport | None]]) -> None:
        self.infected = list(infected)
        self.virus_rows.clear()
        self.clean_buttons = {}
        for local, report in infected:
            name = report.name if report is not None and report.name else local.virus
            removable = report is not None and report.removable
            parts = [f"Virus: {name}", fmt.local_file_location(local)]
            if not removable:
                parts.insert(1, "cannot be removed")
            row = _row(local_name(local), " • ".join(parts))
            row.set_subtitle_lines(3)
            row.add_prefix(status_icon("dialog-warning-symbolic", "error", f"Virus: {name}"))
            if removable:
                button = text_button("C_lean…", None, style="flat")
                button.set_tooltip_text(f"Remove {name} from {local_name(local)}")
                button.connect(
                    "clicked", lambda _b, file=local, what=name: self.confirm_clean(file, what)
                )
                row.add_suffix(button)
                self.clean_buttons[(local.path, local.member)] = button
            self.virus_rows.add(row)
        self.virus_group.set_visible(bool(infected))

    def confirm_clean(self, local: LocalFile, virus: str) -> None:
        def respond(response: str) -> None:
            if response == "clean":
                self.clean(local)

        alert(
            self,
            "Clean the Stored Image?",
            fmt.clean_explanation(local, virus),
            (("cancel", "_Cancel", ""), ("clean", "C_lean", "suggested")),
            respond,
        )

    def clean(self, local: LocalFile) -> None:
        backend = self._host.backend
        button = self.clean_buttons.get((local.path, local.member))
        if button is not None:
            button.set_sensitive(False)

        def done(cleaned: LocalFile) -> None:
            self._host.toast(fmt.cleaned_text(local, cleaned))
            self._host.library_changed()
            self.refresh()

        def failed(error: BaseException) -> None:
            if button is not None:
                button.set_sensitive(True)
            alert(self, "The File Could Not Be Cleaned", str(error))

        run_in_thread(lambda: backend.clean_file(local), done, failed, name="clean-file")

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
        self._host.add_to_queue([item_from_local(local)])

    # Scanning

    def _on_scan(self, _button) -> None:
        self.scan()

    def scan(self) -> None:
        if self.scanning:
            # A boot block check is quick next to a scan: the scan follows it.
            self._scan_after_check = self._checking
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

    def recheck_boot_blocks(self) -> None:
        """Check the library's boot blocks again when the virus data changed.

        Runs at start and after the brainfile is installed; the backend
        answers at once when nothing changed. The progress row shows only
        while boot blocks are read, and a scan waits for the check to end.
        """
        if self.scanning:
            return
        self._cancel = Cancellation()
        self._checking = True
        cancel = self._cancel
        self.scan_button.set_sensitive(False)
        self.progress_row.set_title("Checking Boot Blocks")

        def show(progress: ScanProgress) -> None:
            if self._cancel is cancel:
                self.progress_row.set_visible(True)
                self._show_progress(progress)

        latest = Latest(show)
        backend = self._host.backend
        run_in_thread(
            lambda: backend.recheck_boot_blocks(latest.post, cancel),
            self._recheck_done,
            self._recheck_failed,
            name="boot-recheck",
        )

    def _recheck_done(self, result: BootRecheck | None) -> None:
        self._recheck_ended()
        if result is not None:
            text = fmt.boot_recheck_text(result)
            LOG.add("library", text)
            if result.changed or result.unreadable:
                self._host.toast(text)
                self._host.library_changed()
        self.refresh()
        self._scan_if_asked()

    def _recheck_failed(self, error: BaseException) -> None:
        self._recheck_ended()
        self._host.toast(f"The boot blocks could not be checked again: {error}")
        self.refresh()
        self._scan_if_asked()

    def _recheck_ended(self) -> None:
        self._cancel = None
        self._checking = False
        self.progress_row.set_visible(False)

    def _scan_if_asked(self) -> None:
        if self._scan_after_check:
            self._scan_after_check = False
            self.scan()

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

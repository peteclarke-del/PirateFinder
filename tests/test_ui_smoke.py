"""Drive the real window with the fake backend.

Each test builds a MainWindow over a FakeBackend, works it the way a person
would (typing, ticking, pressing buttons, answering the insert prompt) and
checks what the window and the backend end up with. Needs a display; skipped
without one. Every wait is bounded.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import time
import unittest
from pathlib import Path

# Settings, queue and history belong to whoever runs the tests.
_HOME = Path(tempfile.mkdtemp(prefix="piratefinder-ui-test-"))
for _variable in ("XDG_CONFIG_HOME", "XDG_DATA_HOME"):
    os.environ[_variable] = str(_HOME / _variable.lower())

from tests.gtk_support import require_pygobject  # noqa: E402

require_pygobject()

import gi  # noqa: E402

from piratefinder.ui.application import PirateFinderApplication  # noqa: E402

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, GLib, Gtk  # noqa: E402

from piratefinder.models import (  # noqa: E402
    Availability,
    DeviceStatus,
    DiskKind,
    Platform,
    WriteStatus,
)
from piratefinder.settings import Settings  # noqa: E402
from piratefinder.ui import formatting as fmt  # noqa: E402
from piratefinder.ui.backend import UpdateOffer  # noqa: E402
from piratefinder.ui.fake_backend import FakeBackend  # noqa: E402
from piratefinder.ui.help_content import HELP_TOPICS  # noqa: E402

HAVE_DISPLAY = bool(Gtk.init_check()) and Gdk.Display.get_default() is not None
TIMEOUT = 10.0


def pump(seconds: float = 0.0) -> None:
    context = GLib.MainContext.default()
    deadline = time.monotonic() + seconds
    while True:
        while context.pending():
            context.iteration(False)
        if time.monotonic() >= deadline:
            return
        time.sleep(0.01)


def wait_until(predicate, message: str, timeout: float = TIMEOUT) -> None:
    context = GLib.MainContext.default()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        while context.pending():
            context.iteration(False)
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError(f"Timed out waiting: {message}")


def row_titles(list_box: Gtk.ListBox) -> list[str]:
    titles = []
    child = list_box.get_first_child()
    while child is not None:
        titles.append(child.get_title())
        child = child.get_next_sibling()
    return titles


@unittest.skipUnless(HAVE_DISPLAY, "needs a display")
class WindowTests(unittest.TestCase):
    app: PirateFinderApplication

    @classmethod
    def setUpClass(cls) -> None:
        cls.app = PirateFinderApplication(
            "com.github.pclarke.PirateFinderTests", unique=False, backend_factory=FakeBackend
        )
        cls.app.register(None)

    def setUp(self) -> None:
        from piratefinder.ui.window import MainWindow

        self.folder = Path(tempfile.mkdtemp(dir=_HOME))
        self.settings = Settings(path=self.folder / "settings.json")
        self.settings.download_folder = str(self.folder / "downloads")
        self.backend = FakeBackend(self.settings)
        self.window = MainWindow(application=self.app, backend=self.backend)
        self.window.present()
        wait_until(lambda: self.window.device is not None, "the first device probe")

    def tearDown(self) -> None:
        window = self.window
        if window.queue_page.running:
            window.queue_page.cancel_session()
            wait_until(lambda: not window.queue_page.running, "the session to stop")
        while (dialog := window.get_visible_dialog()) is not None:
            dialog.force_close()
            pump()
        window.close()
        pump(0.05)

    # Helpers

    def search(self, text: str, count: int) -> None:
        page = self.window.find_page
        page.search_entry.set_text(text)
        wait_until(
            lambda: not page.searching and len(page.results) == count,
            f"{count} results for {text!r}",
        )

    def open_first_result(self) -> None:
        page = self.window.find_page
        page.selection.set_selected(0)
        wait_until(
            lambda: page.detail.detail is not None or page.detail.local is not None,
            "the detail pane",
        )

    def answer_prompt(self, response: str, heading: str = "", reason: str = "") -> None:
        page = self.window.queue_page

        def ready() -> bool:
            dialog = page.insert_dialog
            if dialog is None:
                return False
            return heading in dialog.get_heading() and reason in dialog.get_body()

        wait_until(ready, f"an insert prompt {heading!r} {reason!r}")
        self.assertTrue(page.answer_insert_prompt(response))

    # Find

    def test_search_tick_two_rows_and_add_them_to_the_queue(self) -> None:
        page = self.window.find_page
        self.assertEqual(page.stack.get_visible_child_name(), "welcome")
        self.search("automation", 3)
        self.assertEqual(page.stack.get_visible_child_name(), "results")
        self.assertEqual(page.count_label.get_text(), "3 results")
        wait_until(lambda: len(page._rows) >= 3, "rows to be drawn")
        rows = sorted(page._rows, key=lambda row: row.result.disk.id)
        self.assertEqual(rows[0].title.get_text(), "Automation 250")
        rows[0].check.set_active(True)
        rows[1].check.set_active(True)
        self.assertTrue(page.action_bar.get_revealed())
        self.assertEqual(page.selected_label.get_text(), "2 selected")

        page.add_checked_button.emit("clicked")
        self.assertEqual(
            [item.label for item in self.backend.queue_items()],
            ["Automation 250", "Automation 251"],
        )
        self.assertEqual(self.window.pages["queue"].get_badge_number(), 2)
        self.assertFalse(page.action_bar.get_revealed())
        self.assertFalse(any(row.check.get_active() for row in page._rows))

        # The same disk is never queued twice.
        self.window.add_to_queue([fmt.new_queue_item("Automation 250", None, disk_id=1)])
        self.assertEqual(len(self.backend.queue_items()), 2)

    def test_matched_titles_are_bold_in_the_result_row(self) -> None:
        page = self.window.find_page
        self.search("rick", 2)
        wait_until(lambda: len(page._rows) >= 2, "rows to be drawn")
        markup = {row.title.get_text(): row.summary.get_label() for row in page._rows}
        self.assertIn("<b>Rick Dangerous</b>", markup["Pompey Pirates 51"])

    def test_filters_narrow_the_results_and_can_be_cleared(self) -> None:
        page = self.window.find_page
        self.search("rick", 2)
        page.platform_dropdown.set_selected(1)  # Amiga
        wait_until(lambda: not page.searching and len(page.results) == 1, "Amiga only")
        self.assertEqual(page.results[0].disk.label, "Skid Row Compact 12")
        page.kind_buttons[next(iter(page.kind_buttons))].set_active(True)
        page.available_button.set_active(True)
        wait_until(lambda: not page.searching, "search with filters")
        page.search_entry.set_text("zzkq")
        wait_until(lambda: page.stack.get_visible_child_name() == "empty", "no results")
        self.assertTrue(page.clear_filters_button.get_visible())
        page.clear_filters_button.emit("clicked")
        self.assertFalse(page.filters_active())

    def test_kind_buttons_count_every_kind_and_filter_without_searching_again(self) -> None:
        page = self.window.find_page
        calls: list[str] = []
        search = self.backend.search
        self.backend.search = lambda text, filters: calls.append(text) or search(text, filters)
        self.search("blood", 2)  # one menu disk and one compilation
        counts = {kind: label.get_text() for kind, label in page.kind_counts.items()}
        self.assertEqual(counts[DiskKind.MENU], "1")
        self.assertEqual(counts[DiskKind.COMPILATION], "1")
        self.assertEqual(counts[DiskKind.SINGLE], "0")
        searches = len(calls)
        page.kind_buttons[DiskKind.COMPILATION].set_active(True)
        pump(0.1)
        self.assertEqual([r.disk.kind for r in page.results], [DiskKind.COMPILATION])
        self.assertEqual(page.count_label.get_text(), "1 result")
        self.assertEqual(counts[DiskKind.MENU], page.kind_counts[DiskKind.MENU].get_text())
        self.assertEqual(len(calls), searches, "switching kinds must not search again")
        page.kind_buttons[DiskKind.COMPILATION].set_active(False)
        pump(0.1)
        self.assertEqual(len(page.results), 2)

    def test_a_slow_search_never_replaces_a_newer_one(self) -> None:
        page = self.window.find_page
        self.backend.search_delay = 0.3
        page.search_entry.set_text("automation")
        page.search()
        page.search_entry.set_text("speedball")
        page.search()
        wait_until(lambda: not page.searching, "the searches to finish")
        pump(0.5)
        self.assertEqual([result.disk.label for result in page.results], ["Speedball 2"])

    def test_detail_pane_writes_the_chosen_dump(self) -> None:
        page = self.window.find_page
        self.search("pompey 51", 1)
        self.open_first_result()
        detail = page.detail
        self.assertEqual(detail.title.get_text(), "Pompey Pirates 51")
        self.assertTrue(page.split_view.get_show_sidebar())
        # Best Available, the main dump and the [a] alternate.
        self.assertEqual(len(detail.dump_rows.rows), 3)
        detail.dump_rows.rows[2].activate()
        detail.add_button.emit("clicked")
        (item,) = self.backend.queue_items()
        self.assertEqual((item.disk_id, item.image_id), (4, 41))
        detail.close_button.emit("clicked")
        self.assertFalse(page.split_view.get_show_sidebar())

    def test_download_only_makes_an_online_disk_local(self) -> None:
        page = self.window.find_page
        self.search("automation 251", 1)
        self.assertEqual(page.results[0].availability, Availability.ONLINE)
        self.open_first_result()
        actions = page.detail._actions
        self.assertTrue(actions.lookup_action("download").get_enabled())
        self.assertFalse(actions.lookup_action("show-in-files").get_enabled())
        actions.activate_action("download", None)
        wait_until(lambda: 2 in self.backend.local_disks, "the download")
        wait_until(
            lambda: page.results and page.results[0].availability == Availability.LOCAL,
            "the result to show the disk as local",
        )
        wait_until(lambda: not page.detail.downloading, "the download bar to go")

    def test_no_catalogue_shows_only_the_update_page(self) -> None:
        from piratefinder.ui.window import MainWindow

        backend = FakeBackend(Settings(path=None), catalogue=False)
        window = MainWindow(application=self.app, backend=backend)
        window.present()
        pump(0.1)
        try:
            self.assertEqual(window.find_page.stack.get_visible_child_name(), "no-catalogue")
            self.assertFalse(window.find_page.search_area.get_visible())
        finally:
            window.close()
            pump(0.05)

    # Writing

    def queue_two(self) -> None:
        self.backend.queue_add(
            [
                fmt.new_queue_item("Automation 250", Platform.ATARI_ST, disk_id=1),
                fmt.new_queue_item("Automation 251", Platform.ATARI_ST, disk_id=2),
            ]
        )
        self.window.queue_page.refresh()

    def test_a_session_with_prompts_ends_in_a_summary(self) -> None:
        self.backend.script.protect_once = {"Automation 250"}
        self.backend.script.fail_labels = {"Automation 251"}
        self.queue_two()
        page = self.window.queue_page
        self.assertEqual(page.get_visible_child_name(), "list")
        self.window.show_page("queue")
        page.start_button.emit("clicked")

        self.answer_prompt("write", "Insert a Disk for Automation 250", "Disk 1 of 2, drive A.")
        self.assertEqual(page.get_visible_child_name(), "running")
        self.assertEqual(page.running_page.get_title(), "Writing Disk 1 of 2")
        self.answer_prompt("write", "Automation 250", "write-protected")
        self.answer_prompt("write", "Insert a Disk for Automation 251", "Disk 2 of 2")
        wait_until(lambda: page.get_visible_child_name() == "summary", "the summary")

        self.assertEqual(row_titles(page.summary_list), ["Automation 250", "Automation 251"])
        self.assertEqual(page.summary_page.get_description(), "1 of 2 disks written and verified")
        self.assertEqual(page.summary_page.get_title(), "Some Disks Not Written")
        self.assertTrue(page.retry_button.get_visible())
        self.assertIn("of 160 track sides", page.track_label.get_text())
        # The written disk leaves the queue; the failed one stays for another try.
        self.assertEqual([item.label for item in self.backend.queue_items()], ["Automation 251"])
        self.assertEqual(self.window.pages["queue"].get_badge_number(), 1)
        self.assertEqual(len(self.backend.history()), 1)
        report = self.backend.report_text(page.summary)
        self.assertIn("Automation 251: Failed", report)

        self.backend.script.fail_labels = set()
        page.retry_button.emit("clicked")
        self.answer_prompt("write", "Automation 251")
        wait_until(
            lambda: (
                page.get_visible_child_name() == "summary"
                and page.summary is not None
                and len(page.summary.items) == 1
            ),
            "the retry summary",
        )
        self.assertEqual(page.summary_page.get_title(), "All Disks Written")
        self.assertEqual(self.backend.queue_items(), [])
        page.done_button.emit("clicked")
        self.assertEqual(page.get_visible_child_name(), "empty")

    def test_stop_at_the_prompt_leaves_the_rest_in_the_queue(self) -> None:
        self.queue_two()
        page = self.window.queue_page
        page.start_button.emit("clicked")
        self.answer_prompt("stop", "Automation 250")
        wait_until(lambda: page.get_visible_child_name() == "summary", "the summary")
        statuses = [outcome.status for _label, outcome, _source in page.summary.items]
        self.assertEqual(statuses, [WriteStatus.SKIPPED, WriteStatus.SKIPPED])
        self.assertEqual(len(self.backend.queue_items()), 2)
        self.assertFalse(page.retry_button.get_visible())

    def test_cancel_while_writing(self) -> None:
        self.backend.script.track_delay = 0.01
        page = self.window.queue_page
        self.window.write_now([fmt.new_queue_item("Automation 250", Platform.ATARI_ST, disk_id=1)])
        self.answer_prompt("write", "Automation 250")
        wait_until(lambda: page.progress.get_fraction() > 0.1, "writing to start")
        page.cancel_session()
        wait_until(lambda: page.get_visible_child_name() == "summary", "the summary")
        (_label, outcome, _source), *_ = page.summary.items
        self.assertEqual(outcome.status, WriteStatus.CANCELLED)
        self.assertEqual(self.backend.queue_items(), [], "Write Now does not queue disks")

    def test_no_device_shows_the_banner_and_asks_before_writing(self) -> None:
        self.backend.device = DeviceStatus(False, "No Greaseweazle was found.")
        self.window.probe_device()
        wait_until(lambda: self.window.banner.get_revealed(), "the banner")
        self.window.write_now([fmt.new_queue_item("Automation 250", Platform.ATARI_ST, disk_id=1)])
        wait_until(lambda: self.window.get_visible_dialog() is not None, "the alert")
        dialog = self.window.get_visible_dialog()
        self.assertEqual(dialog.get_heading(), "No Greaseweazle Connected")
        self.assertFalse(self.window.queue_page.running)

        self.backend.device = DeviceStatus(True, "Greaseweazle F7", "F7")
        dialog.emit("response", "check")
        dialog.force_close()
        self.answer_prompt("stop", "Automation 250")
        wait_until(lambda: not self.window.queue_page.running, "the session to end")
        wait_until(lambda: not self.window.banner.get_revealed(), "the banner to go")

    # Library, history and preferences

    def test_adding_a_library_folder_saves_it_and_scans(self) -> None:
        page = self.window.library_page
        folder = str(self.folder / "images")
        page.add_folder(folder)
        self.assertEqual(self.settings.library_folders, [folder])
        saved = json.loads(Path(self.settings.path).read_text(encoding="utf-8"))
        self.assertEqual(saved["library_folders"], [folder])
        wait_until(lambda: page.last_summary is not None, "the scan")
        self.assertIn("matched the catalogue", fmt.scan_toast(page.last_summary))
        self.assertEqual(len(page.folder_rows.rows), 1)
        page.remove_folder(folder)
        self.assertEqual(self.settings.library_folders, [])
        wait_until(lambda: not page.scanning, "the rescan")

    def test_history_lists_past_sessions(self) -> None:
        self.queue_two()
        self.window.queue_page.start_button.emit("clicked")
        self.answer_prompt("skip", "Automation 250")
        self.answer_prompt("skip", "Automation 251")
        wait_until(lambda: not self.window.queue_page.running, "the session to end")
        self.window.show_page("history")
        page = self.window.history_page
        wait_until(lambda: len(page.sessions) == 1, "the history page")
        self.assertEqual(page.get_visible_child_name(), "sessions")

    def test_preferences_save_every_setting(self) -> None:
        dialog = self.window.show_preferences()
        pump(0.1)
        self.assertEqual(set(dialog.provider_rows), {"internet-archive", "atari-legend", "d-bug"})

        dialog.provider_rows["atari-legend"].set_active(False)
        self.assertFalse(self.settings.providers["atari-legend"])
        saved = json.loads(Path(self.settings.path).read_text(encoding="utf-8"))
        self.assertIs(saved["providers"]["atari-legend"], False)

        dialog.retries_row.set_value(5)
        dialog.erase_row.set_active(True)
        dialog.prompt_row.set_active(False)
        dialog.check_updates_row.set_active(False)
        dialog.drive_row.set_selected(1)
        dialog.device_entry.set_text("/dev/ttyACM1")
        dialog.online_row.set_active(False)
        self.assertFalse(dialog.provider_rows["d-bug"].get_sensitive())
        wait_until(lambda: self.settings.device == "/dev/ttyACM1", "the device path")
        saved = json.loads(Path(self.settings.path).read_text(encoding="utf-8"))
        self.assertEqual(
            (saved["retries"], saved["pre_erase"], saved["prompt_between_disks"]),
            (5, True, False),
        )
        self.assertEqual((saved["drive"], saved["device"]), ("B", "/dev/ttyACM1"))
        self.assertIs(saved["check_catalogue_updates"], False)
        self.assertIs(saved["online_enabled"], False)
        # The Queue page shows the same drive.
        self.assertEqual(self.window.queue_page.drive_row.get_selected(), 1)
        dialog.force_close()
        pump(0.05)

        # Online downloads off: a disk only available online is now missing.
        self.search("automation 251", 1)
        self.assertEqual(self.window.find_page.results[0].availability, Availability.MISSING)

    def test_update_catalogue_installs_and_refreshes_the_welcome_page(self) -> None:
        self.backend.update_offer = UpdateOffer("2026-09-10", 5_000_000)
        self.window.update_catalogue()
        updater = self.window.updater
        wait_until(lambda: updater.state.phase == "available", "the update check")
        updater.install(updater.state.offer)
        wait_until(lambda: updater.state.phase == "done", "the update")
        self.assertIn("10 Sep 2026", self.window.find_page.welcome.get_description())

    def test_menu_items_open_their_windows(self) -> None:
        window = self.window
        for action, accelerator in (
            ("win.focus-search", "<Control>f"),
            ("win.help", "F1"),
            ("win.preferences", "<Control>comma"),
            ("win.write-selected", "<Control>Return"),
            ("app.quit", "<Control>q"),
        ):
            self.assertIn(accelerator, self.app.get_accels_for_action(action))
        window.activate_action("win.help", None)
        help_window = window._help_window
        self.assertTrue(help_window.get_visible())
        rows = 0
        while help_window.help_view.topic_list.get_row_at_index(rows) is not None:
            rows += 1
        self.assertEqual(rows, len(HELP_TOPICS))
        help_window.help_view.show_topic("troubleshooting")
        help_window.close()

        window.activate_action("win.about", None)
        pump(0.05)
        self.assertIsInstance(window.get_visible_dialog(), Adw.AboutDialog)
        window.get_visible_dialog().force_close()
        window.activate_action("win.diagnostic-log", None)
        pump(0.05)
        self.assertEqual(window.get_visible_dialog().get_title(), "Diagnostic Log")
        window.get_visible_dialog().force_close()
        window.activate_action("win.show-page", GLib.Variant("s", "library"))
        self.assertEqual(window.stack.get_visible_child_name(), "library")
        window.activate_action("win.focus-search", None)
        self.assertEqual(window.stack.get_visible_child_name(), "find")


def tearDownModule() -> None:
    shutil.rmtree(_HOME, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()

"""The main window: header with page switcher, the four pages, banner and toasts."""

from __future__ import annotations

from collections.abc import Callable, Sequence

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gio, GLib, Gtk  # noqa: E402

from .. import __version__  # noqa: E402
from ..branding import APPLICATION_ID, APPLICATION_NAME, HOMEPAGE  # noqa: E402
from ..greaseweazle.client import NOT_CONNECTED  # noqa: E402
from ..images.virus import BRAINFILE_PROJECT  # noqa: E402
from ..jobs.cancellation import Cancellation  # noqa: E402
from ..jobs.queue import queue_key  # noqa: E402
from ..models import DeviceStatus, QueueItem, SessionSummary  # noqa: E402
from . import formatting as fmt  # noqa: E402
from .backend import Backend, CatalogueInfo, Downloaded, UpdateOffer  # noqa: E402
from .bridge import Latest, run_in_thread  # noqa: E402
from .diagnostics import DiagnosticLogDialog, shortcuts_window  # noqa: E402
from .find_page import FindPage  # noqa: E402
from .help_content import DATA_CREDITS, VIRUS_CREDITS  # noqa: E402
from .help_view import HelpWindow  # noqa: E402
from .history_page import HistoryPage  # noqa: E402
from .library_page import LibraryPage  # noqa: E402
from .log import LOG  # noqa: E402
from .preferences import PreferencesDialog  # noqa: E402
from .queue_page import QueuePage  # noqa: E402
from .updater import CatalogueUpdater  # noqa: E402
from .widgets import alert, install_style  # noqa: E402

# The file-only presence check runs this often. It starts no program and makes
# no network request; gw info runs only when a device appears (see
# follow_device), on Retry and on Check Connection.
DEVICE_CHECK_SECONDS = 2
# gw info runs at most this many times for one arrival of the device: once at
# once, and once more at the next check when the first found nothing, in case
# the serial port was not ready yet.
ARRIVAL_PROBES = 2
NO_DEVICE_TEXT = "No Greaseweazle connected. You can still search and download."


class MainWindow(Adw.ApplicationWindow):
    """The PirateFinder window. Pages call back into it as their ``host``."""

    def __init__(self, *, application: Adw.Application, backend: Backend) -> None:
        super().__init__(
            application=application,
            title=APPLICATION_NAME,
            default_width=1280,
            default_height=800,
        )
        self.set_size_request(360, 480)
        self.backend = backend
        self.updater = CatalogueUpdater(self)
        self._info = self._load_catalogue_info()
        self._device: DeviceStatus | None = None
        self._probing = False
        self._probe_waiters: list[Callable[[DeviceStatus], None]] = []
        self._probe_source = 0
        self._present: bool | None = None  # the last file-only answer; None before the first
        self._arrival_probes = 0
        self._preferences: PreferencesDialog | None = None
        self._help_window: HelpWindow | None = None
        self._close_after_session = False
        self._closing = False
        self.pages: dict[str, Adw.ViewStackPage] = {}

        self._build()
        self._create_actions()
        self.find_page.set_catalogue(self._info)
        self.connect("close-request", self._on_close_request)
        GLib.idle_add(self._start_up)

    # Construction

    def _load_catalogue_info(self) -> CatalogueInfo:
        try:
            return self.backend.catalogue_info()
        except Exception as error:  # noqa: BLE001 - the window must still open
            LOG.add("catalogue", f"Could not read the catalogue: {error}")
            return CatalogueInfo(False, error=str(error))

    def _build(self) -> None:
        install_style()
        self.toast_overlay = Adw.ToastOverlay()
        self.set_content(self.toast_overlay)
        toolbar = Adw.ToolbarView()
        self.toast_overlay.set_child(toolbar)

        self.header = Adw.HeaderBar()
        self.stack = Adw.ViewStack(vexpand=True)
        self.switcher = Adw.ViewSwitcher(stack=self.stack, policy=Adw.ViewSwitcherPolicy.WIDE)
        self.header.set_title_widget(self.switcher)
        self.menu_button = Gtk.MenuButton(
            icon_name="open-menu-symbolic",
            menu_model=self._main_menu(),
            tooltip_text="Main Menu",
            primary=True,
        )
        self.menu_button.update_property([Gtk.AccessibleProperty.LABEL], ["Main Menu"])
        self.header.pack_end(self.menu_button)
        toolbar.add_top_bar(self.header)

        self.banner = Adw.Banner(title=NO_DEVICE_TEXT, button_label="_Retry", revealed=False)
        self.banner.connect("button-clicked", lambda _banner: self.probe_device())
        toolbar.add_top_bar(self.banner)
        toolbar.set_content(self.stack)
        self.switcher_bar = Adw.ViewSwitcherBar(stack=self.stack)
        toolbar.add_bottom_bar(self.switcher_bar)

        self.find_page = FindPage(self)
        self.queue_page = QueuePage(self)
        self.library_page = LibraryPage(self)
        self.history_page = HistoryPage(self)
        for widget, name, title, icon in (
            (self.find_page, "find", "Find", "system-search-symbolic"),
            (self.queue_page, "queue", "Queue", "view-list-symbolic"),
            (self.library_page, "library", "Library", "folder-symbolic"),
            (self.history_page, "history", "History", "document-open-recent-symbolic"),
        ):
            self.pages[name] = self.stack.add_titled_with_icon(widget, name, title, icon)
        self.stack.connect("notify::visible-child-name", self._on_page_changed)
        self.queue_changed(len(self.backend.queue_items()))

        # Below 900 the details pane lies over the results instead of beside
        # them, and the table drops the columns it can best do without.
        columns = self.find_page.table.columns
        narrow = Adw.Breakpoint.new(Adw.BreakpointCondition.parse("max-width: 900sp"))
        narrow.add_setter(self.find_page.split_view, "collapsed", True)
        narrow.add_setter(columns["type"], "visible", False)
        self.add_breakpoint(narrow)
        phone = Adw.Breakpoint.new(Adw.BreakpointCondition.parse("max-width: 600sp"))
        phone.add_setter(self.find_page.split_view, "collapsed", True)
        phone.add_setter(self.switcher_bar, "reveal", True)
        phone.add_setter(
            self.header, "title-widget", Adw.WindowTitle(title=APPLICATION_NAME, subtitle="")
        )
        for column_id in ("type", "platform", "crew", "availability"):
            phone.add_setter(columns[column_id], "visible", False)
        # On a phone the details cover the whole width.
        split = self.find_page.split_view
        phone.add_setter(split, "sidebar-width-fraction", 1.0)
        phone.add_setter(split, "max-sidebar-width", 600.0)
        phone.add_setter(split, "min-sidebar-width", 200.0)
        phone.add_setter(self.find_page.pager.size_box, "visible", False)
        self.add_breakpoint(phone)

    def _main_menu(self) -> Gio.Menu:
        menu = Gio.Menu()
        first = Gio.Menu()
        first.append("_Preferences", "win.preferences")
        first.append("_Update Catalogue…", "win.update-catalogue")
        first.append("_Scan Library", "win.scan-library")
        menu.append_section(None, first)
        second = Gio.Menu()
        second.append("_Diagnostic Log", "win.diagnostic-log")
        second.append("_Keyboard Shortcuts", "win.shortcuts")
        second.append("User _Guide", "win.help")
        second.append(f"_About {APPLICATION_NAME}", "win.about")
        menu.append_section(None, second)
        return menu

    def _create_actions(self) -> None:
        callbacks: dict[str, Callable[[], None]] = {
            "focus-search": self.focus_search,
            "preferences": self.show_preferences,
            "update-catalogue": self.update_catalogue,
            "scan-library": self.scan_library,
            "diagnostic-log": self.show_diagnostic_log,
            "shortcuts": self.show_shortcuts,
            "help": self.show_help,
            "about": self.show_about,
            "write-selected": self.write_selected,
            "add-selected": self.add_selected,
            "retry-device": self.probe_device,
            "install-update": self.install_offered_update,
        }
        self.actions: dict[str, Gio.SimpleAction] = {}
        for name, callback in callbacks.items():
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", lambda _action, _parameter, run=callback: run())
            self.add_action(action)
            self.actions[name] = action
        show_page = Gio.SimpleAction.new("show-page", GLib.VariantType.new("s"))
        show_page.connect(
            "activate", lambda _action, parameter: self.show_page(parameter.get_string())
        )
        self.add_action(show_page)
        self.actions["show-page"] = show_page

    def _start_up(self) -> bool:
        self.follow_device()
        self._probe_source = GLib.timeout_add_seconds(DEVICE_CHECK_SECONDS, self._poll_device)
        if self.backend.settings.check_catalogue_updates:
            self.updater.check(self._offer_update)
        self.recheck_boot_blocks()
        return GLib.SOURCE_REMOVE

    # Pages

    def show_page(self, name: str) -> None:
        if self.stack.get_child_by_name(name) is not None:
            self.stack.set_visible_child_name(name)

    def _on_page_changed(self, _stack, _property) -> None:
        name = self.stack.get_visible_child_name()
        page = self.pages.get(name)
        if page is not None:
            page.set_needs_attention(False)

    def close_detail(self) -> None:
        self.find_page.close_detail()

    def focus_search(self) -> None:
        self.show_page("find")
        self.find_page.focus_search()

    def toast(self, message: str, *, button: str = "", action: str = "", target=None) -> None:
        toast = Adw.Toast(title=fmt.escape(message), timeout=5)
        if button and action:
            toast.set_button_label(button)
            toast.set_action_name(action)
            if target is not None:
                toast.set_action_target_value(target)
        self.toast_overlay.add_toast(toast)

    def add_toast(self, toast: Adw.Toast) -> None:
        self.toast_overlay.add_toast(toast)

    # Things pages ask of the window

    def catalogue_info(self) -> CatalogueInfo:
        return self._info

    def provider_names(self) -> dict[str, str]:
        return dict(self._info.providers)

    def source_names(self) -> dict[str, str]:
        """Display names of every catalogue source and provider, by id."""
        names = {
            str(source.get("id")): str(source.get("name"))
            for source in self._info.sources
            if source.get("id") and source.get("name")
        }
        names.update(self._info.providers)
        return names

    def selection_changed(self, _count: int) -> None:
        pass

    def queue_changed(self, count: int) -> None:
        page = self.pages.get("queue")
        if page is not None:
            page.set_badge_number(count)

    def settings_changed(self, name: str) -> None:
        self.backend.settings_changed()
        if name == "drive":
            self.queue_page.refresh_settings()
            if self._preferences is not None:
                from .queue_page import set_drive_row

                set_drive_row(self._preferences.drive_row, self.backend.settings.drive)
        elif name in ("library_folders", "download_folder"):
            self.library_page.refresh()
        elif name in ("providers", "online_enabled"):
            self.find_page.refresh()
            self.find_page.reload_detail()
        elif name == "fetch_media":
            self.find_page.detail.forget_cached()
            self.find_page.reload_detail()
        elif name == "device":
            # Another port: judge it afresh, as if the device had just appeared.
            self._device = None
            self._present = None
            self.follow_device()

    def library_changed(self) -> None:
        """Library files changed: search again and reload the details."""
        self.find_page.refresh()
        self.find_page.reload_detail()

    def details_changed(self) -> None:
        """The user corrected a disc or reverted it: the search, the details and the
        crew and year filters, which count discs by their corrected values, load again."""
        self.find_page.reload_facets()
        self.library_changed()

    def session_running(self) -> bool:
        return self.queue_page.running

    # Queueing and writing

    def add_to_queue(self, items: Sequence[QueueItem]) -> None:
        # A disk, or an unmatched file, is queued at most once.
        queued = {queue_key(item) for item in self.backend.queue_items()}
        new = []
        for item in items:
            if queue_key(item) not in queued:
                queued.add(queue_key(item))
                new.append(item)
        if not new:
            label = items[0].label if len(items) == 1 else "Those disks"
            verb = "is" if len(items) == 1 else "are"
            self.toast(f"{label} {verb} already in the queue")
            return
        self.queue_page.add(new)
        text = (
            f"Added {new[0].label} to the queue"
            if len(new) == 1
            else f"Added {fmt.plural(len(new), 'disk')} to the queue"
        )
        skipped = len(items) - len(new)
        if skipped:
            text += (
                f". {fmt.plural(skipped, 'disk')} {'was' if skipped == 1 else 'were'} there already"
            )
        if self.stack.get_visible_child_name() != "queue":
            self.toast(
                text, button="Show Queue", action="win.show-page", target=GLib.Variant("s", "queue")
            )

    def write_now(self, items: Sequence[QueueItem]) -> None:
        if self.queue_page.running:
            self.toast("Disks are already being written. Add these to the queue instead.")
            return
        self.show_page("queue")
        self.queue_page.start(list(items), from_queue=False)

    def write_selected(self) -> None:
        items = self.find_page.selected_items()
        if items:
            self.write_now(items)
        else:
            self.toast("Tick some disks on the Find page first")

    def add_selected(self) -> None:
        items = self.find_page.selected_items()
        if items:
            self.add_to_queue(items)
            self.find_page.clear_checked()
        else:
            self.toast("Tick some disks on the Find page first")

    def ensure_device(self, then: Callable[[], None]) -> None:
        """Run ``then`` once a Greaseweazle is known to be connected.

        The last answer is used, waiting for a check in progress; gw info runs
        again only when the user chooses Check Again.
        """

        def checked(status: DeviceStatus) -> None:
            if status.connected:
                then()
                return

            def respond(response: str) -> None:
                if response == "check":
                    self.probe_device(checked)

            alert(
                self,
                "No Greaseweazle Connected",
                f"{status.message}\n\nConnect the Greaseweazle and the drive, then choose "
                "Check Again.",
                (("cancel", "_Cancel", ""), ("check", "C_heck Again", "suggested")),
                respond,
            )

        if self._probing:
            self._probe_waiters.append(checked)
        else:
            checked(self._device or DeviceStatus(False, NOT_CONNECTED))

    def session_started(self) -> None:
        self.show_page("queue")

    def session_finished(self, summary: SessionSummary) -> None:
        self.history_page.refresh()
        self.find_page.refresh()
        if self.stack.get_visible_child_name() != "queue":
            self.pages["queue"].set_needs_attention(True)
            self.toast(
                fmt.session_headline(summary),
                button="Show Summary",
                action="win.show-page",
                target=GLib.Variant("s", "queue"),
            )
        if self._close_after_session:
            self._close_after_session = False
            self.close()
            return
        self.follow_device()

    def download(self, item: QueueItem, on_progress, on_done) -> Cancellation:
        """Fetch one disk into the download folder without writing it."""
        cancel = Cancellation()
        latest = Latest(on_progress)
        backend = self.backend

        def done(result: Downloaded) -> None:
            on_done(True)
            self.find_page.refresh()
            self.find_page.reload_detail()
            if not result.notes:
                self.toast(f"Downloaded {item.label} to {result.path}")
                return
            # A note, such as a download that could not be checked, stays until read.
            for note in result.notes:
                LOG.add("download", f"{item.label}: {note}")
            notes = "\n\n".join(result.notes)
            alert(self, f"Downloaded {item.label}", f"Saved to {result.path}.\n\n{notes}")

        def failed(error: BaseException) -> None:
            on_done(False)
            message = "Download cancelled" if cancel.cancelled else f"{item.label}: {error}"
            self.toast(message)

        run_in_thread(
            lambda: backend.download(item, latest.post, cancel), done, failed, name="download"
        )
        return cancel

    # The Greaseweazle

    def probe_device(self, then: Callable[[DeviceStatus], None] | None = None) -> None:
        """Identify the Greaseweazle with gw info: for Retry, Check Connection and arrivals."""
        if then is not None:
            self._probe_waiters.append(then)
        if self._probing:
            return
        if self.queue_page.running:
            self._deliver_probe(self._device or DeviceStatus(True, "Writing in progress"))
            return
        self._probing = True
        port = self.backend.settings.device
        run_in_thread(
            self.backend.probe,
            lambda status: self._probe_done(status, port),
            lambda error: self._probe_failed(error, port),
            name="probe",
        )

    def check_device(self, then: Callable[[DeviceStatus], None]) -> None:
        self.probe_device(then)

    def _probe_failed(self, error: BaseException, port: str) -> None:
        status = DeviceStatus(False, f"The Greaseweazle could not be checked: {error}")
        self._probe_done(status, port)

    def _probe_done(self, status: DeviceStatus, port: str) -> None:
        self._probing = False
        if port != self.backend.settings.device:
            # The device setting changed while gw info ran, so the answer is
            # about the old port: follow the new one instead.
            self.follow_device()
            if not self._probing:
                self._deliver_probe(self._device or DeviceStatus(False, NOT_CONNECTED))
            return
        if status.connected:
            self._arrival_probes = 0
        self._show_device(status)
        self._deliver_probe(status)

    def _show_device(self, status: DeviceStatus) -> None:
        previous = self._device
        self._device = status
        if previous is None or previous.connected != status.connected:
            LOG.add("device", status.message)
        self.banner.set_revealed(not status.connected)
        self.banner.set_tooltip_text(status.message)

    def _deliver_probe(self, status: DeviceStatus) -> None:
        waiters, self._probe_waiters = self._probe_waiters, []
        for waiter in waiters:
            waiter(status)

    def _poll_device(self) -> bool:
        if self._closing:
            self._probe_source = 0
            return GLib.SOURCE_REMOVE
        if self.is_visible():
            self.follow_device()
        return GLib.SOURCE_CONTINUE

    def follow_device(self) -> None:
        """Follow the Greaseweazle from files, and run gw info only when it appears.

        Nothing runs while disks are written or while gw info is running. A
        device that goes away is shown as disconnected without running gw. A
        device that gw info found although the file check never sees one (an
        unusual system) stays as gw info described it.
        """
        if self._probing or self.queue_page.running:
            return
        present = self.backend.device_present()  # reads files; unreadable ones find nothing
        arrived = present and not self._present
        gone = not present and self._present is not False
        self._present = present
        if arrived:
            self._arrival_probes = ARRIVAL_PROBES
        connected = self._device is not None and self._device.connected
        if present and self._arrival_probes and not connected:
            self._arrival_probes -= 1
            self.probe_device()
        elif gone:
            self._arrival_probes = 0
            self._show_device(DeviceStatus(False, NOT_CONNECTED))

    def device_message(self) -> str:
        if self._device is None:
            return "Not checked yet"
        status = self._device
        if not status.connected:
            return status.message or "Not connected"
        parts = [status.model and f"Greaseweazle {status.model}", status.port]
        if status.firmware:
            parts.append(f"firmware {status.firmware}")
        if status.host_tools:
            parts.append(f"host tools {status.host_tools}")
        text = ", ".join(part for part in parts if part)
        return f"Connected: {text}" if text else status.message

    @property
    def device(self) -> DeviceStatus | None:
        return self._device

    # Catalogue

    def update_catalogue(self) -> None:
        dialog = self.show_preferences()
        dialog.show_catalogue_page()
        state = self.updater.state
        if state.phase == "available" and state.offer is not None:
            dialog.confirm_install(state.offer)
        elif not state.busy:
            self.updater.check(dialog.confirm_install)

    def _offer_update(self, offer: UpdateOffer) -> None:
        self.toast(
            f"A newer catalogue, built {fmt.format_timestamp(offer.built_at)}, is available",
            button="Update",
            action="win.install-update",
        )

    def install_offered_update(self) -> None:
        state = self.updater.state
        if state.offer is None:
            self.update_catalogue()
            return
        dialog = self.show_preferences()
        dialog.show_catalogue_page()
        dialog.confirm_install(state.offer)

    def catalogue_updated(self) -> None:
        self._info = self._load_catalogue_info()
        self.find_page.set_catalogue(self._info)
        if self._preferences is not None:
            self._preferences.refresh_catalogue()
        built = fmt.format_timestamp(self._info.built_at)
        self.toast(
            f"The catalogue was updated. It was built {built}."
            if built
            else "The catalogue was updated"
        )

    # Menu items

    def show_preferences(self) -> PreferencesDialog:
        if self._preferences is None:
            dialog = PreferencesDialog(self)

            def closed(_dialog) -> None:
                self._preferences = None

            dialog.connect("closed", closed)
            self._preferences = dialog
            dialog.present(self)
        return self._preferences

    def scan_library(self) -> None:
        self.show_page("library")
        self.library_page.scan()

    def recheck_boot_blocks(self) -> None:
        """Check the library's boot blocks again if the virus data changed: at start,
        and after the brainfile is installed. The Library page shows the progress."""
        self.library_page.recheck_boot_blocks()

    def show_diagnostic_log(self) -> None:
        DiagnosticLogDialog().present(self)

    def show_shortcuts(self) -> None:
        shortcuts_window(self).present()

    def show_help(self, topic: str = "") -> None:
        if self._help_window is None:
            self._help_window = HelpWindow(self)
        if topic:
            self._help_window.help_view.show_topic(topic)
        self._help_window.present()

    def show_about(self) -> None:
        about = Adw.AboutDialog(
            application_name=APPLICATION_NAME,
            application_icon=APPLICATION_ID,
            developer_name="Pete Clarke",
            version=__version__,
            comments=(
                "Find Amiga and Atari ST menu disks, compacts, packs and cracks, and write "
                "them to floppy with a Greaseweazle."
            ),
            website=HOMEPAGE,
            issue_url=f"{HOMEPAGE}/issues",
            developers=["Pete Clarke"],
            copyright="Copyright 2026 Pete Clarke",
            license_type=Gtk.License.GPL_3_0,
        )
        about.add_credit_section(
            "Catalogue Data", [f"{name} {url}" for name, url, _what in DATA_CREDITS]
        )
        about.add_acknowledgement_section(
            "Greaseweazle", ["Keir Fraser https://github.com/keirf/greaseweazle"]
        )
        about.add_acknowledgement_section("Virus Data", list(VIRUS_CREDITS))
        about.add_acknowledgement_section(
            "Amiga Bootblock Reader", [f"Jason and Jordan Smith {BRAINFILE_PROJECT}"]
        )
        about.add_legal_section(
            "Catalogue",
            None,
            Gtk.License.CUSTOM,
            "The catalogue is licensed under CC BY-NC-SA 4.0 because it includes data from "
            "Atari Legend under that licence. Disk names and checksums come from TOSEC.",
        )
        about.add_legal_section(
            "Amiga Bootblock Reader Brainfile",
            None,
            Gtk.License.CUSTOM,
            "The brainfile that names Amiga boot blocks, by Jason and Jordan Smith, is not "
            "shipped with PirateFinder. It is downloaded from its GitHub release only when you "
            "ask for it in Preferences.",
        )
        about.present(self)

    # Closing

    def _on_close_request(self, _window) -> bool:
        if self.queue_page.running:

            def respond(response: str) -> None:
                if response == "stop":
                    self._close_after_session = True
                    self.queue_page.cancel_session()

            alert(
                self,
                "Stop Writing and Quit?",
                "A disk is being written. Stopping now leaves that floppy incomplete.",
                (("keep", "_Keep Writing", ""), ("stop", "_Stop and Quit", "destructive")),
                respond,
                default="keep",
                close="keep",
            )
            return True
        self._closing = True
        if self._probe_source:
            GLib.source_remove(self._probe_source)
            self._probe_source = 0
        if self.library_page.scanning:
            self.library_page.cancel_scan()
        self.updater.cancel()
        if self._help_window is not None:
            self._help_window.destroy()
            self._help_window = None
        try:
            self.backend.close()
        except Exception as error:  # noqa: BLE001
            LOG.add("backend", f"Closing failed: {error}")
        return False

"""Preferences: downloads and providers, the Greaseweazle, and the catalogue.

Every control writes its setting and saves the settings file at once, so
nothing depends on how the dialog is closed.
"""

from __future__ import annotations

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Gtk  # noqa: E402

from ..greaseweazle.caps import CapsState, CapsStatus  # noqa: E402
from ..jobs.cancellation import Cancellation  # noqa: E402
from . import formatting as fmt  # noqa: E402
from .backend import BrainfileStatus, fetch_media, set_fetch_media  # noqa: E402
from .bridge import Latest, run_in_thread  # noqa: E402
from .queue_page import drive_row, set_drive_row  # noqa: E402
from .updater import UpdateState  # noqa: E402
from .widgets import (  # noqa: E402
    alert,
    choose_folder,
    icon_button,
    plain_row,
    progress_bar,
    set_fraction,
    text_button,
)

DEVICE_SAVE_DELAY_MS = 600


def _row(title: str, subtitle: str = "") -> Adw.ActionRow:
    return plain_row(title=title, subtitle=subtitle, use_underline=True)


def caps_text(status: CapsStatus) -> str:
    """What IPF Support says about the SPS Decoder Library."""
    if status.state is CapsState.SYSTEM:
        return f"Found on this computer ({status.path}). IPF images can be written."
    if status.state is CapsState.INSTALLED:
        version = f"Version {status.version}" if status.version else "Installed"
        return f"{version}, installed by PirateFinder. IPF images can be written."
    if status.state is CapsState.MISSING:
        return "Not installed. IPF images cannot be written until it is."
    return (
        "Not installed, and PirateFinder has no build of it for this computer's processor "
        f"({status.machine}); there are builds for 64-bit PCs and 32-bit ARM only. A copy "
        "installed by other means is used when it is found."
    )


class PreferencesDialog(Adw.PreferencesDialog):
    """``host`` is the main window: ``backend``, ``updater``, ``check_device``,
    ``settings_changed``, ``catalogue_info`` and ``session_running``.
    """

    def __init__(self, host) -> None:
        super().__init__(title="Preferences", search_enabled=False)
        self._host = host
        self._device_timeout = 0
        self.provider_rows: dict[str, Adw.SwitchRow] = {}
        self.add(self._general_page())
        self.add(self._greaseweazle_page())
        self.add(self._catalogue_page())
        host.updater.subscribe(self._show_update)
        self._show_update(host.updater.state)
        self.connect("closed", self._on_closed)

    @property
    def settings(self):
        return self._host.backend.settings

    def _save(self, name: str) -> None:
        self._host.backend.save_settings()
        self._host.settings_changed(name)

    # General

    def _general_page(self) -> Adw.PreferencesPage:
        page = Adw.PreferencesPage(
            title="General", icon_name="preferences-system-symbolic", name="general"
        )
        downloads = Adw.PreferencesGroup(title="Downloads")
        self.download_row = _row("Download Folder", self.settings.download_folder)
        self.download_row.set_subtitle_selectable(True)
        self.download_row.add_suffix(
            text_button("_Choose…", self._on_choose_download, tooltip="Choose a folder")
        )
        downloads.add(self.download_row)
        self.online_row = Adw.SwitchRow(
            title="_Online Downloads",
            subtitle="Download disks that are not in your library from the providers below",
            use_underline=True,
            active=self.settings.online_enabled,
        )
        self.online_row.connect("notify::active", self._on_online_changed)
        downloads.add(self.online_row)
        page.add(downloads)

        information = Adw.PreferencesGroup(title="Details Pane")
        self.media_row = Adw.SwitchRow(
            title="Download _Screenshots and Background Information",
            subtitle=(
                "Pictures and Wikipedia summaries for the disc in the details pane, fetched when "
                "it is shown and kept in the cache folder. Facts, notes and crew histories come "
                "with the catalogue and are shown either way."
            ),
            use_underline=True,
            active=fetch_media(self.settings),
        )
        self.media_row.connect("notify::active", self._on_media_changed)
        # Nothing is fetched while online use is off, whatever this switch says.
        self.media_row.set_sensitive(self.settings.online_enabled)
        information.add(self.media_row)
        page.add(information)

        providers = Adw.PreferencesGroup(
            title="Providers",
            description=(
                "Menu disks contain copyrighted software. Downloads come from third-party "
                "sites that PirateFinder does not run. Every download is checked against the "
                "catalogue before it is kept."
            ),
        )
        info = self._host.catalogue_info()
        for provider_id, name in info.providers:
            row = plain_row(
                Adw.SwitchRow, title=name, active=self.settings.provider_enabled(provider_id)
            )
            row.connect("notify::active", self._on_provider_changed, provider_id)
            row.set_sensitive(self.settings.online_enabled)
            providers.add(row)
            self.provider_rows[provider_id] = row
        if not info.providers:
            providers.add(_row("The catalogue lists no download providers"))
        page.add(providers)
        return page

    def _on_choose_download(self, _button) -> None:
        choose_folder(
            self.download_row,
            "Choose the Download Folder",
            self.settings.download_folder,
            self.set_download_folder,
        )

    def set_download_folder(self, folder: str) -> None:
        self.settings.download_folder = folder
        self.download_row.set_subtitle(folder)
        self._save("download_folder")

    def _on_online_changed(self, row: Adw.SwitchRow, _property) -> None:
        self.settings.online_enabled = row.get_active()
        for provider_row in self.provider_rows.values():
            provider_row.set_sensitive(row.get_active())
        self.media_row.set_sensitive(row.get_active())
        self._save("online_enabled")

    def _on_media_changed(self, row: Adw.SwitchRow, _property) -> None:
        set_fetch_media(self.settings, row.get_active())
        self._save("fetch_media")

    def _on_provider_changed(self, row: Adw.SwitchRow, _property, provider_id: str) -> None:
        providers = dict(self.settings.providers)
        providers[provider_id] = row.get_active()
        self.settings.providers = providers
        self._save("providers")

    # Greaseweazle

    def _greaseweazle_page(self) -> Adw.PreferencesPage:
        page = Adw.PreferencesPage(
            title="Greaseweazle", icon_name="media-floppy-symbolic", name="greaseweazle"
        )
        drive = Adw.PreferencesGroup(title="Drive")
        self.drive_row = drive_row(self._on_drive_changed)
        set_drive_row(self.drive_row, self.settings.drive)
        drive.add(self.drive_row)
        self.device_row = _row(
            "D_evice",
            "The serial port, such as /dev/ttyACM0. Leave empty to find it automatically.",
        )
        self.device_entry = Gtk.Entry(
            placeholder_text="Automatic", valign=Gtk.Align.CENTER, width_chars=16
        )
        self.device_entry.set_text(self.settings.device)
        self.device_entry.update_property([Gtk.AccessibleProperty.LABEL], ["Device"])
        self.device_entry.connect("changed", self._on_device_changed)
        self.device_entry.connect("activate", lambda _entry: self._save_device())
        self.device_row.add_suffix(self.device_entry)
        self.device_row.set_activatable_widget(self.device_entry)
        drive.add(self.device_row)
        page.add(drive)

        writing = Adw.PreferencesGroup(title="Writing")
        self.retries_row = Adw.SpinRow.new_with_range(0, 10, 1)
        self.retries_row.set_title("_Retries")
        self.retries_row.set_use_underline(True)
        self.retries_row.set_subtitle(
            "How many times a track that fails to verify is written again"
        )
        self.retries_row.set_value(self.settings.retries)
        self.retries_row.connect("notify::value", self._on_retries_changed)
        writing.add(self.retries_row)
        self.erase_row = Adw.SwitchRow(
            title="_Erase Before Writing",
            subtitle="Wipe each track before writing it. Slower, and helps with old floppies.",
            use_underline=True,
            active=self.settings.pre_erase,
        )
        self.erase_row.connect("notify::active", self._on_erase_changed)
        writing.add(self.erase_row)
        self.prompt_row = Adw.SwitchRow(
            title="_Ask Before Each Disk",
            subtitle="Ask for a floppy before every disk. When off, only the first is asked for.",
            use_underline=True,
            active=self.settings.prompt_between_disks,
        )
        self.prompt_row.connect("notify::active", self._on_prompt_changed)
        writing.add(self.prompt_row)
        page.add(writing)

        connection = Adw.PreferencesGroup(title="Connection")
        self.connection_row = _row("Greaseweazle", self._host.device_message())
        self.connection_row.set_subtitle_lines(3)
        self.connection_spinner = Gtk.Spinner(visible=False, valign=Gtk.Align.CENTER)
        self.connection_row.add_suffix(self.connection_spinner)
        self.check_button = text_button("C_heck Connection", self._on_check_connection)
        self.connection_row.add_suffix(self.check_button)
        connection.add(self.connection_row)
        page.add(connection)
        page.add(self._ipf_group())
        return page

    def _ipf_group(self) -> Adw.PreferencesGroup:
        group = Adw.PreferencesGroup(
            title="IPF Support",
            description=(
                "Writing IPF images needs the SPS Decoder Library (CAPSImg) of the Software "
                "Preservation Society. Its licence allows only non-commercial use, so it is not "
                "shipped with PirateFinder. Install downloads the build made for FS-UAE from "
                "fs-uae.net into your data folder, once you have accepted the licence."
            ),
        )
        self.ipf_group = group
        self._caps_cancel: Cancellation | None = None
        self.caps_status: CapsStatus | None = None
        self.caps_row = _row("SPS Decoder Library", "Checking")
        self.caps_row.set_subtitle_lines(4)
        self.caps_progress = progress_bar()
        self.caps_progress.set_size_request(140, -1)
        self.caps_progress.set_visible(False)
        self.caps_row.add_suffix(self.caps_progress)
        self.caps_cancel_button = icon_button(
            "process-stop-symbolic", "Cancel Download", lambda _b: self.cancel_caps()
        )
        self.caps_cancel_button.set_visible(False)
        self.caps_row.add_suffix(self.caps_cancel_button)
        self.caps_install_button = text_button(
            "_Install…", self._on_install_caps, tooltip="Read the licence and install"
        )
        self.caps_install_button.set_visible(False)
        self.caps_row.add_suffix(self.caps_install_button)
        self.caps_remove_button = text_button("Re_move", self._on_remove_caps)
        self.caps_remove_button.set_visible(False)
        self.caps_row.add_suffix(self.caps_remove_button)
        group.add(self.caps_row)
        run_in_thread(
            self._host.backend.caps_status,
            self._show_caps,
            lambda error: self.caps_row.set_subtitle(str(error)),
            name="caps-status",
        )
        return group

    def _show_caps(self, status: CapsStatus) -> None:
        self.caps_status = status
        self.caps_install_button.set_visible(status.state is CapsState.MISSING)
        self.caps_remove_button.set_visible(status.state is CapsState.INSTALLED)
        self.caps_row.set_subtitle(caps_text(status))

    def _on_install_caps(self, _button) -> None:
        status = self.caps_status
        if status is None or status.build is None or self._caps_cancel is not None:
            return
        build = status.build
        text = Gtk.Label(
            label=self._host.backend.caps_licence(),
            wrap=True,
            xalign=0,
            selectable=True,
            valign=Gtk.Align.START,
        )
        scroller = Gtk.ScrolledWindow(
            child=text,
            hscrollbar_policy=Gtk.PolicyType.NEVER,
            min_content_height=260,
            has_frame=True,
        )
        text.set_margin_start(8)
        text.set_margin_end(8)
        scroller.update_property([Gtk.AccessibleProperty.LABEL], ["Licence"])

        def respond(response: str) -> None:
            if response == "accept":
                self._start_caps_install()

        alert(
            self,
            "Accept the Licence?",
            f"PirateFinder downloads the SPS Decoder Library {build.version} for "
            f"{build.description} from {build.host} and installs it for your account. Its "
            "licence, below, allows only non-commercial use. Accept it to install the library.",
            (("cancel", "_Cancel", ""), ("accept", "_Accept and Install", "suggested")),
            respond,
            default="cancel",
            extra=scroller,
        )

    def _start_caps_install(self) -> None:
        if self._caps_cancel is not None:
            return
        cancel = Cancellation()
        self._caps_cancel = cancel
        self.caps_install_button.set_visible(False)
        self.caps_progress.set_visible(True)
        self.caps_cancel_button.set_visible(True)
        set_fraction(self.caps_progress, 0.0, "Starting")
        latest = Latest(self._caps_progress)
        backend = self._host.backend

        def done(status: CapsStatus) -> None:
            self._caps_finished()
            self._show_caps(status)
            self.add_toast(
                Adw.Toast(title="IPF support was installed. IPF images can be written.", timeout=5)
            )

        def failed(error: BaseException) -> None:
            self._caps_finished()
            self.caps_install_button.set_visible(True)
            message = "The download was cancelled." if cancel.cancelled else str(error)
            self.caps_row.set_subtitle(message)

        run_in_thread(
            lambda: backend.install_caps(latest.post, cancel), done, failed, name="caps-install"
        )

    def _caps_progress(self, done: int, total: int | None) -> None:
        if self._caps_cancel is not None:
            set_fraction(
                self.caps_progress, done / total if total else None, fmt.download_text(done, total)
            )

    def _caps_finished(self) -> None:
        self._caps_cancel = None
        self.caps_progress.set_visible(False)
        self.caps_cancel_button.set_visible(False)

    def cancel_caps(self) -> None:
        if self._caps_cancel is not None:
            self._caps_cancel.cancel()
            self.caps_progress.set_text("Cancelling")

    @property
    def installing_caps(self) -> bool:
        return self._caps_cancel is not None

    def _on_remove_caps(self, _button) -> None:
        self.caps_remove_button.set_sensitive(False)

        def done(status: CapsStatus) -> None:
            self.caps_remove_button.set_sensitive(True)
            self._show_caps(status)
            self.add_toast(Adw.Toast(title="IPF support was removed.", timeout=4))

        def failed(error: BaseException) -> None:
            self.caps_remove_button.set_sensitive(True)
            self.caps_row.set_subtitle(str(error))

        run_in_thread(self._host.backend.remove_caps, done, failed, name="caps-remove")

    def _on_drive_changed(self, code: str) -> None:
        if self.settings.drive != code:
            self.settings.drive = code
            self._save("drive")

    def _on_device_changed(self, _entry) -> None:
        if self._device_timeout:
            GLib.source_remove(self._device_timeout)
        self._device_timeout = GLib.timeout_add(DEVICE_SAVE_DELAY_MS, self._save_device)

    def _save_device(self) -> bool:
        if self._device_timeout:
            GLib.source_remove(self._device_timeout)
        self._device_timeout = 0
        value = self.device_entry.get_text().strip()
        if value != self.settings.device:
            self.settings.device = value
            self._save("device")
        return GLib.SOURCE_REMOVE

    def _on_retries_changed(self, row: Adw.SpinRow, _property) -> None:
        value = int(row.get_value())
        if value != self.settings.retries:
            self.settings.retries = value
            self._save("retries")

    def _on_erase_changed(self, row: Adw.SwitchRow, _property) -> None:
        self.settings.pre_erase = row.get_active()
        self._save("pre_erase")

    def _on_prompt_changed(self, row: Adw.SwitchRow, _property) -> None:
        self.settings.prompt_between_disks = row.get_active()
        self._save("prompt_between_disks")

    def _on_check_connection(self, _button) -> None:
        if self._host.session_running():
            self.connection_row.set_subtitle("Not checked while disks are being written")
            return
        self._save_device()
        self.check_button.set_sensitive(False)
        self.connection_spinner.set_visible(True)
        self.connection_spinner.start()
        self.connection_row.set_subtitle("Checking")
        self._host.check_device(self._show_connection)

    def _show_connection(self, status) -> None:
        self.check_button.set_sensitive(True)
        self.connection_spinner.stop()
        self.connection_spinner.set_visible(False)
        self.connection_row.set_subtitle(self._host.device_message())

    # Catalogue

    def _catalogue_page(self) -> Adw.PreferencesPage:
        page = Adw.PreferencesPage(
            title="Catalogue", icon_name="system-software-update-symbolic", name="catalogue"
        )
        self.catalogue_group = Adw.PreferencesGroup(title="Installed Catalogue")
        self.catalogue_rows: dict[str, Gtk.Label] = {}
        for key, title in (
            ("built", "Built"),
            ("disks", "Disks"),
            ("series", "Series"),
            ("contents", "Titles"),
            ("images", "Known Dumps"),
        ):
            row = _row(title)
            value = Gtk.Label(xalign=1, selectable=True)
            value.add_css_class("dim-label")
            value.add_css_class("numeric")
            row.add_suffix(value)
            self.catalogue_rows[key] = value
            self.catalogue_group.add(row)
        self.location_row = _row("Location")
        self.location_row.set_subtitle_selectable(True)
        self.catalogue_group.add(self.location_row)
        page.add(self.catalogue_group)

        updates = Adw.PreferencesGroup(title="Updates")
        self.check_updates_row = Adw.SwitchRow(
            title="Check for Updates at _Start",
            subtitle="Look for a newer catalogue each time PirateFinder starts",
            use_underline=True,
            active=self.settings.check_catalogue_updates,
        )
        self.check_updates_row.connect("notify::active", self._on_check_updates_changed)
        updates.add(self.check_updates_row)
        self.update_row = _row("Update Catalogue", "Check for a newer catalogue")
        self.update_row.set_subtitle_lines(3)
        self.update_progress = progress_bar()
        self.update_progress.set_size_request(140, -1)
        self.update_progress.set_visible(False)
        self.update_row.add_suffix(self.update_progress)
        self.update_cancel = icon_button(
            "process-stop-symbolic", "Cancel Update", lambda _b: self._host.updater.cancel()
        )
        self.update_cancel.set_visible(False)
        self.update_row.add_suffix(self.update_cancel)
        self.update_button = text_button(
            "_Update Now", self._on_update_now, style="suggested-action"
        )
        self.update_row.add_suffix(self.update_button)
        updates.add(self.update_row)
        page.add(updates)
        page.add(self._virus_group())
        self.refresh_catalogue()
        return page

    def _virus_group(self) -> Adw.PreferencesGroup:
        group = Adw.PreferencesGroup(
            title="Virus Detection",
            description=(
                "Boot blocks are checked with virus signatures built into PirateFinder. The "
                "brainfile of Amiga Bootblock Reader, made by Jason and Jordan Smith, names "
                "thousands more Amiga boot blocks and is asked first when it is installed. It "
                "is downloaded from its GitHub release into your data folder and is not "
                "shipped with PirateFinder."
            ),
        )
        self.virus_detection_group = group
        self._brainfile_cancel: Cancellation | None = None
        self.brainfile_row = _row("Amiga Bootblock Reader Brainfile", "Checking")
        self.brainfile_row.set_subtitle_lines(3)
        self.brainfile_progress = progress_bar()
        self.brainfile_progress.set_size_request(140, -1)
        self.brainfile_progress.set_visible(False)
        self.brainfile_row.add_suffix(self.brainfile_progress)
        self.brainfile_cancel_button = icon_button(
            "process-stop-symbolic", "Cancel Download", lambda _b: self.cancel_brainfile()
        )
        self.brainfile_cancel_button.set_visible(False)
        self.brainfile_row.add_suffix(self.brainfile_cancel_button)
        self.brainfile_button = text_button("_Download Brainfile", self._on_download_brainfile)
        self.brainfile_row.add_suffix(self.brainfile_button)
        group.add(self.brainfile_row)
        self.brainfile_status: BrainfileStatus | None = None
        backend = self._host.backend
        run_in_thread(
            backend.brainfile_status,
            self._show_brainfile,
            lambda error: self._show_brainfile(BrainfileStatus(False, message=str(error))),
            name="brainfile-status",
        )
        return group

    def _show_brainfile(self, status: BrainfileStatus) -> None:
        self.brainfile_status = status
        if status.installed:
            parts = [f"Version {status.version}" if status.version else "Installed"]
            if status.entries:
                parts.append(fmt.plural(status.entries, "known boot block"))
            text = ", ".join(parts)
        else:
            text = (
                "Not installed. Amiga boot blocks are checked against the standard boot blocks "
                "and the built-in virus signatures."
            )
        if status.message:
            text = f"{text}\n{status.message}"
        self.brainfile_row.set_subtitle(text)
        self.brainfile_button.set_label(
            "_Update Brainfile" if status.installed else "_Download Brainfile"
        )

    def _on_download_brainfile(self, _button) -> None:
        if self._brainfile_cancel is not None:
            return
        cancel = Cancellation()
        self._brainfile_cancel = cancel
        self.brainfile_button.set_visible(False)
        self.brainfile_progress.set_visible(True)
        self.brainfile_cancel_button.set_visible(True)
        set_fraction(self.brainfile_progress, 0.0, "Starting")
        latest = Latest(self._brainfile_progress)
        backend = self._host.backend

        def done(status: BrainfileStatus) -> None:
            self._brainfile_finished()
            self._show_brainfile(status)
            self.add_toast(
                Adw.Toast(
                    title="The brainfile was installed. The library's boot blocks are checked "
                    "again.",
                    timeout=5,
                )
            )
            self._host.recheck_boot_blocks()

        def failed(error: BaseException) -> None:
            self._brainfile_finished()
            message = "The download was cancelled" if cancel.cancelled else str(error)
            self.brainfile_row.set_subtitle(message)

        run_in_thread(
            lambda: backend.install_brainfile(latest.post, cancel),
            done,
            failed,
            name="brainfile",
        )

    def _brainfile_progress(self, done: int, total: int | None) -> None:
        if self._brainfile_cancel is not None:
            set_fraction(
                self.brainfile_progress,
                done / total if total else None,
                fmt.download_text(done, total),
            )

    def _brainfile_finished(self) -> None:
        self._brainfile_cancel = None
        self.brainfile_progress.set_visible(False)
        self.brainfile_cancel_button.set_visible(False)
        self.brainfile_button.set_visible(True)

    def cancel_brainfile(self) -> None:
        if self._brainfile_cancel is not None:
            self._brainfile_cancel.cancel()
            self.brainfile_progress.set_text("Cancelling")

    @property
    def downloading_brainfile(self) -> bool:
        return self._brainfile_cancel is not None

    def refresh_catalogue(self) -> None:
        info = self._host.catalogue_info()
        if info.available:
            self.catalogue_rows["built"].set_text(fmt.format_timestamp(info.built_at) or "Unknown")
            for key in ("disks", "series", "contents", "images"):
                value = info.stats.get(key)
                self.catalogue_rows[key].set_text(f"{value:,}" if value is not None else "")
            self.location_row.set_subtitle(info.path)
            self.catalogue_group.set_description(None)
        else:
            for label in self.catalogue_rows.values():
                label.set_text("")
            self.catalogue_rows["built"].set_text("Not installed")
            self.location_row.set_subtitle(info.error or "No catalogue file was found")
            self.catalogue_group.set_description("Use Update Now to download the catalogue.")

    def _on_check_updates_changed(self, row: Adw.SwitchRow, _property) -> None:
        self.settings.check_catalogue_updates = row.get_active()
        self._save("check_catalogue_updates")

    def _on_update_now(self, _button) -> None:
        state = self._host.updater.state
        if state.phase == "available" and state.offer is not None:
            self.confirm_install(state.offer)
        else:
            self._host.updater.check(self.confirm_install)

    def confirm_install(self, offer) -> None:
        size = f" It is {fmt.human_size(offer.size)}." if offer.size else ""
        body = (
            f"A catalogue built {fmt.format_timestamp(offer.built_at)} is available.{size} "
            "Your library, queue and history are kept."
        )
        if offer.notes:
            body += f"\n\n{offer.notes}"

        def respond(response: str) -> None:
            if response == "update":
                self._host.updater.install(offer)

        alert(
            self,
            "Update the Catalogue?",
            body,
            (("cancel", "_Cancel", ""), ("update", "_Update", "suggested")),
            respond,
        )

    def _show_update(self, state: UpdateState) -> None:
        self.update_row.set_subtitle(state.message or "Check for a newer catalogue")
        installing = state.phase == "installing"
        self.update_progress.set_visible(installing)
        self.update_cancel.set_visible(installing)
        self.update_button.set_visible(not state.busy)
        self.update_button.set_label("_Install" if state.phase == "available" else "_Update Now")
        if installing:
            set_fraction(self.update_progress, state.fraction)
        if state.phase == "done":
            self.refresh_catalogue()
            self.add_toast(Adw.Toast(title="The catalogue was updated", timeout=4))

    def show_catalogue_page(self) -> None:
        self.set_visible_page_name("catalogue")

    def _on_closed(self, _dialog) -> None:
        self._save_device()
        self.cancel_brainfile()
        self.cancel_caps()
        self._host.updater.unsubscribe(self._show_update)

"""Preferences: downloads and providers, the Greaseweazle, and the catalogue.

Every control writes its setting and saves the settings file at once, so
nothing depends on how the dialog is closed.
"""

from __future__ import annotations

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Gtk  # noqa: E402

from . import formatting as fmt  # noqa: E402
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
        self._save("online_enabled")

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
        return page

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
        self.refresh_catalogue()
        return page

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
        self._host.updater.unsubscribe(self._show_update)

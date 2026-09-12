"""The pane beside the search results that describes one disk or file."""

from __future__ import annotations

from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gio, Gtk, Pango  # noqa: E402

from ..catalogue.naming import display_title  # noqa: E402
from ..models import (  # noqa: E402
    Availability,
    DiskDetail,
    ImageRecord,
    LocalFile,
    Platform,
    QueueItem,
)
from . import formatting as fmt  # noqa: E402
from .widgets import (  # noqa: E402
    GroupRows,
    copy_text,
    icon_button,
    open_uri,
    plain_row,
    progress_bar,
    set_fraction,
    show_in_files,
    status_icon,
    text_button,
    toast,
)

AMIGA_FORMATS = {"adf", "adz", "dms", "ipf"}
ST_FORMATS = {"st", "msa", "stx"}
PROBLEM_CONDITIONS = {"damaged", "intro only", "missing"}


def platform_for_file(local: LocalFile) -> Platform | None:
    name = (local.format or Path(local.member or local.path).suffix.lstrip(".")).lower()
    if name in AMIGA_FORMATS:
        return Platform.AMIGA
    if name in ST_FORMATS:
        return Platform.ATARI_ST
    return None


def _row(title: str, subtitle: str = "", *, selectable: bool = False) -> Adw.ActionRow:
    row = plain_row(title=title, subtitle=subtitle)
    row.set_title_lines(2)
    row.set_subtitle_lines(3)
    if selectable:
        row.set_subtitle_selectable(True)
    return row


class DetailPane(Gtk.Box):
    """Shows a disk (or an unmatched file) and the actions for it.

    ``host`` is the main window: it provides ``add_to_queue(items)``,
    ``write_now(items)``, ``download(item, on_progress, on_done)``,
    ``provider_names()`` and ``close_detail()``.
    """

    def __init__(self, host) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self._host = host
        self.detail: DiskDetail | None = None
        self.local: LocalFile | None = None
        self._chosen_image: int | None = None
        self._download_cancel = None

        top = Gtk.Box(margin_top=6, margin_bottom=0, margin_start=6, margin_end=6)
        top.append(Gtk.Box(hexpand=True))
        self.close_button = icon_button(
            "window-close-symbolic", "Close Details", lambda _button: host.close_detail()
        )
        top.append(self.close_button)
        self.append(top)

        self._stack = Gtk.Stack(vexpand=True, transition_type=Gtk.StackTransitionType.CROSSFADE)
        self.append(self._stack)
        loading = Adw.StatusPage(title="Loading…")
        self._stack.add_named(loading, "loading")

        scroller = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER, vexpand=True)
        clamp = Adw.Clamp(maximum_size=620, tightening_threshold=400)
        scroller.set_child(clamp)
        self._stack.add_named(scroller, "content")
        body = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=18,
            margin_top=0,
            margin_bottom=24,
            margin_start=18,
            margin_end=18,
        )
        clamp.set_child(body)

        heading = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self.title = Gtk.Label(xalign=0, wrap=True, selectable=True)
        self.title.add_css_class("title-2")
        heading.append(self.title)
        self.subtitle = Gtk.Label(xalign=0, wrap=True)
        self.subtitle.add_css_class("dim-label")
        heading.append(self.subtitle)
        self.availability = Gtk.Box(spacing=6)
        self.availability_icon = Gtk.Image()
        self.availability.append(self.availability_icon)
        self.availability_label = Gtk.Label(xalign=0, wrap=True, hexpand=True)
        self.availability.append(self.availability_label)
        heading.append(self.availability)
        self.condition = Gtk.Box(spacing=6, visible=False)
        self.condition.append(status_icon("dialog-warning-symbolic", "warning"))
        self.condition_label = Gtk.Label(xalign=0, wrap=True, hexpand=True)
        self.condition_label.add_css_class("warning")
        self.condition.append(self.condition_label)
        heading.append(self.condition)
        body.append(heading)

        buttons = Gtk.Box(spacing=8)
        self.write_button = text_button(
            "_Write Now",
            self._on_write,
            style="suggested-action pill",
            tooltip="Write this disk to a floppy now",
        )
        buttons.append(self.write_button)
        self.add_button = text_button(
            "_Add to Queue", self._on_add, style="pill", tooltip="Write it later with other disks"
        )
        buttons.append(self.add_button)
        self._actions = Gio.SimpleActionGroup()
        for name, callback in (
            ("download", self._on_download),
            ("show-in-files", self._on_show_in_files),
            ("copy-label", self._on_copy_label),
        ):
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", lambda _action, _parameter, run=callback: run())
            self._actions.add_action(action)
        self.insert_action_group("detail", self._actions)
        menu = Gio.Menu()
        menu.append("_Download Only", "detail.download")
        menu.append("_Show in Files", "detail.show-in-files")
        menu.append("_Copy Label Text", "detail.copy-label")
        self.more_button = Gtk.MenuButton(
            icon_name="view-more-symbolic",
            menu_model=menu,
            tooltip_text="More Actions",
            valign=Gtk.Align.CENTER,
        )
        self.more_button.update_property([Gtk.AccessibleProperty.LABEL], ["More Actions"])
        self.more_button.add_css_class("circular")
        buttons.append(self.more_button)
        body.append(buttons)

        self.download_box = Gtk.Box(spacing=8, visible=False)
        self.download_bar = progress_bar()
        self.download_bar.set_hexpand(True)
        self.download_box.append(self.download_bar)
        self.download_box.append(
            icon_button("process-stop-symbolic", "Cancel Download", self._on_cancel_download)
        )
        body.append(self.download_box)

        self.contents_group = Adw.PreferencesGroup(title="Contents")
        self.contents_rows = GroupRows(self.contents_group)
        body.append(self.contents_group)

        self.dumps_group = Adw.PreferencesGroup(
            title="Dumps", description="Choose which dump to write."
        )
        self.dump_rows = GroupRows(self.dumps_group)
        body.append(self.dumps_group)

        self.notes_group = Adw.PreferencesGroup()
        self.notes_expander = Adw.ExpanderRow(title="Notes and Credits")
        self.notes_label = Gtk.Label(
            xalign=0,
            wrap=True,
            wrap_mode=Pango.WrapMode.WORD_CHAR,
            selectable=True,
            margin_top=12,
            margin_bottom=12,
            margin_start=12,
            margin_end=12,
        )
        self.notes_expander.add_row(self.notes_label)
        self.notes_group.add(self.notes_expander)
        body.append(self.notes_group)

        self.links_group = Adw.PreferencesGroup(title="Links")
        self.link_rows = GroupRows(self.links_group)
        body.append(self.links_group)

    # Showing things

    def show_loading(self) -> None:
        self._stack.set_visible_child_name("loading")

    def show_disk(self, detail: DiskDetail) -> None:
        self.detail = detail
        self.local = None
        self._chosen_image = None
        disk = detail.disk
        self.title.set_text(disk.label)
        subtitle = fmt.disk_subtitle(disk)
        if disk.title and disk.title != disk.label:
            subtitle = f"{subtitle}\n{disk.title}" if subtitle else disk.title
        self.subtitle.set_text(subtitle)
        self._set_availability(detail.availability, self._availability_text(detail))
        condition = disk.condition.strip().lower()
        self.condition.set_visible(condition in PROBLEM_CONDITIONS)
        self.condition_label.set_text(
            f"The catalogue lists this disk as {condition}. It may not work as expected."
        )

        self.contents_group.set_title("Contents")
        self.contents_rows.clear()
        for content in detail.contents:
            self.contents_rows.add(
                _row(display_title(content.title), fmt.content_subtitle(content))
            )
        if not detail.contents:
            self.contents_rows.add(_row("No contents are listed for this disk"))
        self.contents_group.set_visible(True)

        self._fill_dumps(detail)
        notes = "\n\n".join(text for text in (disk.notes, disk.credits) if text.strip())
        self.notes_label.set_text(notes)
        self.notes_group.set_visible(bool(notes))
        self.notes_expander.set_expanded(False)

        self.link_rows.clear()
        for link in detail.links:
            row = _row(link.label, link.url)
            row.set_activatable(True)
            row.add_suffix(Gtk.Image(icon_name="adw-external-link-symbolic"))
            row.set_tooltip_text(f"Open {link.url}")
            row.connect("activated", lambda _row, uri=link.url: open_uri(self, uri))
            self.link_rows.add(row)
        self.links_group.set_visible(bool(detail.links))

        can_write = detail.availability != Availability.MISSING
        self.write_button.set_sensitive(can_write)
        self.add_button.set_sensitive(True)
        self.write_button.set_tooltip_text(
            "Write this disk to a floppy now"
            if can_write
            else "No image of this disk is in your library or online"
        )
        self._actions.lookup_action("download").set_enabled(
            bool(detail.locations) and detail.availability == Availability.ONLINE
        )
        self._actions.lookup_action("show-in-files").set_enabled(bool(detail.local_files))
        self._actions.lookup_action("copy-label").set_enabled(True)
        self._stack.set_visible_child_name("content")

    def show_local(self, local: LocalFile) -> None:
        self.detail = None
        self.local = local
        self.title.set_text(fmt.local_file_name(local))
        parts = ["Unmatched file", (local.format or "").upper(), fmt.human_size(local.size)]
        self.subtitle.set_text(" • ".join(part for part in parts if part))
        self._set_availability(Availability.LOCAL, "In your library, not in the catalogue")
        self.condition.set_visible(False)

        self.contents_group.set_title("File")
        self.contents_rows.clear()
        self.contents_rows.add(_row("Location", local.path, selectable=True))
        if local.member:
            self.contents_rows.add(_row("Inside Archive", local.member, selectable=True))
        if local.volume_label:
            self.contents_rows.add(_row("Volume Label", local.volume_label, selectable=True))
        hashes = [
            f"{name} {value}"
            for name, value in (("MD5", local.md5), ("SHA-1", local.sha1), ("CRC32", local.crc32))
            if value
        ]
        if hashes:
            self.contents_rows.add(_row("Checksums", "\n".join(hashes), selectable=True))

        self.dumps_group.set_title("Files on the Disk")
        self.dumps_group.set_description(None)
        self.dump_rows.clear()
        for name in local.listing[:100]:
            self.dump_rows.add(_row(name))
        self.dumps_group.set_visible(bool(local.listing))
        self.notes_group.set_visible(False)
        self.link_rows.clear()
        self.links_group.set_visible(False)
        self.write_button.set_sensitive(True)
        self.write_button.set_tooltip_text("Write this file to a floppy now")
        self.add_button.set_sensitive(True)
        self._actions.lookup_action("download").set_enabled(False)
        self._actions.lookup_action("show-in-files").set_enabled(True)
        self._actions.lookup_action("copy-label").set_enabled(True)
        self._stack.set_visible_child_name("content")

    def _set_availability(self, availability: Availability, text: str) -> None:
        icon = fmt.AVAILABILITY_ICONS[availability] or "action-unavailable-symbolic"
        self.availability_icon.set_from_icon_name(icon)
        self.availability_label.set_text(text)
        for css in ("success", "dim-label"):
            self.availability_label.remove_css_class(css)
            self.availability_icon.remove_css_class(css)
        css = "success" if availability == Availability.LOCAL else "dim-label"
        if availability != Availability.ONLINE:
            self.availability_label.add_css_class(css)
            self.availability_icon.add_css_class(css)

    def _availability_text(self, detail: DiskDetail) -> str:
        if detail.availability == Availability.LOCAL:
            return "In your library"
        if detail.availability == Availability.ONLINE:
            names = self._host.provider_names()
            providers = dict.fromkeys(
                fmt.provider_name(location.provider, names) for location in detail.locations
            )
            return "Can be downloaded from " + ", ".join(providers) if providers else "Online"
        return "No image of this disk is in your library or online"

    def _fill_dumps(self, detail: DiskDetail) -> None:
        self.dumps_group.set_title("Dumps")
        self.dump_rows.clear()
        if not detail.images:
            self.dumps_group.set_visible(False)
            return
        self.dumps_group.set_visible(True)
        self.dumps_group.set_description(
            "Choose which dump to write." if len(detail.images) > 1 else None
        )
        names = self._host.provider_names()
        sources = self._host.source_names()
        local_by_image = {
            local.image_id: local for local in detail.local_files if local.image_id is not None
        }
        online_by_image: dict[int | None, str] = {}
        for location in detail.locations:
            online_by_image.setdefault(
                location.image_id, fmt.provider_name(location.provider, names)
            )
        group_leader: Gtk.CheckButton | None = None
        if len(detail.images) > 1:
            best = _row("Best Available", "PirateFinder picks the best dump it can find")
            group_leader = self._radio(best, None, None)
            group_leader.set_active(True)
            self.dump_rows.add(best)
        for record in detail.images:
            subtitle = self._dump_subtitle(record, local_by_image, online_by_image, sources)
            row = _row(record.name, subtitle)
            if len(detail.images) > 1:
                self._radio(row, record, group_leader)
            self.dump_rows.add(row)

    def _radio(self, row, record: ImageRecord | None, leader) -> Gtk.CheckButton:
        check = Gtk.CheckButton(valign=Gtk.Align.CENTER)
        check.update_property(
            [Gtk.AccessibleProperty.LABEL], [record.name if record else "Best Available"]
        )
        if leader is not None:
            check.set_group(leader)
        row.add_prefix(check)
        row.set_activatable_widget(check)
        image_id = record.id if record else None
        check.connect("toggled", lambda button: self._choose_image(button, image_id))
        return check

    def _choose_image(self, button: Gtk.CheckButton, image_id: int | None) -> None:
        if button.get_active():
            self._chosen_image = image_id

    @staticmethod
    def _dump_subtitle(record: ImageRecord, local_by_image, online_by_image, names) -> str:
        parts = [record.format.upper()]
        if record.flags:
            parts.append(record.flags)
        if record.bad:
            parts.append("bad dump")
        if record.source:
            parts.append(f"listed by {fmt.provider_name(record.source, names)}")
        if record.id in local_by_image:
            parts.append(f"in your library at {fmt.local_file_location(local_by_image[record.id])}")
        elif record.id in online_by_image:
            parts.append(f"online at {online_by_image[record.id]}")
        return " • ".join(parts)

    # Actions

    def queue_item(self) -> QueueItem | None:
        if self.detail is not None:
            disk = self.detail.disk
            return fmt.new_queue_item(
                disk.label, disk.platform, disk_id=disk.id, image_id=self._chosen_image
            )
        if self.local is not None:
            return fmt.new_queue_item(
                fmt.local_file_name(self.local), platform_for_file(self.local), local=self.local
            )
        return None

    def _on_write(self, _button) -> None:
        item = self.queue_item()
        if item is not None:
            self._host.write_now([item])

    def _on_add(self, _button) -> None:
        item = self.queue_item()
        if item is not None:
            self._host.add_to_queue([item])

    def _on_show_in_files(self) -> None:
        if self.local is not None:
            show_in_files(self, self.local.path)
        elif self.detail is not None and self.detail.local_files:
            show_in_files(self, self.detail.local_files[0].path)

    def _on_copy_label(self) -> None:
        if self.detail is not None:
            text = fmt.sticker_text(self.detail.disk.label, self.detail.contents)
        elif self.local is not None:
            text = self.local.volume_label or fmt.local_file_name(self.local)
        else:
            return
        copy_text(self, text)
        toast(self, f"Copied “{text}”")

    def _on_download(self) -> None:
        item = self.queue_item()
        if item is None or self._download_cancel is not None:
            return
        self.download_box.set_visible(True)
        set_fraction(self.download_bar, 0.0, "Starting download")
        self._actions.lookup_action("download").set_enabled(False)
        self._download_cancel = self._host.download(item, self._download_progress, self._finished)

    def _download_progress(self, done: int, total: int | None) -> None:
        fraction = done / total if total else None
        set_fraction(self.download_bar, fraction, fmt.download_text(done, total))

    def _finished(self, _success: bool) -> None:
        self._download_cancel = None
        self.download_box.set_visible(False)

    def _on_cancel_download(self, _button) -> None:
        if self._download_cancel is not None:
            self._download_cancel.cancel()
            self.download_bar.set_text("Cancelling…")

    @property
    def downloading(self) -> bool:
        return self._download_cancel is not None


def make_item_for_result(result) -> QueueItem:
    """A queue item for a search result, with the best dump."""
    if result.disk is not None:
        return fmt.new_queue_item(result.disk.label, result.disk.platform, disk_id=result.disk.id)
    local = result.local
    return fmt.new_queue_item(fmt.local_file_name(local), platform_for_file(local), local=local)

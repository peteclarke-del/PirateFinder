"""The pane beside the results that describes one disc, or one title on it.

From the top: pictures, the title and disc, the write buttons, any virus on
the dump that would be written, the facts, the other titles on the disc,
the crew, trivia with its sources, the known dumps, the user's copies that
match none of them, notes and links. An unmatched library file can be
linked to its disc from the More Actions menu (``link_disc``).
"""

from __future__ import annotations

from collections.abc import Callable

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gio, Gtk, Pango  # noqa: E402

from ..catalogue.naming import display_title  # noqa: E402
from ..jobs.queue import item_from_disk, item_from_local  # noqa: E402
from ..library.library import local_name  # noqa: E402
from ..models import (  # noqa: E402
    Availability,
    Content,
    DiskDetail,
    ImageRecord,
    LocalFile,
    MediaItem,
    QueueItem,
    ResultRow,
    TriviaItem,
    VirusReport,
)
from . import formatting as fmt  # noqa: E402
from .bridge import run_in_thread  # noqa: E402
from .edit_details import EDITED, EditDetailsDialog, edited_label  # noqa: E402
from .link_disc import LinkDiscDialog  # noqa: E402
from .log import LOG  # noqa: E402
from .media_view import MediaView  # noqa: E402
from .widgets import (  # noqa: E402
    GroupRows,
    alert,
    caption_label,
    clear_children,
    copy_text,
    icon_button,
    link_markup,
    open_uri,
    plain_row,
    progress_bar,
    set_accessible_label,
    set_fraction,
    show_in_files,
    status_icon,
    text_button,
    toast,
)

PROBLEM_CONDITIONS = {"damaged", "intro only", "missing"}
# Facts that show a field the user can correct with Edit Details.
EDITABLE_FACTS = {
    "Crew": "crew",
    "Disc": "label",
    "Catalogue Name": "title",
    "Released": "date",
    "Publisher": "publisher",
    "Cracked By": "cracker",
}
WIKIPEDIA = "https://en.wikipedia.org/wiki/"


def _row(title: str, subtitle: str = "", *, selectable: bool = False) -> Adw.ActionRow:
    row = plain_row(title=title, subtitle=subtitle)
    row.set_title_lines(2)
    row.set_subtitle_lines(3)
    if selectable:
        row.set_subtitle_selectable(True)
    return row


def _wrapped(text: str = "", **properties) -> Gtk.Label:
    label = Gtk.Label(
        label=text,
        xalign=0,
        wrap=True,
        wrap_mode=Pango.WrapMode.WORD_CHAR,
        selectable=True,
        focusable=False,
        **properties,
    )
    return label


def _text_block(text: str) -> Gtk.Widget:
    """Catalogue text as a label. Text laid out in columns or drawn with symbols keeps its
    lines in a fixed-width font and scrolls sideways when it is wider than the pane."""
    if not fmt.laid_out(text):
        return _wrapped(text)
    label = Gtk.Label(label=text, xalign=0, selectable=True, focusable=False)
    label.add_css_class("monospace")
    return Gtk.ScrolledWindow(
        child=label,
        hscrollbar_policy=Gtk.PolicyType.AUTOMATIC,
        vscrollbar_policy=Gtk.PolicyType.NEVER,
        propagate_natural_height=True,
    )


class DetailPane(Gtk.Box):
    """Shows a disc (or an unmatched file) and the actions for it.

    ``host`` is the main window: it provides ``backend``, ``add_to_queue``,
    ``write_now``, ``download(item, on_progress, on_done)``,
    ``provider_names()``, ``source_names()``, ``close_detail()``, ``toast``,
    ``library_changed()``, ``details_changed()`` and ``show_disc(disk_id)``. ``on_title_selected(content_id)`` is called
    when a title is picked in "On This Disc".
    """

    def __init__(self, host) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self._host = host
        self.detail: DiskDetail | None = None
        self.local: LocalFile | None = None
        self.content_id: int | None = None
        self.alternates: list[ImageRecord] = []
        self.summaries: list[TriviaItem] = []
        self.summaries_loading = False
        self.on_title_selected: Callable[[int | None], None] | None = None
        self._chosen_image: int | None = None
        self._radios: dict[int | None, Gtk.CheckButton] = {}
        self._title_rows: dict[int, Adw.ActionRow] = {}
        self._download_cancel = None
        self._summary_cache: dict[tuple[int, int | None], list[TriviaItem]] = {}
        self._summary_generation = 0
        self.cleaning = False
        self.fact_values: dict[str, str] = {}
        self.edited_facts: dict[str, str] = {}  # fact name -> the catalogue's value
        self.edit_dialog: EditDetailsDialog | None = None
        self.link_dialog: LinkDiscDialog | None = None

        top = Gtk.Box(margin_top=6, margin_bottom=0, margin_start=6, margin_end=6)
        top.append(Gtk.Box(hexpand=True))
        self.close_button = icon_button(
            "window-close-symbolic", "Close Details", lambda _button: host.close_detail()
        )
        top.append(self.close_button)
        self.append(top)

        self._stack = Gtk.Stack(vexpand=True, transition_type=Gtk.StackTransitionType.CROSSFADE)
        self.append(self._stack)
        loading = Adw.StatusPage(title="Loading")
        spinner = Gtk.Spinner(spinning=True, halign=Gtk.Align.CENTER)
        spinner.set_size_request(32, 32)
        loading.set_child(spinner)
        self._stack.add_named(loading, "loading")

        self.scroller = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER, vexpand=True)
        clamp = Adw.Clamp(maximum_size=620, tightening_threshold=400)
        self.scroller.set_child(clamp)
        self._stack.add_named(self.scroller, "content")
        body = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=18,
            margin_top=0,
            margin_bottom=24,
            margin_start=18,
            margin_end=18,
        )
        clamp.set_child(body)

        self.media = MediaView(self._load_media, host.source_names)
        body.append(self.media)

        body.append(self._build_heading())
        body.append(self._build_buttons())

        self.download_box = Gtk.Box(spacing=8, visible=False)
        self.download_bar = progress_bar()
        self.download_bar.set_hexpand(True)
        self.download_box.append(self.download_bar)
        self.download_box.append(
            icon_button("process-stop-symbolic", "Cancel Download", self._on_cancel_download)
        )
        body.append(self.download_box)

        body.append(self._build_virus())

        self.facts_group = Adw.PreferencesGroup(title="Details")
        facts_card = Gtk.Box()
        facts_card.add_css_class("card")
        facts_card.add_css_class("facts")
        self.facts = Gtk.Grid(column_spacing=18, row_spacing=6, hexpand=True)
        facts_card.append(self.facts)
        self.facts_group.add(facts_card)
        body.append(self.facts_group)

        self.titles_group = Adw.PreferencesGroup(title="On This Disc")
        self.title_rows = GroupRows(self.titles_group)
        body.append(self.titles_group)

        self.crew_group = Adw.PreferencesGroup(title="About the Crew")
        crew_card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        crew_card.add_css_class("card")
        crew_card.add_css_class("facts")
        self.crew_name = Gtk.Label(xalign=0, wrap=True)
        self.crew_name.add_css_class("heading")
        crew_card.append(self.crew_name)
        self.crew_notes = _wrapped()
        crew_card.append(self.crew_notes)
        self.crew_facts = _wrapped()
        crew_card.append(self.crew_facts)
        self.crew_credit = caption_label(dim=False, use_markup=True)
        self.crew_credit.connect("activate-link", self._on_link)
        crew_card.append(self.crew_credit)
        self.crew_group.add(crew_card)
        body.append(self.crew_group)

        self.trivia_group = Adw.PreferencesGroup(title="Trivia")
        self.trivia_list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        self.trivia_list.add_css_class("boxed-list")
        set_accessible_label(self.trivia_list, "Trivia")
        self.trivia_group.add(self.trivia_list)
        body.append(self.trivia_group)

        self.dumps_group = Adw.PreferencesGroup(
            title="Dumps", description="Choose which dump to write."
        )
        self.dump_rows = GroupRows(self.dumps_group)
        body.append(self.dumps_group)

        self.copies_group = Adw.PreferencesGroup(
            title="Your Copies",
            description=(
                "Files in your library that count as this disc although they match none of "
                "its dumps: linked by you, downloaded where no checksum could check them, or "
                "cleaned of a virus."
            ),
            visible=False,
        )
        self.copy_rows = GroupRows(self.copies_group)
        body.append(self.copies_group)

        self.notes_group = Adw.PreferencesGroup()
        self.notes_expander = Adw.ExpanderRow(title="Notes and Credits")
        self.notes_box = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            margin_top=12,
            margin_bottom=12,
            margin_start=12,
            margin_end=12,
        )
        self.notes_expander.add_row(self.notes_box)
        self.notes_group.add(self.notes_expander)
        # The text the menu scrolls across the screen, as the catalogue captured it.
        self.scroll_expander = Adw.ExpanderRow(title="Scroll Text")
        self.scroll_label = _wrapped(
            margin_top=12, margin_bottom=12, margin_start=12, margin_end=12
        )
        self.scroll_label.add_css_class("monospace")
        self.scroll_expander.add_row(self.scroll_label)
        self.notes_group.add(self.scroll_expander)
        body.append(self.notes_group)

        self.links_group = Adw.PreferencesGroup(title="Links")
        self.link_rows = GroupRows(self.links_group)
        body.append(self.links_group)

    # Building

    def _build_heading(self) -> Gtk.Widget:
        heading = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        self.title = Gtk.Label(xalign=0, wrap=True, selectable=True, focusable=False)
        self.title.add_css_class("title-2")
        heading.append(self.title)
        self.subtitle = Gtk.Label(xalign=0, wrap=True)
        self.subtitle.add_css_class("dim-label")
        heading.append(self.subtitle)
        self.condition = Gtk.Box(spacing=6, visible=False, margin_top=4)
        self.condition.append(status_icon("dialog-warning-symbolic", "warning"))
        self.condition_label = Gtk.Label(xalign=0, wrap=True, hexpand=True)
        self.condition.append(self.condition_label)
        heading.append(self.condition)
        return heading

    def _build_buttons(self) -> Gtk.Widget:
        buttons = Gtk.Box(spacing=8)
        self.write_button = text_button(
            "_Write Now",
            self._on_write,
            style="suggested-action pill",
            tooltip="Write this disc to a floppy now",
        )
        buttons.append(self.write_button)
        self.add_button = text_button(
            "_Add to Queue", self._on_add, style="pill", tooltip="Write it later with other discs"
        )
        buttons.append(self.add_button)
        self._actions = Gio.SimpleActionGroup()
        for name, callback in (
            ("download", self._on_download),
            ("show-in-files", self._on_show_in_files),
            ("copy-label", self._on_copy_label),
            ("edit-details", self.edit_details),
            ("revert-details", lambda: self.confirm_revert()),
            ("link-disc", self.link_disc),
        ):
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", lambda _action, _parameter, run=callback: run())
            self._actions.add_action(action)
        self.insert_action_group("detail", self._actions)
        menu = Gio.Menu()
        menu.append("_Download Only", "detail.download")
        menu.append("_Show in Files", "detail.show-in-files")
        menu.append("_Copy Label Text", "detail.copy-label")
        edits = Gio.Menu()
        edits.append("_Edit Details…", "detail.edit-details")
        edits.append("_Revert to Catalogue", "detail.revert-details")
        edits.append("_Link to Disc…", "detail.link-disc")
        menu.append_section(None, edits)
        self.more_button = Gtk.MenuButton(
            icon_name="view-more-symbolic",
            menu_model=menu,
            tooltip_text="More Actions",
            valign=Gtk.Align.CENTER,
        )
        set_accessible_label(self.more_button, "More Actions")
        self.more_button.add_css_class("circular")
        buttons.append(self.more_button)
        return buttons

    def _build_virus(self) -> Gtk.Widget:
        self.virus_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, visible=False)
        card = Gtk.Box(spacing=12)
        card.add_css_class("virus-card")
        icon = Gtk.Image(icon_name="dialog-warning-symbolic", pixel_size=24)
        icon.set_valign(Gtk.Align.START)
        icon.add_css_class("error")
        card.append(icon)
        texts = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4, hexpand=True)
        self.virus_heading = Gtk.Label(xalign=0, wrap=True)
        self.virus_heading.add_css_class("heading")
        self.virus_heading.add_css_class("error")
        texts.append(self.virus_heading)
        self.virus_body = _wrapped()
        texts.append(self.virus_body)
        self.virus_source = caption_label()
        texts.append(self.virus_source)
        card.append(texts)
        self.virus_card = card
        self.virus_box.append(card)

        group = Adw.PreferencesGroup()
        self.remove_virus_row = Adw.SwitchRow(
            title="_Remove Before Writing",
            subtitle="The floppy gets a clean boot block. The stored image is not changed.",
            use_underline=True,
            active=True,
        )
        group.add(self.remove_virus_row)
        self.clean_row = _row("Clean the Stored Image")
        self.clean_button = text_button(
            "C_lean…", self._on_clean_stored, tooltip="Remove the virus from the file itself"
        )
        self.clean_row.add_suffix(self.clean_button)
        group.add(self.clean_row)
        self.alternate_row = _row("A Clean Dump Is Known")
        self.use_clean_button = text_button(
            "_Use Clean Dump", self._on_use_clean, tooltip="Write the clean dump instead"
        )
        self.alternate_row.add_suffix(self.use_clean_button)
        group.add(self.alternate_row)
        self.clean_chosen_row = _row("The Clean Dump Will Be Written")
        self.clean_chosen_row.add_prefix(status_icon("emblem-ok-symbolic", "success"))
        group.add(self.clean_chosen_row)
        self.virus_group = group
        self.virus_box.append(group)
        return self.virus_box

    # Showing things

    def show_loading(self) -> None:
        self._stack.set_visible_child_name("loading")

    def key(self) -> str:
        """The key of the result row this pane shows, "" for none."""
        if self.detail is None:
            return ""
        if self.content_id is not None:
            return f"title:{self.content_id}"
        return f"disk:{self.detail.disk.id}"

    def keys(self) -> tuple[str, ...]:
        """Keys of result rows that belong to what is shown: the title, then its disc."""
        if self.detail is None:
            return ()
        disc = f"disk:{self.detail.disk.id}"
        return (self.key(), disc) if self.content_id is not None else (disc,)

    def show_disk(
        self,
        detail: DiskDetail,
        content_id: int | None = None,
        alternates: list[ImageRecord] | tuple[ImageRecord, ...] = (),
    ) -> None:
        self._restore_disc_layout()
        same_disc = self.detail is not None and self.detail.disk.id == detail.disk.id
        chosen = self._chosen_image if same_disc else None
        self.detail = detail
        self.local = None
        written = self._written_images()
        self.alternates = [record for record in alternates if record.id not in written]
        ids = {record.id for record in detail.images}
        self._chosen_image = chosen if chosen in ids else None
        if not same_disc:
            self.remove_virus_row.set_active(True)

        self._fill_titles(detail)
        self._fill_crew(detail)
        self._fill_dumps(detail)
        self._fill_copies(detail)
        self._update_virus()

        disk = detail.disk
        notes = "\n\n".join(text for text in (disk.notes, disk.credits) if text.strip())
        clear_children(self.notes_box)
        if notes:
            self.notes_box.append(_text_block(notes))
        self.notes_expander.set_visible(bool(notes))
        self.notes_expander.set_expanded(False)
        self.notes_expander.set_subtitle(EDITED if "notes" in detail.edited else "")
        scroll_text = disk.menu_text.strip()
        self.scroll_label.set_text(scroll_text)
        self.scroll_expander.set_visible(bool(scroll_text))
        self.scroll_expander.set_expanded(False)
        self.notes_group.set_visible(bool(notes or scroll_text))

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
            "Write this disc to a floppy now"
            if can_write
            else "No image of this disc is in your library or online"
        )
        self._actions.lookup_action("download").set_enabled(
            bool(detail.locations) and detail.availability == Availability.ONLINE
        )
        self._actions.lookup_action("show-in-files").set_enabled(bool(detail.local_files))
        self._actions.lookup_action("copy-label").set_enabled(True)
        self._actions.lookup_action("edit-details").set_enabled(True)
        self._actions.lookup_action("revert-details").set_enabled(
            bool(detail.edited or detail.edited_titles)
        )
        self._actions.lookup_action("link-disc").set_enabled(False)
        self.select_title(content_id, notify=False, scroll=not same_disc)
        self._stack.set_visible_child_name("content")

    def select_title(
        self, content_id: int | None, *, notify: bool = True, scroll: bool = False
    ) -> None:
        """Show one title of the disc (or the disc itself for None)."""
        detail = self.detail
        if detail is None:
            return
        content = self.content(content_id)
        self.content_id = content.id if content is not None else None
        disk = detail.disk
        if content is not None:
            self.title.set_text(display_title(content.title))
            self.subtitle.set_text(f"on {disk.label}")
        else:
            self.title.set_text(disk.label)
            self.subtitle.set_text(fmt.disk_subtitle(disk))
        condition = disk.condition.strip().lower()
        self.condition.set_visible(condition in PROBLEM_CONDITIONS)
        self.condition_label.set_text(
            f"The catalogue lists this disc as {condition}. It may not work as expected."
        )
        self._fill_facts(detail, content)
        for cid, row in self._title_rows.items():
            current = cid == self.content_id
            if current:
                row.add_css_class("current-title")
            else:
                row.remove_css_class("current-title")
            row.marker.set_visible(current)
        self._show_media(detail, content)
        self._fill_trivia()
        self._load_summaries()
        if scroll:
            self.scroller.get_vadjustment().set_value(0)
        if notify and self.on_title_selected is not None:
            self.on_title_selected(self.content_id)

    def content(self, content_id: int | None) -> Content | None:
        if self.detail is None or content_id is None:
            return None
        return next((c for c in self.detail.contents if c.id == content_id), None)

    def show_row(self, row: ResultRow) -> bool:
        """Show another title of the disc already shown; False when it is another disc."""
        if self.detail is None or self.detail.disk.id != row.disk.id:
            return False
        self.select_title(row.content_id, notify=False)
        return True

    def _fill_facts(self, detail: DiskDetail, content: Content | None) -> None:
        disk = detail.disk
        facts: list[tuple[str, str]] = [
            ("Platform", fmt.platform_name(disk.platform)),
            ("Crew", disk.crew),
            ("Disc", disk.label),
        ]
        if disk.title and disk.title != disk.label:
            facts.append(("Catalogue Name", disk.title))
        if content is not None:
            facts.append(("Type", fmt.CONTENT_KIND_NAMES.get(content.kind, "")))
            if disk.category:
                facts.append(("Disc Type", disk.category))
        else:
            facts.append(("Type", disk.category))
        facts.append(("Disc Kind", fmt.KIND_NAMES.get(disk.kind, "")))
        facts.append(("Released", fmt.disk_release(disk)))
        publisher = (content.publisher if content else "") or disk.publisher
        cracker = (content.cracker if content else "") or disk.cracker
        facts.append(("Publisher", publisher))
        facts.append(("Cracked By", cracker))
        if content is not None:
            facts.append(("Version", content.version))
            facts.append(("Extras", content.extra))
        if disk.condition:
            facts.append(("Condition", disk.condition.capitalize()))
        # Of the dump that would be written, for information; a virus has its own card.
        facts.append(("Boot Block", fmt.boot_block_text(detail.virus)))
        facts.append(("Availability", self._availability_text(detail)))
        self.edited_facts = self._edited_facts(detail, content)
        self._set_facts(facts)

    @staticmethod
    def _edited_facts(detail: DiskDetail, content: Content | None) -> dict[str, str]:
        """Facts that show a value the user corrected: fact name -> the catalogue's value."""
        original = detail.original
        if original is None:
            return {}
        edited: dict[str, str] = {}
        for fact, name in EDITABLE_FACTS.items():
            if name not in detail.edited:
                continue
            if content is not None and getattr(content, name, ""):
                continue  # the fact shows the title's own publisher or cracker
            edited[fact] = fmt.disk_release(original) if name == "date" else getattr(original, name)
        return edited

    def _set_facts(self, facts: list[tuple[str, str]]) -> None:
        """Show these (name, value) facts, leaving out the empty ones."""
        clear_children(self.facts)
        self.fact_values: dict[str, str] = {}
        for name, value in facts:
            self._add_fact(name, value)

    def _add_fact(self, name: str, value: str) -> None:
        """One fact; one the user corrected gets an "Edited" note naming the catalogue's value."""
        if not value:
            return
        row = len(self.fact_values)
        key = Gtk.Label(label=name, xalign=0, valign=Gtk.Align.START)
        key.add_css_class("dim-label")
        self.facts.attach(key, 0, row, 1, 1)
        self.facts.attach(_wrapped(value, hexpand=True), 1, row, 1, 1)
        if name in self.edited_facts:
            note = edited_label(self.edited_facts[name])
            note.set_valign(Gtk.Align.START)
            self.facts.attach(note, 2, row, 1, 1)
        self.fact_values[name] = value

    def _fill_titles(self, detail: DiskDetail) -> None:
        self.title_rows.clear()
        self._title_rows = {}
        renamed = dict(detail.edited_titles)
        for content in detail.contents:
            row = _row(display_title(content.title), fmt.content_subtitle(content))
            row.set_activatable(True)
            row.set_tooltip_text("Show this title")
            if content.id in renamed:
                row.add_suffix(edited_label(display_title(renamed[content.id])))
            marker = status_icon("object-select-symbolic", "accent", "Shown above")
            marker.set_visible(False)
            row.add_suffix(marker)
            row.marker = marker
            row.connect("activated", lambda _row, cid=content.id: self.select_title(cid))
            self.title_rows.add(row)
            self._title_rows[content.id] = row
        if not detail.contents:
            self.title_rows.add(_row("No titles are listed for this disc"))
        count = len(detail.contents)
        self.titles_group.set_description(fmt.plural(count, "title") if count > 1 else None)

    def _fill_crew(self, detail: DiskDetail) -> None:
        crew = detail.crew
        self.crew_group.set_visible(crew is not None)
        if crew is None:
            return
        self.crew_name.set_text(crew.name)
        self.crew_notes.set_text(crew.notes)
        self.crew_notes.set_visible(bool(crew.notes))
        facts = []
        if crew.founded:
            facts.append(f"Founded {crew.founded}")
        if crew.members:
            facts.append("Members: " + ", ".join(crew.members))
        self.crew_facts.set_text("\n".join(facts))
        self.crew_facts.set_visible(bool(facts))
        names = self._host.source_names()
        credits = []
        if crew.source:
            credits.append(
                link_markup(f"Source: {fmt.provider_name(crew.source, names)}", crew.url)
            )
        if crew.wikipedia:
            url = WIKIPEDIA + crew.wikipedia.replace(" ", "_")
            credits.append(link_markup(f"Wikipedia: {crew.wikipedia}", url))
        self.crew_credit.set_markup("   ".join(credits))
        self.crew_credit.set_visible(bool(credits))

    # Pictures

    def _show_media(self, detail: DiskDetail, content: Content | None) -> None:
        disc_items = [item for item in detail.media if item.content_id is None]
        if content is not None:
            items = [item for item in detail.media if item.content_id == content.id]
            items += disc_items
        else:
            items = disc_items + [item for item in detail.media if item.content_id is not None]
        subjects: dict[int | None, str] = {None: detail.disk.label}
        subjects.update({c.id: display_title(c.title) for c in detail.contents})
        backend = self._host.backend
        reason = ""
        if not backend.settings.online_enabled:
            reason = "Online use is switched off in Preferences."
        self.media.show(items, subjects, enabled=backend.media_enabled, off_reason=reason)

    def _load_media(self, item: MediaItem) -> Gdk.Texture | None:
        """Runs on a worker thread."""
        path = self._host.backend.media_file(item)
        if path is None:
            return None
        return Gdk.Texture.new_from_filename(str(path))

    # Trivia

    def _trivia_items(self) -> list[TriviaItem]:
        if self.detail is None:
            return []
        own = [item for item in self.detail.trivia if item.content_id == self.content_id]
        if self.content_id is None:
            return own
        disc = [item for item in self.detail.trivia if item.content_id is None]
        return own + disc

    def _fill_trivia(self) -> None:
        clear_children(self.trivia_list)
        items = self._trivia_items() + list(self.summaries)
        for item in items:
            self.trivia_list.append(self._trivia_row(item))
        if self.summaries_loading:
            box = Gtk.Box(spacing=12, margin_top=12, margin_bottom=12, margin_start=12)
            spinner = Gtk.Spinner(spinning=True)
            box.append(spinner)
            box.append(caption_label("Looking up background information"))
            row = Gtk.ListBoxRow(activatable=False, child=box)
            self.trivia_list.append(row)
        self.trivia_group.set_visible(bool(items) or self.summaries_loading)

    def _trivia_row(self, item: TriviaItem) -> Gtk.ListBoxRow:
        box = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=6,
            margin_top=10,
            margin_bottom=10,
            margin_start=12,
            margin_end=12,
        )
        if item.kind == "summary" and item.title:
            heading = Gtk.Label(label=item.title, xalign=0, wrap=True)
            heading.add_css_class("heading")
            box.append(heading)
        box.append(_text_block(item.text))
        credit = fmt.trivia_credit(item, self._host.source_names())
        if credit:
            label = caption_label(dim=False, use_markup=True)
            label.set_markup(link_markup(credit, item.url))
            label.connect("activate-link", self._on_link)
            box.append(label)
        row = Gtk.ListBoxRow(activatable=False, child=box)
        row.trivia = item
        return row

    def _load_summaries(self) -> None:
        """Fetch Wikipedia summaries for what is shown, on a worker thread."""
        self._summary_generation += 1
        generation = self._summary_generation
        self.summaries = []
        self.summaries_loading = False
        detail = self.detail
        backend = self._host.backend
        if detail is None or not backend.media_enabled:
            self._fill_trivia()
            return
        key = (detail.disk.id, self.content_id)
        if key in self._summary_cache:
            self.summaries = list(self._summary_cache[key])
            self._fill_trivia()
            return
        self.summaries_loading = True
        self._fill_trivia()

        def done(items: list[TriviaItem]) -> None:
            self._summary_cache[key] = list(items)
            if generation == self._summary_generation:
                self.summaries = list(items)
                self.summaries_loading = False
                self._fill_trivia()

        def failed(_error: BaseException) -> None:
            if generation == self._summary_generation:
                self.summaries_loading = False
                self._fill_trivia()

        run_in_thread(lambda: backend.summaries(*key), done, failed, name="summaries")

    def forget_cached(self) -> None:
        """Drop fetched summaries, for example after online information was switched on."""
        self._summary_cache.clear()

    # Viruses

    def _infected_local(self) -> LocalFile | None:
        """The library file the writer would use, when the virus report is about it."""
        detail = self.detail
        if detail is None or detail.virus is None or not detail.virus.infected:
            return None
        return detail.write_local

    def _written_images(self) -> set[int]:
        """Dumps that carry the virus: the ones a clean alternate must differ from."""
        detail = self.detail
        if detail is None:
            return set()
        ids = {record.id for record in detail.images if record.virus}
        local = self._infected_local()
        if local is not None and local.image_id is not None:
            ids.add(local.image_id)
        return ids

    def _update_virus(self) -> None:
        detail = self.detail
        report: VirusReport | None = detail.virus if detail is not None else None
        infected = report is not None and report.infected
        self.virus_box.set_visible(infected)
        if not infected:
            return
        names = self._host.source_names()
        self.virus_heading.set_text(fmt.virus_heading(report))
        self.virus_body.set_text(fmt.virus_body(report, names))
        source = fmt.virus_source(report, names)
        self.virus_source.set_text(source)
        self.virus_source.set_visible(bool(source))
        alternates = {record.id: record for record in self.alternates}
        using_clean = self._chosen_image in alternates
        local = self._infected_local()
        self.remove_virus_row.set_visible(report.removable and not using_clean)
        self.clean_row.set_visible(report.removable and local is not None and not using_clean)
        if local is not None:
            self.clean_row.set_subtitle(fmt.local_file_location(local))
        self.clean_button.set_sensitive(not self.cleaning)
        self.alternate_row.set_visible(bool(alternates) and not using_clean)
        if alternates:
            self.alternate_row.set_subtitle(self.alternates[0].name)
        self.clean_chosen_row.set_visible(using_clean)
        if using_clean:
            self.clean_chosen_row.set_subtitle(alternates[self._chosen_image].name)
        self.virus_group.set_visible(
            any(
                row.get_visible()
                for row in (
                    self.remove_virus_row,
                    self.clean_row,
                    self.alternate_row,
                    self.clean_chosen_row,
                )
            )
        )

    def _on_use_clean(self, _button) -> None:
        if not self.alternates:
            return
        record = self.alternates[0]
        radio = self._radios.get(record.id)
        if radio is not None:
            radio.set_active(True)  # _choose_image updates the virus section
        else:
            self._chosen_image = record.id
            self._update_virus()
        toast(self, f"{record.name} will be written")

    def _on_clean_stored(self, _button) -> None:
        local = self._infected_local()
        report = self.detail.virus if self.detail is not None else None
        if local is None or report is None:
            return
        body = fmt.clean_explanation(local, report.name, self.detail.disk.platform)

        def respond(response: str) -> None:
            if response == "clean":
                self.clean_stored(local)

        alert(
            self,
            "Clean the Stored Image?",
            body,
            (("cancel", "_Cancel", ""), ("clean", "C_lean", "suggested")),
            respond,
        )

    def clean_stored(self, local: LocalFile) -> None:
        """Remove the virus from ``local`` on a worker thread."""
        if self.cleaning:
            return
        self.cleaning = True
        self._update_virus()
        backend = self._host.backend

        def done(cleaned: LocalFile) -> None:
            self.cleaning = False
            self._host.toast(fmt.cleaned_text(local, cleaned))
            self._host.library_changed()

        def failed(error: BaseException) -> None:
            self.cleaning = False
            self._update_virus()
            alert(self, "The File Could Not Be Cleaned", str(error))

        run_in_thread(lambda: backend.clean_file(local), done, failed, name="clean-file")

    # Dumps

    def _availability_text(self, detail: DiskDetail) -> str:
        if detail.availability == Availability.LOCAL:
            return "In your library"
        if detail.availability == Availability.ONLINE:
            names = self._host.provider_names()
            providers = dict.fromkeys(
                fmt.provider_name(location.provider, names) for location in detail.locations
            )
            return "Can be downloaded from " + ", ".join(providers) if providers else "Online"
        return "Not in your library or online"

    def _fill_dumps(self, detail: DiskDetail) -> None:
        self.dumps_group.set_title("Dumps")
        self.dump_rows.clear()
        self._radios = {}
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
            group_leader.set_active(self._chosen_image is None)
            self.dump_rows.add(best)
        for record in detail.images:
            subtitle = self._dump_subtitle(record, local_by_image, online_by_image, sources)
            row = _row(record.name, subtitle)
            local = local_by_image.get(record.id)
            virus = record.virus or (local.virus if local is not None else "")
            if virus:
                row.add_suffix(status_icon("dialog-warning-symbolic", "error", f"Virus: {virus}"))
            if len(detail.images) > 1:
                radio = self._radio(row, record, group_leader)
                radio.set_active(self._chosen_image == record.id)
            self.dump_rows.add(row)

    def _radio(self, row, record: ImageRecord | None, leader) -> Gtk.CheckButton:
        check = Gtk.CheckButton(valign=Gtk.Align.CENTER)
        set_accessible_label(check, record.name if record else "Best Available")
        if leader is not None:
            check.set_group(leader)
        row.add_prefix(check)
        row.set_activatable_widget(check)
        image_id = record.id if record else None
        self._radios[image_id] = check
        check.connect("toggled", lambda button: self._choose_image(button, image_id))
        return check

    def _choose_image(self, button: Gtk.CheckButton, image_id: int | None) -> None:
        if button.get_active():
            self._chosen_image = image_id
            self._update_virus()

    @staticmethod
    def _dump_subtitle(record: ImageRecord, local_by_image, online_by_image, names) -> str:
        parts = [record.format.upper()]
        if record.flags:
            parts.append(record.flags)
        if record.bad:
            parts.append("bad dump")
        if record.virus:
            parts.append(f"virus {record.virus}")
        if record.source:
            parts.append(f"listed by {fmt.provider_name(record.source, names)}")
        local = local_by_image.get(record.id)
        if local is not None:
            where = f"in your library at {fmt.local_file_location(local)}"
            if local.virus:
                where += f", with the {local.virus} virus"
            parts.append(where)
        elif record.id in online_by_image:
            parts.append(f"online at {online_by_image[record.id]}")
        return " • ".join(parts)

    # Unmatched files

    def show_local(self, local: LocalFile) -> None:
        self.detail = None
        self.local = local
        self.content_id = None
        self.edited_facts = {}
        self.title.set_text(local_name(local))
        parts = ["Unmatched file", (local.format or "").upper(), fmt.human_size(local.size)]
        self.subtitle.set_text(" • ".join(part for part in parts if part))
        self.condition.set_visible(False)
        self.media.show([], {}, enabled=False)
        self.media.set_visible(False)
        self.virus_box.set_visible(False)
        facts = [("Location", local.path), ("Inside Archive", local.member)]
        facts += [("Volume Label", local.volume_label), ("Virus", local.virus)]
        hashes = [
            f"{name} {value}"
            for name, value in (("MD5", local.md5), ("SHA-1", local.sha1), ("CRC32", local.crc32))
            if value
        ]
        facts.append(("Checksums", "\n".join(hashes)))
        self._set_facts(facts)
        self._load_local_boot_block(local)
        self.title_rows.clear()
        self.titles_group.set_title("Files on the Disk")
        for name in local.listing[:100]:
            self.title_rows.add(_row(name))
        self.titles_group.set_visible(bool(local.listing))
        self.titles_group.set_description(None)
        for group in (
            self.crew_group,
            self.trivia_group,
            self.dumps_group,
            self.copies_group,
            self.notes_group,
        ):
            group.set_visible(False)
        self.link_rows.clear()
        self.links_group.set_visible(False)
        self.write_button.set_sensitive(True)
        self.write_button.set_tooltip_text("Write this file to a floppy now")
        self.add_button.set_sensitive(True)
        self._actions.lookup_action("download").set_enabled(False)
        self._actions.lookup_action("show-in-files").set_enabled(True)
        self._actions.lookup_action("copy-label").set_enabled(True)
        self._actions.lookup_action("edit-details").set_enabled(False)
        self._actions.lookup_action("revert-details").set_enabled(False)
        self._actions.lookup_action("link-disc").set_enabled(not local.matched)
        self._stack.set_visible_child_name("content")

    def _load_local_boot_block(self, local: LocalFile) -> None:
        """Read the file's boot block on a worker thread; add it when it is not a virus."""
        backend = self._host.backend

        def done(report) -> None:
            if self.local is local and self.detail is None:
                self._add_fact("Boot Block", fmt.boot_block_text(report))

        def failed(error: BaseException) -> None:
            LOG.add("virus", f"The boot block of {local_name(local)} could not be read: {error}")

        run_in_thread(lambda: backend.virus_report(local), done, failed, name="boot-block")

    def _restore_disc_layout(self) -> None:
        self.media.set_visible(True)
        self.titles_group.set_title("On This Disc")
        self.titles_group.set_visible(True)

    # Actions

    def queue_item(self) -> QueueItem | None:
        if self.detail is not None:
            disk = self.detail.disk
            report = self.detail.virus
            clean = True
            if report is not None and report.infected and report.removable:
                clean = self.remove_virus_row.get_active()
            return item_from_disk(disk, self._chosen_image, clean_virus=clean)
        if self.local is not None:
            return item_from_local(self.local)
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
        """Show the file the writer would use, or else the first local copy."""
        if self.local is not None:
            show_in_files(self, self.local.path)
        elif self.detail is not None and self.detail.local_files:
            local = self.detail.write_local or self.detail.local_files[0]
            show_in_files(self, local.path)

    def _on_copy_label(self) -> None:
        if self.detail is not None:
            text = fmt.sticker_text(self.detail.disk.label, self.detail.contents)
        elif self.local is not None:
            text = self.local.volume_label or local_name(self.local)
        else:
            return
        copy_text(self, text)
        toast(self, f"Copied “{text}”")

    # Your copies and Link to Disc

    def _fill_copies(self, detail: DiskDetail) -> None:
        """The library files kept with this disc although they match none of its dumps."""
        self.copy_rows.clear()
        copies = [local for local in detail.local_files if local.image_id is None]
        for local in copies:
            row = _row(local_name(local), fmt.local_file_location(local))
            button = text_button("_Unlink", None, style="flat")
            button.set_valign(Gtk.Align.CENTER)
            button.set_tooltip_text("Stop counting this file as a copy of this disc")
            button.connect("clicked", lambda _button, file=local: self.confirm_unlink(file))
            row.add_suffix(button)
            self.copy_rows.add(row)
        self.copies_group.set_visible(bool(copies))

    def confirm_unlink(self, local: LocalFile) -> None:
        """Ask, then stop counting ``local`` as a copy of the disc shown."""
        label = self.detail.disk.label if self.detail is not None else "this disc"

        def respond(response: str) -> None:
            if response == "unlink":
                self.unlink(local)

        alert(
            self,
            "Unlink This File?",
            f"{local_name(local)} will no longer count as a copy of {label}. The file itself is "
            "not changed, and Link to Disc can link it again.",
            (("cancel", "_Cancel", ""), ("unlink", "_Unlink", "destructive")),
            respond,
        )

    def unlink(self, local: LocalFile) -> None:
        backend = self._host.backend

        def done(_result) -> None:
            toast(self, f"{local_name(local)} is no longer linked")
            self._host.library_changed()

        def failed(error: BaseException) -> None:
            alert(self, "The File Could Not Be Unlinked", str(error))

        run_in_thread(lambda: backend.unlink_file(local), done, failed, name="unlink-file")

    def link_disc(self) -> LinkDiscDialog | None:
        """Open Link to Disc for the unmatched file shown."""
        if self.local is None or self.local.matched:
            return None
        dialog = LinkDiscDialog(self.local, self._host.backend, self._link_chosen)
        dialog.connect("closed", lambda _dialog: setattr(self, "link_dialog", None))
        dialog.present(self.get_root())
        self.link_dialog = dialog
        return dialog

    def _link_chosen(self, dialog: LinkDiscDialog, disk) -> None:
        """Link the dialog's file to ``disk`` on a worker thread, then show the disc."""
        local = dialog.local
        backend = self._host.backend
        dialog.set_busy(True)

        def done(_result) -> None:
            dialog.close()
            toast(self, f"{local_name(local)} is linked to {disk.label}")
            self._host.library_changed()
            self._host.show_disc(disk.id)

        def failed(error: BaseException) -> None:
            dialog.set_busy(False)
            dialog.show_problem(str(error))

        run_in_thread(lambda: backend.link_file(local, disk.id), done, failed, name="link-file")

    # Edit Details

    def edit_details(self) -> EditDetailsDialog | None:
        """Open the Edit Details dialog for the disc shown."""
        if self.detail is None:
            return None
        dialog = EditDetailsDialog(self.detail, self._save_details, self.confirm_revert)
        dialog.connect("closed", lambda _dialog: setattr(self, "edit_dialog", None))
        dialog.present(self.get_root())
        self.edit_dialog = dialog
        return dialog

    def _save_details(
        self, dialog: EditDetailsDialog, values: dict[str, str], titles: dict[int, str]
    ) -> None:
        """Store the dialog's values on a worker thread; close it when they are kept."""
        disk_id = dialog.detail.disk.id
        backend = self._host.backend
        dialog.set_busy(True)

        def done(_result) -> None:
            dialog.close()
            self._host.details_changed()
            toast(self, "Details saved")

        def failed(error: BaseException) -> None:
            dialog.set_busy(False)
            dialog.show_problem(str(error))

        run_in_thread(
            lambda: backend.save_details(disk_id, values, titles), done, failed, name="save-details"
        )

    def confirm_revert(self, dialog: EditDetailsDialog | None = None) -> Adw.AlertDialog | None:
        """Ask, then forget the user's corrections of the disc shown."""
        detail = dialog.detail if dialog is not None else self.detail
        if detail is None or not (detail.edited or detail.edited_titles):
            return None
        disk_id = detail.disk.id
        backend = self._host.backend

        def reverted(_result) -> None:
            if dialog is not None:
                dialog.close()
            self._host.details_changed()
            toast(self, "The catalogue's details are shown again")

        def failed(error: BaseException) -> None:
            if dialog is not None:
                dialog.set_busy(False)
            alert(self, "The Details Could Not Be Reverted", str(error))

        def respond(response: str) -> None:
            if response != "revert":
                return
            if dialog is not None:
                dialog.set_busy(True)
            run_in_thread(
                lambda: backend.revert_details(disk_id), reverted, failed, name="revert-details"
            )

        return alert(
            dialog or self,
            "Revert to Catalogue?",
            f"Your changes to the details of {detail.disk.label} are forgotten and the "
            "catalogue's values show again.",
            (("cancel", "_Cancel", ""), ("revert", "_Revert", "destructive")),
            respond,
            default="cancel",
        )

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
            self.download_bar.set_text("Cancelling")

    def _on_link(self, _label, uri: str) -> bool:
        open_uri(self, uri)
        return True

    @property
    def downloading(self) -> bool:
        return self._download_cancel is not None


def queue_items_for_rows(rows) -> list[QueueItem]:
    """One queue item per disc among ``rows``, with the best dump, in order."""
    items: list[QueueItem] = []
    seen: set[int] = set()
    for row in rows:
        disk = row.disk
        if disk.id in seen:
            continue
        seen.add(disk.id)
        items.append(item_from_disk(disk))
    return items

"""The Edit Details dialog: the user's own values for a disc's details.

It edits the fields ``library.corrections`` allows (label, catalogue name,
crew, release date, publisher, cracker and notes) and the name of each title
on the disc. It checks the values as they are typed with the same rules the
backend stores them by (``correction_from_form``), and hands them to the
details pane, which saves them on a worker thread.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gtk  # noqa: E402

from ..catalogue.naming import display_title  # noqa: E402
from ..library.corrections import (  # noqa: E402
    CorrectionError,
    correction_from_form,
    field_value,
    parse_date,
)
from ..models import DiskDetail  # noqa: E402
from . import formatting as fmt  # noqa: E402
from .widgets import caption_label, set_accessible_label, text_button  # noqa: E402

# The one-line fields, in the order they are shown, with their row titles.
LINE_FIELDS = (
    ("label", "Label"),
    ("title", "Catalogue Name"),
    ("crew", "Crew"),
    ("date", "Release Date"),
    ("publisher", "Publisher"),
    ("cracker", "Cracked By"),
)
EDITED = "Edited"

SaveCallback = Callable[["EditDetailsDialog", dict[str, str], dict[int, str]], None]


def edited_label(original: str) -> Gtk.Label:
    """The small "Edited" note beside a value the user changed, naming the catalogue's value."""
    label = caption_label(EDITED, valign=Gtk.Align.CENTER)
    label.set_tooltip_text(
        f"Changed with Edit Details. The catalogue has: {original}"
        if original
        else "Changed with Edit Details. The catalogue has no value here."
    )
    return label


class EditDetailsDialog(Adw.Dialog):
    """Edits one disc's details; ``on_save(dialog, values, titles)`` stores them."""

    def __init__(
        self,
        detail: DiskDetail,
        on_save: SaveCallback,
        on_revert: Callable[[EditDetailsDialog], None] | None = None,
    ) -> None:
        super().__init__(title="Edit Details", content_width=540, content_height=680)
        self.detail = detail
        self._on_save = on_save
        # The catalogue's own values, which the form is compared with.
        self.original = detail.original or detail.disk
        catalogue_titles = dict(detail.edited_titles)
        self.original_contents = tuple(
            replace(content, title=catalogue_titles.get(content.id, content.title))
            for content in detail.contents
        )

        view = Adw.ToolbarView()
        header = Adw.HeaderBar(show_start_title_buttons=False, show_end_title_buttons=False)
        self.cancel_button = text_button("_Cancel", lambda _button: self.close())
        header.pack_start(self.cancel_button)
        self.save_button = text_button("_Save", self._save, style="suggested-action")
        header.pack_end(self.save_button)
        view.add_top_bar(header)

        page = Adw.PreferencesPage()
        self.problem_group = Adw.PreferencesGroup(visible=False)
        self.problem = Gtk.Label(xalign=0, wrap=True)
        self.problem.add_css_class("error")
        self.problem_group.add(self.problem)
        page.add(self.problem_group)

        disk = detail.disk
        disc = Adw.PreferencesGroup(
            title="Disc",
            description=(
                "Your changes are kept in your own database, never in the catalogue, and stay "
                "when the catalogue is updated. Enter the release date as YYYY, YYYY-MM or "
                "YYYY-MM-DD."
            ),
        )
        self.entries: dict[str, Adw.EntryRow] = {}
        for name, title in LINE_FIELDS:
            row = Adw.EntryRow(title=title)
            row.set_text(field_value(disk, name))
            if name in detail.edited:
                row.add_suffix(edited_label(field_value(self.original, name)))
            row.connect("changed", lambda _row: self.validate())
            disc.add(row)
            self.entries[name] = row
        page.add(disc)

        notes_group = Adw.PreferencesGroup(title="Notes")
        if "notes" in detail.edited:
            notes_group.set_header_suffix(edited_label(self.original.notes))
        self.notes = Gtk.TextView(
            wrap_mode=Gtk.WrapMode.WORD_CHAR,
            top_margin=8,
            bottom_margin=8,
            left_margin=10,
            right_margin=10,
            accepts_tab=False,
        )
        self.notes.get_buffer().set_text(disk.notes)
        set_accessible_label(self.notes, "Notes")
        frame = Gtk.ScrolledWindow(
            child=self.notes,
            hscrollbar_policy=Gtk.PolicyType.NEVER,
            min_content_height=96,
            max_content_height=240,
            propagate_natural_height=True,
        )
        frame.add_css_class("card")
        notes_group.add(frame)
        page.add(notes_group)

        self.title_entries: dict[int, Adw.EntryRow] = {}
        if detail.contents:
            titles_group = Adw.PreferencesGroup(title="Titles on This Disc")
            for index, content in enumerate(detail.contents, start=1):
                kind = fmt.CONTENT_KIND_NAMES.get(content.kind, "Title")
                row = Adw.EntryRow(title=f"{index}. {kind}")
                row.set_text(content.title)
                if content.id in catalogue_titles:
                    row.add_suffix(edited_label(display_title(catalogue_titles[content.id])))
                row.connect("changed", lambda _row: self.validate())
                titles_group.add(row)
                self.title_entries[content.id] = row
            page.add(titles_group)

        self.revert_group = Adw.PreferencesGroup(
            visible=bool(on_revert and (detail.edited or detail.edited_titles))
        )
        self.revert_button = text_button(
            "_Revert to Catalogue",
            lambda _button: on_revert(self) if on_revert else None,
            style="destructive-action pill",
            tooltip="Forget your changes to this disc and show the catalogue's values",
        )
        self.revert_button.set_halign(Gtk.Align.CENTER)
        self.revert_group.add(self.revert_button)
        page.add(self.revert_group)

        view.set_content(page)
        self.set_child(view)
        self.validate()

    # The form

    def values(self) -> dict[str, str]:
        """Every field as it is in the form, by field name."""
        values = {name: row.get_text() for name, row in self.entries.items()}
        buffer = self.notes.get_buffer()
        values["notes"] = buffer.get_text(buffer.get_start_iter(), buffer.get_end_iter(), False)
        return values

    def titles(self) -> dict[int, str]:
        """Every title name as it is in the form, by content id."""
        return {content_id: row.get_text() for content_id, row in self.title_entries.items()}

    def validate(self) -> str:
        """Check the form; show and return the problem, "" when it can be saved."""
        problem = ""
        try:
            correction_from_form(
                self.original, self.original_contents, self.values(), self.titles()
            )
        except CorrectionError as error:
            problem = str(error)
        date = self.entries["date"]
        try:
            parse_date(date.get_text())
        except CorrectionError:
            date.add_css_class("error")
        else:
            date.remove_css_class("error")
        self.show_problem(problem)
        self.save_button.set_sensitive(not problem)
        return problem

    def show_problem(self, text: str) -> None:
        self.problem.set_text(text)
        self.problem_group.set_visible(bool(text))

    def set_busy(self, busy: bool) -> None:
        """Keep the form still while the pane stores it."""
        for widget in (self.save_button, self.revert_button, self.cancel_button):
            widget.set_sensitive(not busy)
        if not busy:
            self.validate()

    def _save(self, _button=None) -> None:
        if self.validate():
            return
        self._on_save(self, self.values(), self.titles())

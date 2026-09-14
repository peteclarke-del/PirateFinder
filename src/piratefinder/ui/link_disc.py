"""The Link to Disc dialog: tie a library file that matches no dump to its disc.

A menu disk downloaded by hand from a forum, or dumped from a floppy the user
owns, matches no dump the catalogue knows, so nothing ties it to its disc.
The dialog searches the catalogue's discs, starting from the file's volume
label or name, and hands the chosen disc to the details pane, which links the
file on a worker thread (``Library.link_file``).
"""

from __future__ import annotations

import re
from collections.abc import Callable
from pathlib import PurePosixPath

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gtk  # noqa: E402

from ..images.inspect import local_platform  # noqa: E402
from ..library.library import local_name  # noqa: E402
from ..models import Disk, LocalFile, Query, ResultMode  # noqa: E402
from . import formatting as fmt  # noqa: E402
from .bridge import run_in_thread  # noqa: E402
from .widgets import caption_label, plain_row, set_accessible_label, text_button  # noqa: E402

# How many discs one search lists; a more precise search finds the rest.
RESULT_LIMIT = 50

LinkCallback = Callable[["LinkDiscDialog", Disk], None]


def search_guess(local: LocalFile) -> str:
    """What the search starts from: the volume label, else the file name as words.

    "AUTO_312.MSA" gives "AUTO 312", so a series alias and a number find the disc.
    """
    label = local.volume_label.strip()
    if label:
        return re.sub(r"[_.\-]+", " ", label).strip()
    stem = PurePosixPath(local_name(local)).stem
    words = re.sub(r"[_.\-]+", " ", stem)
    return re.sub(r"(?<=[A-Za-z])(?=\d)", " ", words).strip()


class LinkDiscDialog(Adw.Dialog):
    """Chooses the disc a library file is a copy of; ``on_link(dialog, disk)`` links it."""

    def __init__(self, local: LocalFile, backend, on_link: LinkCallback) -> None:
        super().__init__(title="Link to Disc", content_width=520, content_height=620)
        self.local = local
        self._backend = backend
        self._on_link = on_link
        self._generation = 0
        self._discs: dict[Gtk.ListBoxRow, Disk] = {}
        self.chosen: Disk | None = None
        self.platform = local_platform(local)

        view = Adw.ToolbarView()
        header = Adw.HeaderBar(show_start_title_buttons=False, show_end_title_buttons=False)
        self.cancel_button = text_button("_Cancel", lambda _button: self.close())
        header.pack_start(self.cancel_button)
        self.link_button = text_button("_Link", self._link, style="suggested-action")
        self.link_button.set_sensitive(False)
        header.pack_end(self.link_button)
        view.add_top_bar(header)

        body = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=12,
            margin_top=12,
            margin_bottom=18,
            margin_start=18,
            margin_end=18,
        )
        intro = Gtk.Label(
            label=(
                f"Choose the disc that {local_name(local)} is a copy of. PirateFinder then "
                "counts it as that disc in your library, for as long as the file is unchanged. "
                "The file itself is not changed."
            ),
            xalign=0,
            wrap=True,
        )
        body.append(intro)
        self.search_entry = Gtk.SearchEntry(
            placeholder_text="Disc, crew or title", hexpand=True, search_delay=250
        )
        set_accessible_label(self.search_entry, "Search for the disc")
        self.search_entry.set_text(search_guess(local))
        self.search_entry.connect("search-changed", lambda _entry: self.search())
        self.search_entry.connect("activate", lambda _entry: self._link())
        body.append(self.search_entry)
        self.problem = Gtk.Label(xalign=0, wrap=True, visible=False)
        self.problem.add_css_class("error")
        body.append(self.problem)
        self.status = caption_label()
        body.append(self.status)
        self.list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.SINGLE)
        self.list.add_css_class("boxed-list")
        set_accessible_label(self.list, "Discs")
        self.list.connect("row-selected", self._on_selected)
        self.list.connect("row-activated", lambda _list, _row: self._link())
        scroller = Gtk.ScrolledWindow(
            child=self.list, hscrollbar_policy=Gtk.PolicyType.NEVER, vexpand=True
        )
        body.append(scroller)
        view.set_content(body)
        self.set_child(view)
        self.set_focus(self.search_entry)
        self.search()

    # Searching

    def search(self) -> None:
        """List the discs the search text finds, on the platform of the file when known."""
        text = self.search_entry.get_text().strip()
        self._generation += 1
        generation = self._generation
        self._show_discs([])
        if not text:
            self.status.set_text("Type a disc, crew or title to find the disc.")
            return
        self.status.set_text("Searching")
        query = Query(
            text=text, platform=self.platform, mode=ResultMode.DISCS, page_size=RESULT_LIMIT
        )
        backend = self._backend

        def found(page) -> None:
            if generation != self._generation:
                return
            discs = [row.disk for row in page.rows]
            self._show_discs(discs)
            if not discs:
                self.status.set_text("No disc matches. Try the crew and the disc number.")
            elif page.total > len(discs):
                self.status.set_text(
                    f"The first {len(discs)} of {page.total:,} discs. Add words to narrow it."
                )
            else:
                self.status.set_text(f"{len(discs)} disc{'s' if len(discs) != 1 else ''}")

        def failed(error: BaseException) -> None:
            if generation == self._generation:
                self.status.set_text(f"The catalogue could not be searched: {error}")

        run_in_thread(lambda: backend.search_page(query), found, failed, name="link-search")

    def _show_discs(self, discs: list[Disk]) -> None:
        self.list.remove_all()
        self._discs = {}
        self._on_selected(self.list, None)
        for disk in discs:
            row = plain_row(title=disk.label, subtitle=self.subtitle(disk))
            row.set_activatable(True)
            self.list.append(row)
            self._discs[row] = disk
        self.list.set_visible(bool(discs))

    @staticmethod
    def subtitle(disk: Disk) -> str:
        """ "Automation • June 1991 • Atari ST • Menu disk", with the crew when it differs."""
        text = fmt.disk_subtitle(disk)
        if disk.crew and disk.crew != disk.series_name:
            text = f"{disk.crew} • {text}" if text else disk.crew
        return text

    @property
    def discs(self) -> list[Disk]:
        return list(self._discs.values())

    # Choosing

    def choose(self, disk_id: int) -> None:
        """Select the listed disc with this id."""
        for row, disk in self._discs.items():
            if disk.id == disk_id:
                self.list.select_row(row)
                return

    def _on_selected(self, _list, row: Gtk.ListBoxRow | None) -> None:
        self.chosen = self._discs.get(row) if row is not None else None
        self.link_button.set_sensitive(self.chosen is not None)

    def _link(self, _button=None) -> None:
        if self.chosen is not None and self.link_button.get_sensitive():
            self.show_problem("")
            self._on_link(self, self.chosen)

    def show_problem(self, text: str) -> None:
        self.problem.set_text(text)
        self.problem.set_visible(bool(text))

    def set_busy(self, busy: bool) -> None:
        """Keep the dialog still while the pane links the file."""
        for widget in (self.cancel_button, self.search_entry, self.list):
            widget.set_sensitive(not busy)
        self.link_button.set_sensitive(not busy and self.chosen is not None)

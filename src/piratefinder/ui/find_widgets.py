"""The parts of the Find page: filter drop-downs, the results table and the pager."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gdk, Gio, GObject, Gtk, Pango  # noqa: E402

from ..models import (  # noqa: E402
    Availability,
    Query,
    ResultMode,
    ResultPage,
    ResultRow,
    SortOrder,
)
from . import formatting as fmt  # noqa: E402
from .widgets import icon_button, set_accessible_label  # noqa: E402

# Filter drop-downs


class WrapBox(Gtk.Widget):
    """Children side by side at their natural width, wrapping onto more lines.

    Gtk.FlowBox lines its children up in columns and stretches them to fill
    each line; the filter bar wants each control only as wide as it needs.
    (Adw.WrapBox does this from libadwaita 1.7.)
    """

    __gtype_name__ = "PirateFinderWrapBox"

    def __init__(self, spacing: int = 6, line_spacing: int = 6) -> None:
        super().__init__()
        self.spacing = spacing
        self.line_spacing = line_spacing
        self._children: list[Gtk.Widget] = []

    def append(self, child: Gtk.Widget) -> None:
        child.set_parent(self)
        self._children.append(child)

    @property
    def children(self) -> list[Gtk.Widget]:
        return list(self._children)

    def _shown(self) -> list[Gtk.Widget]:
        return [child for child in self._children if child.should_layout()]

    def _lines(self, width: int) -> list[list[tuple[Gtk.Widget, int]]]:
        lines: list[list[tuple[Gtk.Widget, int]]] = [[]]
        used = 0
        for child in self._shown():
            minimum, natural, _b, _l = child.measure(Gtk.Orientation.HORIZONTAL, -1)
            size = max(minimum, min(natural, width)) if width > 0 else natural
            if lines[-1] and width > 0 and used + self.spacing + size > width:
                lines.append([])
                used = 0
            used += (self.spacing if lines[-1] else 0) + size
            lines[-1].append((child, size))
        return [line for line in lines if line]

    def _line_height(self, line: list[tuple[Gtk.Widget, int]]) -> int:
        return max(child.measure(Gtk.Orientation.VERTICAL, size)[1] for child, size in line)

    def do_get_request_mode(self) -> Gtk.SizeRequestMode:
        return Gtk.SizeRequestMode.HEIGHT_FOR_WIDTH

    def do_measure(self, orientation: Gtk.Orientation, for_size: int):
        shown = self._shown()
        if orientation == Gtk.Orientation.HORIZONTAL:
            sizes = [child.measure(orientation, -1) for child in shown]
            minimum = max((size[0] for size in sizes), default=0)
            natural = sum(size[1] for size in sizes) + self.spacing * max(0, len(sizes) - 1)
            return minimum, natural, -1, -1
        lines = self._lines(for_size)
        height = sum(self._line_height(line) for line in lines)
        height += self.line_spacing * max(0, len(lines) - 1)
        return height, height, -1, -1

    def do_size_allocate(self, width: int, height: int, baseline: int) -> None:
        rtl = self.get_direction() == Gtk.TextDirection.RTL
        y = 0
        for line in self._lines(width):
            line_height = self._line_height(line)
            x = 0
            for child, size in line:
                area = Gdk.Rectangle()  # its constructor takes no positions
                area.x = width - x - size if rtl else x
                area.y = y
                area.width = size
                area.height = line_height
                child.size_allocate(area, -1)
                x += size + self.spacing
            y += line_height + self.line_spacing

    def do_dispose(self) -> None:
        for child in self._children:
            child.unparent()
        self._children = []


class ChoiceItem(GObject.Object):
    """One choice in a filter drop-down: what it shows, how many discs, its value."""

    __gtype_name__ = "PirateFinderChoice"

    name = GObject.Property(type=str, default="")

    def __init__(self, value: Any, name: str, count: int | None = None) -> None:
        super().__init__()
        self.value = value
        self.name = name
        self.count = count


class ChoiceDropDown(Gtk.DropDown):
    """A drop-down of ``ChoiceItem``; the list shows each choice's count.

    The first choice is always "any" (value None or ""). ``set_choices``
    keeps the selected value when it is still offered, without reporting a
    change.

    The list is as wide as its longest name, up to ``NAME_CHARS`` characters:
    the popover gives each row only its minimum width, so a name that may be
    shortened needs a minimum of its own ("Applications" showed as "Ap...").
    A longer name is shortened and shown whole in its tooltip.
    """

    NAME_CHARS = 32

    def __init__(self, label: str, tooltip: str, *, search: bool = False) -> None:
        self.store = Gio.ListStore(item_type=ChoiceItem)
        super().__init__(
            model=self.store,
            expression=Gtk.PropertyExpression.new(ChoiceItem, None, "name"),
            enable_search=search,
            tooltip_text=tooltip,
            valign=Gtk.Align.CENTER,
        )
        if search:
            self.set_property("search-match-mode", Gtk.StringFilterMatchMode.SUBSTRING)
        set_accessible_label(self, label)
        button_factory = Gtk.SignalListItemFactory()
        button_factory.connect("setup", self._setup_button)
        button_factory.connect("bind", self._bind_button)
        self.set_factory(button_factory)
        list_factory = Gtk.SignalListItemFactory()
        list_factory.connect("setup", self._setup_row)
        list_factory.connect("bind", self._bind_row)
        self.set_list_factory(list_factory)
        self.name_chars = 0  # the width of the list's names, in characters
        self._callbacks: list[Callable[[], None]] = []
        self._quiet = False
        self.connect("notify::selected", self._on_selected)

    @staticmethod
    def _setup_button(_factory, item: Gtk.ListItem) -> None:
        item.set_child(Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.END, max_width_chars=22))

    @staticmethod
    def _bind_button(_factory, item: Gtk.ListItem) -> None:
        item.get_child().set_text(item.get_item().name)

    def _setup_row(self, _factory, item: Gtk.ListItem) -> None:
        box = Gtk.Box(spacing=12)
        name = Gtk.Label(xalign=0, hexpand=True, ellipsize=Pango.EllipsizeMode.END)
        name.set_max_width_chars(self.NAME_CHARS)
        box.append(name)
        count = Gtk.Label(xalign=1)
        count.add_css_class("dim-label")
        count.add_css_class("numeric")
        box.append(count)
        item.set_child(box)

    def _bind_row(self, _factory, item: Gtk.ListItem) -> None:
        choice = item.get_item()
        name = item.get_child().get_first_child()
        count = name.get_next_sibling()
        name.set_width_chars(self.name_chars)
        name.set_text(choice.name)
        name.set_tooltip_text(choice.name if len(choice.name) > self.NAME_CHARS else None)
        count.set_text(f"{choice.count:,}" if choice.count is not None else "")
        count.set_visible(choice.count is not None)

    def set_choices(self, choices: Sequence[tuple[Any, str, int | None]]) -> None:
        current = self.value
        self.name_chars = min(
            max((len(name) for _v, name, _c in choices), default=0), self.NAME_CHARS
        )
        self._quiet = True
        try:
            self.store.splice(
                0, self.store.get_n_items(), [ChoiceItem(*choice) for choice in choices]
            )
            self.set_selected(self._index(current))
        finally:
            self._quiet = False

    def _index(self, value: Any) -> int:
        for index in range(self.store.get_n_items()):
            if self.store.get_item(index).value == value:
                return index
        return 0

    @property
    def value(self) -> Any:
        item = self.get_selected_item()
        return item.value if item is not None else None

    def set_value(self, value: Any, *, quiet: bool = False) -> None:
        self._quiet = quiet
        try:
            self.set_selected(self._index(value))
        finally:
            self._quiet = False

    @property
    def choices(self) -> list[Any]:
        return [self.store.get_item(index).value for index in range(self.store.get_n_items())]

    def on_changed(self, callback: Callable[[], None]) -> None:
        self._callbacks.append(callback)

    def _on_selected(self, *_args) -> None:
        if not self._quiet:
            for callback in self._callbacks:
                callback()


# The results table


class RowObject(GObject.Object):
    __gtype_name__ = "PirateFinderResultRow"

    def __init__(self, row: ResultRow) -> None:
        super().__init__()
        self.row = row


# Column id, header, width (0 expands), sort orders for ascending and descending.
COLUMNS: tuple[tuple[str, str, int, SortOrder | None, SortOrder | None], ...] = (
    ("check", "", 36, None, None),
    ("title", "Title", 0, SortOrder.TITLE, SortOrder.TITLE_DESC),
    ("disc", "Disc", 150, SortOrder.DISC, None),
    ("contents", "Contents", 0, None, None),
    ("crew", "Crew", 120, SortOrder.CREW, None),
    ("type", "Type", 100, None, None),
    ("year", "Year", 58, SortOrder.YEAR, SortOrder.YEAR_DESC),
    ("platform", "Platform", 82, SortOrder.PLATFORM, None),
    ("availability", "Availability", 104, None, None),
    ("virus", "", 34, None, None),
)
MODE_COLUMNS = {
    ResultMode.TITLES: ("check", "title", "disc", "crew", "type", "year", "platform"),
    ResultMode.DISCS: ("check", "disc", "contents", "crew", "type", "year", "platform"),
}
TRAILING_COLUMNS = ("availability", "virus")
DISC_WIDTH = {ResultMode.TITLES: 150, ResultMode.DISCS: 200}


class ResultsTable(Gtk.ScrolledWindow):
    """A table of result rows. Sorting by a column header asks for a new query.

    The model is never sorted here: a header click calls ``on_sort`` with
    the matching ``SortOrder`` and the page shows whatever the backend
    returns. ``is_checked(key)`` and ``on_checked(row, active)`` keep the
    tick boxes, ``on_selected(row)`` is called when a row is selected.
    """

    def __init__(
        self,
        *,
        is_checked: Callable[[str], bool],
        on_checked: Callable[[ResultRow, bool], None],
        on_selected: Callable[[ResultRow], None],
        on_sort: Callable[[SortOrder], None],
        on_activate: Callable[[ResultRow], None],
    ) -> None:
        super().__init__(vexpand=True, hexpand=True)
        self._is_checked = is_checked
        self._on_checked = on_checked
        self._on_selected = on_selected
        self._on_sort = on_sort
        self._on_activate = on_activate
        self.mode = ResultMode.TITLES
        self.words: list[str] = []
        self._syncing = False
        self._checks: dict[Gtk.CheckButton, RowObject] = {}

        self.store = Gio.ListStore(item_type=RowObject)
        self.selection = Gtk.SingleSelection(model=self.store, autoselect=False, can_unselect=True)
        self.selection.connect("notify::selected", self._selected)
        self.view = Gtk.ColumnView(
            model=self.selection,
            show_row_separators=True,
            reorderable=False,
            tab_behavior=Gtk.ListTabBehavior.ITEM,
        )
        self.view.add_css_class("data-table")
        set_accessible_label(self.view, "Search results")
        self.view.connect("activate", self._activated)
        self.columns: dict[str, Gtk.ColumnViewColumn] = {}
        self.sort_columns: dict[SortOrder, tuple[str, Gtk.SortType]] = {}
        for column_id, title, width, ascending, descending in COLUMNS:
            factory = Gtk.SignalListItemFactory()
            factory.connect("setup", self._setup_cell, column_id)
            factory.connect("bind", self._bind_cell, column_id)
            factory.connect("unbind", self._unbind_cell, column_id)
            column = Gtk.ColumnViewColumn(title=title, factory=factory)
            if width:
                column.set_fixed_width(width)
            column.set_expand(width == 0)
            column.set_resizable(column_id not in ("check", "virus"))
            if ascending is not None:
                # The sorter only makes the header clickable; see _sort_changed.
                column.set_sorter(Gtk.CustomSorter.new(None))
                self.sort_columns[ascending] = (column_id, Gtk.SortType.ASCENDING)
            if descending is not None:
                self.sort_columns[descending] = (column_id, Gtk.SortType.DESCENDING)
            column.column_id = column_id
            self.columns[column_id] = column
            self.view.append_column(column)
        self.view.get_sorter().connect("changed", self._sort_changed)
        keys = Gtk.EventControllerKey(propagation_phase=Gtk.PropagationPhase.CAPTURE)
        keys.connect("key-pressed", self._on_key)
        self.view.add_controller(keys)
        self.set_child(self.view)
        self._arrange()

    # Columns

    def _arrange(self) -> None:
        wanted = [*MODE_COLUMNS[self.mode], *TRAILING_COLUMNS]
        for position, column_id in enumerate(wanted):
            self.view.insert_column(position, self.columns[column_id])
        # Only the columns that differ between the modes are shown or hidden
        # here; the window hides others when it is narrow.
        for column_id in ("title", "contents"):
            self.columns[column_id].set_visible(column_id in wanted)
        # The disc is the first thing a row names in Discs mode, so it gets more room.
        self.columns["disc"].set_fixed_width(DISC_WIDTH[self.mode])

    def set_sort(self, sort: SortOrder) -> None:
        """Show ``sort`` on the column headers without asking for a new query."""
        target = self.sort_columns.get(sort)
        column = self.columns[target[0]] if target else None
        if column is not None and not column.get_visible():
            column = None
        self._syncing = True
        try:
            if column is None:
                self.view.sort_by_column(None, Gtk.SortType.ASCENDING)
            else:
                self.view.sort_by_column(column, target[1])
        finally:
            self._syncing = False

    def _sort_changed(self, sorter: Gtk.ColumnViewSorter, _change) -> None:
        if self._syncing:
            return
        column = sorter.get_primary_sort_column()
        if column is None:
            self._on_sort(SortOrder.RELEVANCE)
            return
        wanted = sorter.get_primary_sort_order()
        orders = {
            direction: order
            for order, (column_id, direction) in self.sort_columns.items()
            if column_id == column.column_id
        }
        order = orders.get(wanted)
        if order is None:
            # A second click on a column that sorts one way only: back to relevance.
            self.set_sort(SortOrder.RELEVANCE)
            self._on_sort(SortOrder.RELEVANCE)
            return
        self._on_sort(order)

    # Rows

    def set_rows(self, rows: Sequence[ResultRow], mode: ResultMode, words: Sequence[str]) -> None:
        if mode != self.mode:
            self.mode = mode
            self._arrange()
        self.words = list(words)
        self.columns["virus"].set_visible(any(row.virus for row in rows))
        self.store.splice(0, self.store.get_n_items(), [RowObject(row) for row in rows])
        self.selection.set_selected(Gtk.INVALID_LIST_POSITION)

    @property
    def rows(self) -> list[ResultRow]:
        return [self.store.get_item(index).row for index in range(self.store.get_n_items())]

    def selected_row(self) -> ResultRow | None:
        item = self.selection.get_selected_item()
        return item.row if item is not None else None

    def select_key(self, keys: Sequence[str], *, notify: bool = False) -> bool:
        """Select the first row whose key is in ``keys``; False when none is on the page."""
        for index in range(self.store.get_n_items()):
            if self.store.get_item(index).row.key in keys:
                self._select(index, notify)
                return True
        return False

    def select_index(self, index: int, *, notify: bool = True) -> None:
        if 0 <= index < self.store.get_n_items():
            self._select(index, notify)

    def _select(self, index: int, notify: bool) -> None:
        self._syncing_selection = not notify
        try:
            self.selection.set_selected(index)
        finally:
            self._syncing_selection = False
        self.view.scroll_to(index, None, Gtk.ListScrollFlags.NONE, None)

    def unselect(self) -> None:
        self._syncing_selection = True
        try:
            self.selection.set_selected(Gtk.INVALID_LIST_POSITION)
        finally:
            self._syncing_selection = False

    _syncing_selection = False

    def _selected(self, selection: Gtk.SingleSelection, _property) -> None:
        item = selection.get_selected_item()
        if item is not None and not self._syncing_selection:
            self._on_selected(item.row)

    def _activated(self, _view, position: int) -> None:
        item = self.store.get_item(position)
        if item is not None:
            self._on_activate(item.row)

    def refresh_checks(self) -> None:
        for check, item in self._checks.items():
            self._set_check(check, item.row)

    def _on_key(self, _controller, keyval: int, _keycode: int, state) -> bool:
        if keyval != Gdk.KEY_space or state & Gtk.accelerator_get_default_mod_mask():
            return False
        focus = self.get_root().get_focus() if self.get_root() is not None else None
        if isinstance(focus, Gtk.CheckButton):
            return False  # the tick box toggles itself
        row = self.selected_row()
        if row is None:
            return False
        self._on_checked(row, not self._is_checked(row.key))
        self.refresh_checks()
        return True

    # Cells

    def _setup_cell(self, _factory, item: Gtk.ListItem, column_id: str) -> None:
        if column_id == "check":
            check = Gtk.CheckButton(halign=Gtk.Align.CENTER, tooltip_text="Select for writing")
            check.handler = check.connect("toggled", self._toggled)
            item.set_child(check)
        elif column_id == "availability":
            box = Gtk.Box(spacing=6)
            box.append(Gtk.Image(pixel_size=16))
            box.append(Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.END))
            item.set_child(box)
        elif column_id == "virus":
            image = Gtk.Image(icon_name="dialog-warning-symbolic", halign=Gtk.Align.CENTER)
            image.add_css_class("error")
            item.set_child(image)
        else:
            label = Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.END, single_line_mode=True)
            if column_id == "year":
                label.add_css_class("numeric")
            item.set_child(label)

    def _bind_cell(self, _factory, item: Gtk.ListItem, column_id: str) -> None:
        row_object = item.get_item()
        row: ResultRow = row_object.row
        child = item.get_child()
        disk = row.disk
        if column_id == "check":
            self._checks[child] = row_object
            self._set_check(child, row)
            label = row.title or disk.label
            set_accessible_label(child, f"Select {label}")
        elif column_id == "availability":
            icon = child.get_first_child()
            text = icon.get_next_sibling()
            name = fmt.AVAILABILITY_ICONS[row.availability]
            icon.set_from_icon_name(name or "action-unavailable-symbolic")
            text.set_text(fmt.AVAILABILITY_NAMES[row.availability])
            child.set_tooltip_text(fmt.AVAILABILITY_TOOLTIPS[row.availability])
            for widget in (icon, text):
                if row.availability == Availability.LOCAL:
                    widget.add_css_class("success")
                else:
                    widget.remove_css_class("success")
                if row.availability == Availability.MISSING:
                    widget.add_css_class("dim-label")
                else:
                    widget.remove_css_class("dim-label")
        elif column_id == "virus":
            child.set_visible(bool(row.virus))
            tooltip = f"Known virus: {row.virus}" if row.virus else ""
            child.set_tooltip_text(tooltip or None)
            if tooltip:
                set_accessible_label(child, tooltip)
        else:
            text, markup = self._cell_text(row, column_id)
            if markup:
                child.set_markup(text)
            else:
                child.set_text(text)
            full = child.get_text()
            child.set_tooltip_text(full if len(full) > 18 else None)

    def _cell_text(self, row: ResultRow, column_id: str) -> tuple[str, bool]:
        disk = row.disk
        if column_id == "title":
            return fmt.highlight_markup(row.title or disk.label, self.words), True
        if column_id == "disc":
            return fmt.highlight_markup(disk.label, self.words), True
        if column_id == "contents":
            return fmt.summary_markup(row.summary, row.matched), True
        if column_id == "crew":
            return fmt.highlight_markup(disk.crew, self.words), True
        if column_id == "type":
            if self.mode == ResultMode.TITLES and row.content_kind is not None:
                return fmt.CONTENT_KIND_NAMES.get(row.content_kind, ""), False
            return disk.category, False
        if column_id == "year":
            return fmt.disk_year(disk), False
        if column_id == "platform":
            return fmt.platform_name(disk.platform), False
        return "", False

    def _unbind_cell(self, _factory, item: Gtk.ListItem, column_id: str) -> None:
        if column_id == "check":
            self._checks.pop(item.get_child(), None)

    def _set_check(self, check: Gtk.CheckButton, row: ResultRow) -> None:
        with check.handler_block(check.handler):
            check.set_active(self._is_checked(row.key))

    def _toggled(self, check: Gtk.CheckButton) -> None:
        item = self._checks.get(check)
        if item is not None:
            self._on_checked(item.row, check.get_active())


# The pager


class Pager(Gtk.Box):
    """ "101 to 200 of 1,234 titles", page buttons, a page number and the page size."""

    def __init__(self, on_page: Callable[[int], None], on_page_size: Callable[[int], None]):
        super().__init__(spacing=6)
        self.add_css_class("pager")
        self.add_css_class("toolbar")
        self._on_page = on_page
        self._on_page_size = on_page_size
        self.page = 0
        self.pages = 1
        self._quiet = False

        self.range_label = Gtk.Label(xalign=0, hexpand=True, ellipsize=Pango.EllipsizeMode.END)
        self.range_label.add_css_class("numeric")
        self.append(self.range_label)
        self.spinner = Gtk.Spinner(visible=False)
        self.append(self.spinner)

        self.first_button = icon_button("go-first-symbolic", "First Page", lambda _b: self._go(0))
        self.previous_button = icon_button(
            "go-previous-symbolic",
            "Previous Page",
            lambda _b: self._go(self.page - 1),
        )
        self.previous_button.set_tooltip_text("Previous Page (Ctrl+Page Up)")
        self.append(self.first_button)
        self.append(self.previous_button)
        page_label = Gtk.Label.new_with_mnemonic("_Page")
        self.append(page_label)
        self.page_entry = Gtk.Entry(
            width_chars=4,
            max_width_chars=6,
            xalign=1,
            input_purpose=Gtk.InputPurpose.DIGITS,
            valign=Gtk.Align.CENTER,
        )
        self.page_entry.add_css_class("numeric")
        page_label.set_mnemonic_widget(self.page_entry)
        set_accessible_label(self.page_entry, "Page number")
        self.page_entry.connect("activate", self._entry_activated)
        focus = Gtk.EventControllerFocus()
        focus.connect("leave", lambda _controller: self._show_page_number())
        self.page_entry.add_controller(focus)
        self.append(self.page_entry)
        self.of_label = Gtk.Label()
        self.of_label.add_css_class("numeric")
        self.append(self.of_label)
        self.next_button = icon_button(
            "go-next-symbolic", "Next Page", lambda _b: self._go(self.page + 1)
        )
        self.next_button.set_tooltip_text("Next Page (Ctrl+Page Down)")
        self.last_button = icon_button(
            "go-last-symbolic", "Last Page", lambda _b: self._go(self.pages - 1)
        )
        self.append(self.next_button)
        self.append(self.last_button)

        self.size_box = Gtk.Box(spacing=6, hexpand=True, halign=Gtk.Align.END)
        size_label = Gtk.Label.new_with_mnemonic("_Rows per Page")
        self.size_box.append(size_label)
        self.size_dropdown = Gtk.DropDown.new_from_strings([str(size) for size in fmt.PAGE_SIZES])
        self.size_dropdown.set_valign(Gtk.Align.CENTER)
        self.size_dropdown.set_tooltip_text("How many rows each page shows")
        size_label.set_mnemonic_widget(self.size_dropdown)
        set_accessible_label(self.size_dropdown, "Rows per page")
        self.size_dropdown.connect("notify::selected", self._size_changed)
        self.size_box.append(self.size_dropdown)
        self.append(self.size_box)
        self.set_page_size(Query().page_size)

    @property
    def page_size(self) -> int:
        return fmt.PAGE_SIZES[self.size_dropdown.get_selected()]

    def set_page_size(self, size: int) -> None:
        index = fmt.PAGE_SIZES.index(size) if size in fmt.PAGE_SIZES else 1
        self._quiet = True
        try:
            self.size_dropdown.set_selected(index)
        finally:
            self._quiet = False

    def update(self, page: ResultPage) -> None:
        self.page = page.query.page
        self.pages = page.pages
        self.range_label.set_text(fmt.range_text(page))
        self._show_page_number()
        self.of_label.set_text(f"of {self.pages:,}")
        self.first_button.set_sensitive(self.page > 0)
        self.previous_button.set_sensitive(self.page > 0)
        self.next_button.set_sensitive(self.page < self.pages - 1)
        self.last_button.set_sensitive(self.page < self.pages - 1)
        self.page_entry.set_sensitive(self.pages > 1)

    def set_busy(self, busy: bool) -> None:
        self.spinner.set_visible(busy)
        if busy:
            self.spinner.start()
        else:
            self.spinner.stop()

    def go(self, page: int) -> bool:
        """Ask for ``page`` (from 0) when it exists; False when it does not."""
        return self._go(page)

    def _go(self, page: int) -> bool:
        if not 0 <= page < self.pages or page == self.page:
            return False
        self._on_page(page)
        return True

    def _show_page_number(self) -> None:
        self.page_entry.set_text(str(self.page + 1))

    def _entry_activated(self, entry: Gtk.Entry) -> None:
        text = entry.get_text().strip()
        if text.isdigit() and self._go(min(max(int(text), 1), self.pages) - 1):
            return
        self._show_page_number()

    def _size_changed(self, *_args) -> None:
        if not self._quiet:
            self._on_page_size(self.page_size)

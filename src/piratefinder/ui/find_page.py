"""The Find page: search entry, filters, results and the detail pane."""

from __future__ import annotations

from collections import Counter
from dataclasses import replace

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gio, GObject, Gtk, Pango  # noqa: E402

from ..models import Availability, DiskKind, Platform, SearchFilters, SearchResult  # noqa: E402
from . import formatting as fmt  # noqa: E402
from .backend import CatalogueInfo  # noqa: E402
from .bridge import run_in_thread  # noqa: E402
from .detail_pane import DetailPane, make_item_for_result  # noqa: E402
from .help_content import EXAMPLE_SEARCHES  # noqa: E402
from .log import LOG  # noqa: E402
from .widgets import text_button  # noqa: E402

PLATFORM_CHOICES = (
    (None, "All Platforms"),
    (Platform.AMIGA, "Amiga"),
    (Platform.ATARI_ST, "Atari ST"),
)
SEARCH_DELAY_MS = 250
MAX_EXAMPLES = 4


class ResultObject(GObject.Object):
    """A search result in a Gio.ListStore."""

    __gtype_name__ = "PirateFinderResult"

    def __init__(self, result: SearchResult) -> None:
        super().__init__()
        self.result = result


class ResultRow(Gtk.Box):
    """One row of the results list. Rows are recycled by the list view."""

    def __init__(self, page: FindPage) -> None:
        super().__init__(spacing=12, margin_top=8, margin_bottom=8, margin_start=6, margin_end=6)
        self._page = page
        self.result: SearchResult | None = None
        self.check = Gtk.CheckButton(valign=Gtk.Align.CENTER, tooltip_text="Select for writing")
        self._toggle_handler = self.check.connect("toggled", self._on_toggled)
        self.append(self.check)

        grid = Gtk.Grid(column_spacing=12, row_spacing=2, hexpand=True)
        self.title = Gtk.Label(xalign=0, hexpand=True, ellipsize=Pango.EllipsizeMode.END)
        self.title.add_css_class("heading")
        grid.attach(self.title, 0, 0, 1, 1)
        self.platform = Gtk.Label(xalign=1)
        self.platform.add_css_class("caption")
        self.platform.add_css_class("dim-label")
        grid.attach(self.platform, 1, 0, 1, 1)
        self.date = Gtk.Label(xalign=1, width_chars=4)
        self.date.add_css_class("caption")
        self.date.add_css_class("dim-label")
        grid.attach(self.date, 2, 0, 1, 1)

        self.summary = Gtk.Label(
            xalign=0, hexpand=True, ellipsize=Pango.EllipsizeMode.END, use_markup=True
        )
        self.summary.add_css_class("dim-label")
        grid.attach(self.summary, 0, 1, 1, 1)
        availability = Gtk.Box(spacing=4, halign=Gtk.Align.END)
        self.availability_icon = Gtk.Image(pixel_size=14)
        availability.append(self.availability_icon)
        self.availability = Gtk.Label(xalign=1)
        self.availability.add_css_class("caption")
        availability.append(self.availability)
        self.availability_box = availability
        grid.attach(availability, 1, 1, 2, 1)
        self.append(grid)

    def bind(self, result: SearchResult) -> None:
        self.result = result
        with self.check.handler_block(self._toggle_handler):
            self.check.set_active(result.key in self._page.checked)
        if result.disk is not None:
            disk = result.disk
            self.title.set_text(disk.label)
            self.platform.set_text(fmt.platform_name(disk.platform))
            self.date.set_text(disk.date[:4])
            summary = result.summary or disk.title
        else:
            local = result.local
            self.title.set_text(fmt.local_file_name(local))
            self.platform.set_text((local.format or "").upper())
            self.date.set_text("")
            summary = "Unmatched file"
            if result.summary:
                summary += f": {result.summary}"
        self.summary.set_markup(fmt.summary_markup(summary, result.matched))
        self.check.update_property(
            [Gtk.AccessibleProperty.LABEL], [f"Select {self.title.get_text()}"]
        )
        availability = result.availability
        icon = fmt.AVAILABILITY_ICONS[availability]
        self.availability_icon.set_visible(bool(icon))
        if icon:
            self.availability_icon.set_from_icon_name(icon)
        self.availability.set_text(fmt.AVAILABILITY_NAMES[availability])
        self.availability_box.set_tooltip_text(fmt.AVAILABILITY_TOOLTIPS[availability])
        if availability == Availability.MISSING:
            self.availability.add_css_class("dim-label")
        else:
            self.availability.remove_css_class("dim-label")

    def _on_toggled(self, check: Gtk.CheckButton) -> None:
        if self.result is not None:
            self._page.set_checked(self.result, check.get_active())


class FindPage(Gtk.Box):
    """Search the catalogue and the library, and act on the results.

    ``host`` is the main window (see ``DetailPane`` for what it provides, plus
    ``backend``).
    """

    def __init__(self, host) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self._host = host
        self.checked: dict[str, SearchResult] = {}
        self._rows: set[ResultRow] = set()
        self._generation = 0
        self._detail_generation = 0
        self._info: CatalogueInfo | None = None
        self.results: list[SearchResult] = []
        self._all_results: list[SearchResult] = []  # before the kind filter
        self._last_text = ""
        self.searching = False

        self.split_view = Adw.OverlaySplitView(
            sidebar_position=Gtk.PackType.END,
            show_sidebar=False,
            # The detail pane is shown and hidden by selecting a result, never
            # by the window getting narrower or wider.
            pin_sidebar=True,
            min_sidebar_width=320,
            max_sidebar_width=500,
            sidebar_width_fraction=0.38,
            vexpand=True,
        )
        self.append(self.split_view)
        self.detail = DetailPane(host)
        self.split_view.set_sidebar(self.detail)

        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.split_view.set_content(content)

        top = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=10,
            margin_top=12,
            margin_bottom=10,
            margin_start=12,
            margin_end=12,
        )
        clamp = Adw.Clamp(maximum_size=760, tightening_threshold=560, child=top)
        self.search_area = clamp
        content.append(clamp)

        self.search_entry = Gtk.SearchEntry(
            placeholder_text="Search for a game, crew or disk number",
            hexpand=True,
            search_delay=SEARCH_DELAY_MS,
        )
        self.search_entry.update_property(
            [Gtk.AccessibleProperty.LABEL], ["Search the catalogue and your library"]
        )
        self.search_entry.connect("search-changed", lambda _entry: self.search())
        self.search_entry.connect("activate", self._on_entry_activate)
        self.search_entry.connect("stop-search", lambda _entry: self.search_entry.set_text(""))
        top.append(self.search_entry)

        filters = Gtk.FlowBox(
            selection_mode=Gtk.SelectionMode.NONE,
            column_spacing=8,
            row_spacing=8,
            max_children_per_line=4,
            homogeneous=False,
        )
        self.platform_dropdown = Gtk.DropDown.new_from_strings(
            [name for _platform, name in PLATFORM_CHOICES]
        )
        self.platform_dropdown.set_tooltip_text("Platform")
        self.platform_dropdown.update_property([Gtk.AccessibleProperty.LABEL], ["Platform"])
        self.platform_dropdown.connect("notify::selected", lambda *_args: self.search())
        self._add_filter(filters, self.platform_dropdown)

        kinds = Gtk.Box()
        kinds.add_css_class("linked")
        self.kind_buttons: dict[DiskKind, Gtk.ToggleButton] = {}
        self.kind_counts: dict[DiskKind, Gtk.Label] = {}
        for kind, label, tooltip in fmt.KIND_FILTERS:
            # Each button says how many results of its kind the search found,
            # so menu disks are visible even when single disks rank first.
            inner = Gtk.Box(spacing=6)
            inner.append(Gtk.Label(label=label))
            count = Gtk.Label(visible=False)
            count.add_css_class("dim-label")
            count.add_css_class("numeric")
            inner.append(count)
            button = Gtk.ToggleButton(child=inner, tooltip_text=tooltip)
            button.update_property([Gtk.AccessibleProperty.LABEL], [label])
            button.connect("toggled", lambda *_args: self._apply_kinds())
            kinds.append(button)
            self.kind_buttons[kind] = button
            self.kind_counts[kind] = count
        self._add_filter(filters, kinds)

        top.append(filters)

        status = Gtk.Box(spacing=8)
        self.count_label = Gtk.Label(xalign=0, hexpand=True)
        self.count_label.add_css_class("caption")
        self.count_label.add_css_class("dim-label")
        status.append(self.count_label)
        self.spinner = Gtk.Spinner(visible=False)
        status.append(self.spinner)
        self.available_button = Gtk.CheckButton.new_with_mnemonic("A_vailable Only")
        self.available_button.set_tooltip_text(
            "Show only disks in your library or that can be downloaded"
        )
        self.available_button.connect("toggled", lambda *_args: self.search())
        status.append(self.available_button)
        top.append(status)

        self.stack = Gtk.Stack(vexpand=True, transition_type=Gtk.StackTransitionType.CROSSFADE)
        self.stack.set_transition_duration(120)
        content.append(self.stack)
        self._build_welcome()
        self._build_results()
        self._build_no_results()
        self._build_no_catalogue()

        self.action_bar = Gtk.ActionBar(revealed=False)
        self.selected_label = Gtk.Label()
        self.action_bar.pack_start(self.selected_label)
        self.write_checked_button = text_button(
            "_Write Now", self._on_write_checked, style="suggested-action"
        )
        self.action_bar.pack_end(self.write_checked_button)
        self.add_checked_button = text_button("_Add to Queue", self._on_add_checked)
        self.action_bar.pack_end(self.add_checked_button)
        self.clear_checked_button = text_button(
            "C_lear", lambda _button: self.clear_checked(), tooltip="Untick all"
        )
        self.action_bar.pack_end(self.clear_checked_button)
        content.append(self.action_bar)

    @staticmethod
    def _add_filter(flow: Gtk.FlowBox, widget: Gtk.Widget) -> None:
        child = Gtk.FlowBoxChild(focusable=False, child=widget)
        flow.append(child)

    # Empty states

    def _build_welcome(self) -> None:
        self.welcome = Adw.StatusPage(
            icon_name="system-search-symbolic",
            title="Find a Disk",
            description="Search for a game, a crew or a disk number.",
        )
        # Example searches as links in one line of text, which wraps at any width.
        self.examples_label = Gtk.Label(
            wrap=True, justify=Gtk.Justification.CENTER, use_markup=True, visible=False
        )
        self.examples_label.connect("activate-link", self._on_example)
        self.examples: list[str] = []
        box = self.examples_label
        self.welcome.set_child(box)
        self.stack.add_named(self.welcome, "welcome")

    def _build_results(self) -> None:
        self.store = Gio.ListStore(item_type=ResultObject)
        self.selection = Gtk.SingleSelection(model=self.store, autoselect=False, can_unselect=True)
        self.selection.connect("notify::selected", self._on_selected)
        factory = Gtk.SignalListItemFactory()
        factory.connect("setup", lambda _factory, item: item.set_child(ResultRow(self)))
        factory.connect("bind", self._bind_row)
        factory.connect("unbind", self._unbind_row)
        self.list_view = Gtk.ListView(
            model=self.selection,
            factory=factory,
            show_separators=True,
            tab_behavior=Gtk.ListTabBehavior.ITEM,
        )
        self.list_view.update_property([Gtk.AccessibleProperty.LABEL], ["Search results"])
        self.list_view.connect("activate", self._on_activate)
        scroller = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER, vexpand=True)
        scroller.set_child(self.list_view)
        self.stack.add_named(scroller, "results")

    def _bind_row(self, _factory, item: Gtk.ListItem) -> None:
        row = item.get_child()
        row.bind(item.get_item().result)
        self._rows.add(row)

    def _unbind_row(self, _factory, item: Gtk.ListItem) -> None:
        self._rows.discard(item.get_child())

    def _build_no_results(self) -> None:
        self.no_results = Adw.StatusPage(icon_name="edit-find-symbolic", title="No Results")
        self.clear_filters_button = text_button(
            "_Clear Filters", lambda _button: self.clear_filters(), style="pill"
        )
        self.clear_filters_button.set_halign(Gtk.Align.CENTER)
        self.no_results.set_child(self.clear_filters_button)
        self.stack.add_named(self.no_results, "empty")

    def _build_no_catalogue(self) -> None:
        self.no_catalogue = Adw.StatusPage(
            icon_name="dialog-information-symbolic",
            title="No Catalogue Installed",
            description=(
                "PirateFinder needs its catalogue of menu disks to search. "
                "Download the latest catalogue to start."
            ),
        )
        button = text_button("_Update Catalogue", None, style="suggested-action pill")
        button.set_action_name("win.update-catalogue")
        button.set_halign(Gtk.Align.CENTER)
        self.no_catalogue.set_child(button)
        self.stack.add_named(self.no_catalogue, "no-catalogue")

    # Catalogue information

    def set_catalogue(self, info: CatalogueInfo) -> None:
        self._info = info
        # Without a catalogue there is nothing to search or filter.
        self.search_area.set_visible(info.available)
        if not info.available:
            self.close_detail()
            self.stack.set_visible_child_name("no-catalogue")
            self.count_label.set_text("")
            return
        stats = fmt.catalogue_stats_text(info.stats, info.built_at)
        self.welcome.set_description(
            "Search for a game, a crew or a disk number, then write the disks you pick "
            f"to floppy.\n{stats}"
        )
        self._find_examples(info)
        self.search()

    def _find_examples(self, info: CatalogueInfo) -> None:
        """Offer example searches that are known to find something."""
        largest = sorted(info.series, key=lambda item: -item.disk_count)[:3]
        candidates = list(dict.fromkeys([*EXAMPLE_SEARCHES, *(item.name for item in largest)]))
        backend = self._host.backend

        def work() -> list[str]:
            found: list[str] = []
            for text in candidates:
                try:
                    if backend.search(text, SearchFilters()):
                        found.append(text)
                except Exception as error:  # noqa: BLE001 - examples are optional
                    LOG.add("search", f"Example search {text!r} failed: {error}")
                if len(found) >= MAX_EXAMPLES:
                    break
            return found

        run_in_thread(work, self._show_examples, name="example-searches")

    def _show_examples(self, examples: list[str]) -> None:
        self.examples = list(examples)
        links = [
            f'<a href="example:{index}">{fmt.escape(text)}</a>'
            for index, text in enumerate(examples)
        ]
        if len(links) > 1:
            text = f"Try {', '.join(links[:-1])} or {links[-1]}."
        else:
            text = f"Try {links[0]}." if links else ""
        self.examples_label.set_markup(text)
        self.examples_label.set_visible(bool(links))

    def _on_example(self, _label, uri: str) -> bool:
        if uri.startswith("example:"):
            index = int(uri.removeprefix("example:"))
            if 0 <= index < len(self.examples):
                self.search_for(self.examples[index])
        return True

    # Searching

    def filters(self) -> SearchFilters:
        platform = PLATFORM_CHOICES[self.platform_dropdown.get_selected()][0]
        kinds = frozenset(kind for kind, button in self.kind_buttons.items() if button.get_active())
        return SearchFilters(platform, kinds, self.available_button.get_active())

    def filters_active(self) -> bool:
        current = self.filters()
        return current.platform is not None or bool(current.kinds) or current.available_only

    def clear_filters(self) -> None:
        self.platform_dropdown.set_selected(0)
        for button in self.kind_buttons.values():
            button.set_active(False)
        self.available_button.set_active(False)
        self.search()

    def search_for(self, text: str) -> None:
        self.search_entry.set_text(text)
        self.search_entry.set_position(-1)
        self.search()

    def focus_search(self) -> None:
        self.search_entry.grab_focus()
        self.search_entry.select_region(0, -1)

    def search(self) -> None:
        if self._info is not None and not self._info.available:
            return
        text = self.search_entry.get_text().strip()
        self._generation += 1
        generation = self._generation
        if not text:
            self.searching = False
            self.spinner.stop()
            self.spinner.set_visible(False)
            self._all_results = []
            self._show_kind_counts([])
            self._replace_results([])
            self.count_label.set_text("")
            self.stack.set_visible_child_name("welcome")
            return
        # Kinds are filtered here rather than in the search, so the buttons can
        # count every kind and switching kinds needs no new search.
        filters = replace(self.filters(), kinds=frozenset())
        backend = self._host.backend
        self.searching = True
        self.spinner.set_visible(True)
        self.spinner.start()
        run_in_thread(
            lambda: backend.search(text, filters),
            lambda results: self._show_results(generation, text, results),
            lambda error: self._search_failed(generation, error),
            name="search",
        )

    def _search_failed(self, generation: int, error: BaseException) -> None:
        if generation != self._generation:
            return
        self.searching = False
        self.spinner.stop()
        self.spinner.set_visible(False)
        self.no_results.set_title("Search Failed")
        self.no_results.set_description(
            f"{fmt.escape(str(error))}\nThe details are in the Diagnostic Log in the main menu."
        )
        self.clear_filters_button.set_visible(False)
        self.stack.set_visible_child_name("empty")

    def _show_results(self, generation: int, text: str, results: list[SearchResult]) -> None:
        if generation != self._generation:
            return  # a newer search has started since
        self.searching = False
        self.spinner.stop()
        self.spinner.set_visible(False)
        self._all_results = list(results)
        self._last_text = text
        self._show_kind_counts(results)
        self._show_filtered(text)

    def _apply_kinds(self) -> None:
        if self.searching:
            return  # the running search applies the kind filter when it lands
        if self._last_text and self._last_text == self.search_entry.get_text().strip():
            self._show_filtered(self._last_text)
        else:
            self.search()

    def _show_kind_counts(self, results: list[SearchResult]) -> None:
        counts = Counter(result.disk.kind for result in results if result.disk is not None)
        for kind, label in self.kind_counts.items():
            label.set_text(f"{counts[kind]:,}")
            label.set_visible(bool(results))

    def _show_filtered(self, text: str) -> None:
        kinds = self.filters().kinds
        results = [
            result
            for result in self._all_results
            if not kinds or (result.disk is not None and result.disk.kind in kinds)
        ]
        self._replace_results(results)
        if results:
            self.count_label.set_text(fmt.plural(len(results), "result"))
            self.stack.set_visible_child_name("results")
            return
        self.count_label.set_text("")
        self.no_results.set_title("No Results")
        advice = "Check the spelling, try fewer words, or search for a crew and disk number."
        if self.filters_active():
            advice = "Some filters are on. Clear them to search everything."
        quoted = fmt.escape(text)
        self.no_results.set_description(
            f"Nothing in the catalogue or your library matches “{quoted}”. {advice}"
        )
        self.clear_filters_button.set_visible(self.filters_active())
        self.stack.set_visible_child_name("empty")

    def _replace_results(self, results: list[SearchResult]) -> None:
        self.results = list(results)
        keep = self.detail_key()
        self.store.splice(0, self.store.get_n_items(), [ResultObject(r) for r in results])
        self.selection.set_selected(Gtk.INVALID_LIST_POSITION)
        if keep:
            for index, result in enumerate(results):
                if result.key == keep:
                    self.selection.set_selected(index)
                    break

    def refresh(self) -> None:
        """Search again, for example after a download changed availability."""
        self.search()

    # Selection and detail

    def detail_key(self) -> str:
        if not self.split_view.get_show_sidebar():
            return ""
        if self.detail.detail is not None:
            return f"disk:{self.detail.detail.disk.id}"
        if self.detail.local is not None:
            return f"file:{self.detail.local.path}::{self.detail.local.member}"
        return ""

    def _on_selected(self, selection: Gtk.SingleSelection, _property) -> None:
        item = selection.get_selected_item()
        if item is not None:
            self.show_result(item.result)

    def _on_activate(self, _view, position: int) -> None:
        item = self.store.get_item(position)
        if item is not None:
            self.selection.set_selected(position)
            self.show_result(item.result)

    def show_result(self, result: SearchResult) -> None:
        if result.key == self.detail_key():
            return
        self.split_view.set_show_sidebar(True)
        if result.local is not None and result.disk is None:
            self.detail.show_local(result.local)
            return
        self._detail_generation += 1
        generation = self._detail_generation
        disk_id = result.disk.id
        self.detail.show_loading()
        backend = self._host.backend

        def shown(detail) -> None:
            if generation == self._detail_generation:
                self.detail.show_disk(detail)

        def failed(error: BaseException) -> None:
            if generation == self._detail_generation:
                self._host.toast(f"Could not load {result.disk.label}: {error}")
                self.close_detail()

        run_in_thread(lambda: backend.detail(disk_id), shown, failed, name="detail")

    def reload_detail(self) -> None:
        """Load the open disk again, for example after it was downloaded."""
        if self.detail.detail is None or not self.split_view.get_show_sidebar():
            return
        disk_id = self.detail.detail.disk.id
        backend = self._host.backend
        self._detail_generation += 1
        generation = self._detail_generation

        def shown(detail) -> None:
            if generation == self._detail_generation:
                self.detail.show_disk(detail)

        run_in_thread(lambda: backend.detail(disk_id), shown, name="detail")

    def close_detail(self) -> None:
        self.split_view.set_show_sidebar(False)
        self.selection.set_selected(Gtk.INVALID_LIST_POSITION)
        self.detail.detail = None
        self.detail.local = None

    def _on_entry_activate(self, _entry) -> None:
        """Enter in the search box opens the first result."""
        if self.store.get_n_items():
            self.selection.set_selected(0)
            self.list_view.grab_focus()

    # Ticked rows

    def set_checked(self, result: SearchResult, checked: bool) -> None:
        if checked:
            self.checked[result.key] = result
        else:
            self.checked.pop(result.key, None)
        self._update_action_bar()

    def clear_checked(self) -> None:
        self.checked.clear()
        self._rebind()
        self._update_action_bar()

    def _rebind(self) -> None:
        for row in self._rows:
            if row.result is not None:
                row.bind(row.result)

    def _update_action_bar(self) -> None:
        count = len(self.checked)
        self.selected_label.set_text(f"{count:,} selected")
        self.action_bar.set_revealed(count > 0)
        self._host.selection_changed(count)

    def checked_items(self):
        return [make_item_for_result(result) for result in self.checked.values()]

    def selected_items(self):
        """Ticked rows, or the disk in the detail pane when nothing is ticked."""
        if self.checked:
            return self.checked_items()
        if self.split_view.get_show_sidebar():
            item = self.detail.queue_item()
            return [item] if item is not None else []
        return []

    def _on_add_checked(self, _button) -> None:
        items = self.checked_items()
        if items:
            self._host.add_to_queue(items)
            self.clear_checked()

    def _on_write_checked(self, _button) -> None:
        items = self.checked_items()
        if items:
            self._host.write_now(items)
            self.clear_checked()

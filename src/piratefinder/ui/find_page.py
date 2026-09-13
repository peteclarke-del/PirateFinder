"""The Find page: search, filters, a page of titles or discs, and the details pane.

Every change of text, filter, sort, mode or page asks the backend for one
page (``Backend.search_page``) on a worker thread. A generation counter drops
answers that arrive after a newer question. The table is never sorted here;
a click on a column header changes the query's sort order.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gtk  # noqa: E402

from ..archive_layout import UNKNOWN_CREW  # noqa: E402
from ..jobs.queue import item_from_local  # noqa: E402
from ..library.library import local_name  # noqa: E402
from ..models import (  # noqa: E402
    DiskKind,
    Facets,
    LocalFile,
    Platform,
    Query,
    ResultMode,
    ResultPage,
    ResultRow,
    Series,
    SortOrder,
)
from . import formatting as fmt  # noqa: E402
from .backend import CatalogueInfo  # noqa: E402
from .bridge import run_in_thread  # noqa: E402
from .detail_pane import DetailPane, queue_items_for_rows  # noqa: E402
from .find_widgets import ChoiceDropDown, Pager, ResultsTable, WrapBox  # noqa: E402
from .help_content import EXAMPLE_SEARCHES  # noqa: E402
from .log import LOG  # noqa: E402
from .widgets import icon_button, plain_row, set_accessible_label, text_button  # noqa: E402

PLATFORM_CHOICES = (
    (None, "Any Platform", None),
    (Platform.AMIGA, "Amiga", None),
    (Platform.ATARI_ST, "Atari ST", None),
)
SEARCH_DELAY_MS = 250
MAX_EXAMPLES = 4
UNMATCHED_LIMIT = 50
SIDEBAR_WIDTH = (340, 420)  # the details pane's usual narrowest and widest
SIDEBAR_LIMITS = (300, 720)  # how far it can be dragged
MIN_CONTENT_WIDTH = 420


def largest_crew(facets: Facets, series: Sequence[Series] = ()) -> str:
    """A crew to offer for browsing: the one with the most discs.

    Crews with a numbered menu series come first, since menu disks are what
    PirateFinder is for. The name given to discs with no known crew is never
    offered.
    """
    counts = {name: count for name, count in facets.crews if name and name != UNKNOWN_CREW}
    menus = {item.group for item in series if item.kind is DiskKind.MENU and item.group}
    for pool in ({name: n for name, n in counts.items() if name in menus}, counts):
        if pool:
            return max(pool.items(), key=lambda item: item[1])[0]
    return ""


class FindPage(Gtk.Box):
    """Search the catalogue and act on the results.

    ``host`` is the main window (see ``DetailPane`` for what it provides).
    """

    def __init__(self, host) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self._host = host
        self._info: CatalogueInfo | None = None
        self._generation = 0
        self._detail_generation = 0
        self.facets = Facets()
        self.page_index = 0
        self.mode = ResultMode.TITLES
        self.sort = SortOrder.RELEVANCE
        self.result_page: ResultPage | None = None
        self.results: list[ResultRow] = []
        self.unmatched: list[LocalFile] = []
        self.unmatched_dialog: Adw.Dialog | None = None
        # Ticked rows by key, with the query page they were ticked on.
        self.checked: dict[str, tuple[ResultRow, tuple]] = {}
        self.searching = False
        self.examples: list[tuple[str, str]] = []
        self._quiet = False
        # The text of the last search started, so the entry only searches when it changes.
        self._searched_text = ""

        self.split_view = Adw.OverlaySplitView(
            sidebar_position=Gtk.PackType.END,
            show_sidebar=False,
            # The pane is shown and hidden by selecting a result, never by the
            # window getting narrower or wider.
            pin_sidebar=True,
            min_sidebar_width=SIDEBAR_WIDTH[0],
            max_sidebar_width=SIDEBAR_WIDTH[1],
            sidebar_width_fraction=0.36,
            vexpand=True,
        )
        self.append(self.split_view)
        sidebar = Gtk.Box()
        sidebar.append(self._build_resize_handle())
        self.detail = DetailPane(host)
        self.detail.set_hexpand(True)
        self.detail.on_title_selected = self._pane_title_selected
        sidebar.append(self.detail)
        self.split_view.set_sidebar(sidebar)

        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.split_view.set_content(content)
        self.search_area = self._build_search_area()
        content.append(self.search_area)

        self.unmatched_banner = Adw.Banner(button_label="_Show Files", revealed=False)
        self.unmatched_banner.connect("button-clicked", lambda _banner: self.show_unmatched())
        content.append(self.unmatched_banner)

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

        shortcuts = Gtk.ShortcutController(
            propagation_phase=Gtk.PropagationPhase.CAPTURE, scope=Gtk.ShortcutScope.LOCAL
        )
        for accelerator, offset in (("<Control>Page_Down", 1), ("<Control>Page_Up", -1)):
            shortcuts.add_shortcut(
                Gtk.Shortcut.new(
                    Gtk.ShortcutTrigger.parse_string(accelerator),
                    Gtk.CallbackAction.new(lambda *_args, step=offset: self.step_page(step)),
                )
            )
        self.add_controller(shortcuts)

    # Building

    def _build_search_area(self) -> Gtk.Widget:
        top = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=8,
            margin_top=10,
            margin_bottom=8,
            margin_start=12,
            margin_end=12,
        )
        line = Gtk.Box(spacing=8)
        self.search_entry = Gtk.SearchEntry(
            placeholder_text="Search titles, discs, crews, years, file names",
            hexpand=True,
            search_delay=SEARCH_DELAY_MS,
        )
        set_accessible_label(self.search_entry, "Search the catalogue")
        self.search_entry.connect("search-changed", self._on_search_changed)
        self.search_entry.connect("activate", self._on_entry_activate)
        self.search_entry.connect("stop-search", lambda _entry: self.search_entry.set_text(""))
        line.append(self.search_entry)

        modes = Gtk.Box(valign=Gtk.Align.CENTER)
        modes.add_css_class("linked")
        self.titles_button = Gtk.ToggleButton.new_with_mnemonic("_Titles")
        self.titles_button.set_tooltip_text("One row for each game, demo or program")
        self.discs_button = Gtk.ToggleButton.new_with_mnemonic("_Discs")
        self.discs_button.set_tooltip_text("One row for each disc")
        self.discs_button.set_group(self.titles_button)
        self.titles_button.set_active(True)
        for button, mode in (
            (self.titles_button, ResultMode.TITLES),
            (self.discs_button, ResultMode.DISCS),
        ):
            button.connect("toggled", self._on_mode_toggled, mode)
            modes.append(button)
        line.append(modes)

        sort_label = Gtk.Label.new_with_mnemonic("_Sort")
        sort_label.add_css_class("dim-label")
        line.append(sort_label)
        self.sort_dropdown = ChoiceDropDown("Sort order", "How the results are ordered")
        self.sort_dropdown.set_choices([(order, name, None) for order, name in fmt.SORT_CHOICES])
        self.sort_dropdown.on_changed(self._on_sort_chosen)
        sort_label.set_mnemonic_widget(self.sort_dropdown)
        line.append(self.sort_dropdown)
        top.append(line)

        # Each control keeps its own width; they wrap onto a second line when
        # the window is narrow.
        filters = WrapBox(spacing=6, line_spacing=6)
        self.platform_dropdown = ChoiceDropDown("Platform", "Platform")
        self.platform_dropdown.set_choices(PLATFORM_CHOICES)
        self.type_dropdown = ChoiceDropDown("Type", "Games, applications, demos or music")
        self.kind_dropdown = ChoiceDropDown(
            "Disc kind", "Menu disks, packs, singles or compilations"
        )
        self.kind_dropdown.set_choices(
            [(None, "Any Disc Kind", None)]
            + [(kind, label, None) for kind, label, _tooltip in fmt.KIND_FILTERS]
        )
        self.crew_dropdown = ChoiceDropDown("Crew", "Crew; type to find one", search=True)
        self.year_dropdown = ChoiceDropDown("Year", "Year of release")
        self._set_facet_choices(Facets())
        for dropdown in (
            self.platform_dropdown,
            self.type_dropdown,
            self.kind_dropdown,
            self.crew_dropdown,
            self.year_dropdown,
        ):
            dropdown.on_changed(self._filters_changed)
            filters.append(dropdown)
        self.available_button = Gtk.CheckButton.new_with_mnemonic("A_vailable Only")
        self.available_button.set_tooltip_text(
            "Show only discs in your library or that can be downloaded"
        )
        self.available_button.set_valign(Gtk.Align.CENTER)
        self.available_button.connect("toggled", lambda *_args: self._filters_changed())
        filters.append(self.available_button)
        self.clear_filters_bar_button = icon_button(
            "edit-clear-all-symbolic", "Clear Filters", lambda _button: self.clear_filters()
        )
        self.clear_filters_bar_button.set_visible(False)
        filters.append(self.clear_filters_bar_button)
        self.filter_box = filters
        top.append(filters)
        return top

    def _build_resize_handle(self) -> Gtk.Widget:
        handle = Gtk.Box(width_request=5)
        handle.add_css_class("pane-resize")
        handle.set_cursor(Gdk.Cursor.new_from_name("col-resize", None))
        handle.set_tooltip_text("Drag to change the width of the details")
        drag = Gtk.GestureDrag()
        drag.connect("drag-begin", self._resize_begin)
        drag.connect("drag-update", self._resize_update)
        handle.add_controller(drag)
        self._resize_start = 0
        return handle

    def _resize_begin(self, _gesture, _x, _y) -> None:
        self._resize_start = self.detail.get_width() + 5

    def _resize_update(self, _gesture, offset_x: float, _y) -> None:
        widest = max(
            SIDEBAR_LIMITS[0], min(SIDEBAR_LIMITS[1], self.get_width() - MIN_CONTENT_WIDTH)
        )
        self.set_detail_width(
            int(max(SIDEBAR_LIMITS[0], min(widest, self._resize_start - offset_x)))
        )

    def set_detail_width(self, width: int) -> None:
        """Fix the width of the details pane, as dragging its edge does."""
        view = self.split_view
        if width >= view.get_max_sidebar_width():
            view.set_max_sidebar_width(width)
            view.set_min_sidebar_width(width)
        else:
            view.set_min_sidebar_width(width)
            view.set_max_sidebar_width(width)

    def _build_welcome(self) -> None:
        self.welcome = Adw.StatusPage(
            icon_name="system-search-symbolic",
            title="Find a Title or Disc",
            description="Search for anything, or choose a filter to browse.",
        )
        self.examples_label = Gtk.Label(
            wrap=True, justify=Gtk.Justification.CENTER, use_markup=True, visible=False
        )
        self.examples_label.connect("activate-link", self._on_example)
        self.welcome.set_child(self.examples_label)
        self.stack.add_named(self.welcome, "welcome")

    def _build_results(self) -> None:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.table = ResultsTable(
            is_checked=lambda key: key in self.checked,
            on_checked=self.set_checked,
            on_selected=self.show_row,
            on_sort=self._on_header_sort,
            on_activate=self.show_row,
        )
        box.append(self.table)
        box.append(Gtk.Separator())
        self.pager = Pager(self.go_to_page, self._on_page_size)
        box.append(self.pager)
        self.stack.add_named(box, "results")

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

    # Catalogue information and facets

    def set_catalogue(self, info: CatalogueInfo) -> None:
        self._info = info
        self.search_area.set_visible(info.available)
        if not info.available:
            self.close_detail()
            self.unmatched_banner.set_revealed(False)
            self.stack.set_visible_child_name("no-catalogue")
            return
        stats = fmt.catalogue_stats_text(info.stats, info.built_at)
        self.welcome.set_description(
            "Search for a title, disc, crew, year or file name, or choose a filter to browse. "
            f"Then write the discs you pick to floppy.\n{stats}"
        )
        self._load_facets()
        self.search()

    def _load_facets(self) -> None:
        backend = self._host.backend
        info = self._info or CatalogueInfo(False)

        def work() -> tuple[Facets, list[tuple[str, str]]]:
            facets = backend.facets()
            examples: list[tuple[str, str]] = []
            for text in EXAMPLE_SEARCHES:
                try:
                    if backend.search_page(Query(text=text, page_size=1)).total:
                        examples.append(("text", text))
                except Exception as error:  # noqa: BLE001 - examples are optional
                    LOG.add("search", f"Example search {text!r} failed: {error}")
                if len(examples) >= MAX_EXAMPLES - 1:
                    break
            crew = largest_crew(facets, info.series)
            if crew:
                examples.append(("crew", crew))
            return facets, examples

        run_in_thread(work, self._facets_loaded, name="facets")

    def _facets_loaded(self, result: tuple[Facets, list[tuple[str, str]]]) -> None:
        facets, examples = result
        self._set_facet_choices(facets)
        self._show_examples(examples)

    def reload_facets(self) -> None:
        """Load the filter choices again, after a disc's crew or year was corrected."""
        run_in_thread(self._host.backend.facets, self._set_facet_choices, name="facets")

    def _set_facet_choices(self, facets: Facets) -> None:
        self.facets = facets
        self.type_dropdown.set_choices(
            [("", "Any Type", None)] + [(name, name, count) for name, count in facets.categories]
        )
        self.crew_dropdown.set_choices(
            [("", "Any Crew", None)] + [(name, name, count) for name, count in facets.crews]
        )
        self.year_dropdown.set_choices(
            [(None, "Any Year", None)] + [(year, str(year), count) for year, count in facets.years]
        )

    def _show_examples(self, examples: list[tuple[str, str]]) -> None:
        self.examples = list(examples)
        links = []
        for index, (kind, text) in enumerate(examples):
            shown = f"every {text} disc by number" if kind == "crew" else text
            links.append(f'<a href="example:{index}">{fmt.escape(shown)}</a>')
        if len(links) > 1:
            markup = f"Try {', '.join(links[:-1])} or {links[-1]}."
        else:
            markup = f"Try {links[0]}." if links else ""
        self.examples_label.set_markup(markup)
        self.examples_label.set_visible(bool(links))

    def _on_example(self, _label, uri: str) -> bool:
        if uri.startswith("example:"):
            index = int(uri.removeprefix("example:"))
            if 0 <= index < len(self.examples):
                kind, text = self.examples[index]
                if kind == "crew":
                    self.browse_crew(text)
                else:
                    self.search_for(text)
        return True

    # The query

    def query(self) -> Query:
        kind = self.kind_dropdown.value
        return Query(
            text=self.search_entry.get_text().strip(),
            platform=self.platform_dropdown.value,
            category=self.type_dropdown.value or "",
            kinds=frozenset({kind}) if kind else frozenset(),
            crew=self.crew_dropdown.value or "",
            year=self.year_dropdown.value,
            available_only=self.available_button.get_active(),
            mode=self.mode,
            sort=self.sort,
            page=self.page_index,
            page_size=self.pager.page_size,
        )

    def filters_active(self) -> bool:
        query = self.query()
        return bool(
            query.platform
            or query.category
            or query.kinds
            or query.crew
            or query.year
            or query.available_only
        )

    def _filters_changed(self) -> None:
        self.clear_filters_bar_button.set_visible(self.filters_active())
        if not self._quiet:
            self.search()

    def clear_filters(self) -> None:
        self._quiet = True
        try:
            for dropdown in (
                self.platform_dropdown,
                self.type_dropdown,
                self.kind_dropdown,
                self.crew_dropdown,
                self.year_dropdown,
            ):
                dropdown.set_selected(0)
            self.available_button.set_active(False)
        finally:
            self._quiet = False
        self.clear_filters_bar_button.set_visible(False)
        self.search()

    def search_for(self, text: str) -> None:
        self.search_entry.set_text(text)
        self.search_entry.set_position(-1)
        self.search()

    def browse_crew(self, crew: str) -> None:
        """Every disc of one crew in disc order, with no search text."""
        self._quiet = True
        try:
            self.search_entry.set_text("")
            self.crew_dropdown.set_value(crew)
            self._set_sort(SortOrder.DISC)
        finally:
            self._quiet = False
        self._filters_changed()

    def focus_search(self) -> None:
        self.search_entry.grab_focus()
        self.search_entry.select_region(0, -1)

    def _on_mode_toggled(self, button: Gtk.ToggleButton, mode: ResultMode) -> None:
        if button.get_active() and mode != self.mode:
            self.mode = mode
            self.search()

    def set_mode(self, mode: ResultMode) -> None:
        (self.titles_button if mode == ResultMode.TITLES else self.discs_button).set_active(True)

    def _set_sort(self, sort: SortOrder) -> None:
        self.sort = sort
        self.sort_dropdown.set_value(sort, quiet=True)
        self.table.set_sort(sort)

    def _on_sort_chosen(self) -> None:
        sort = self.sort_dropdown.value or SortOrder.RELEVANCE
        if sort != self.sort:
            self.sort = sort
            self.table.set_sort(sort)
            if not self._quiet:
                self.search()

    def _on_header_sort(self, sort: SortOrder) -> None:
        if sort != self.sort:
            self.sort = sort
            self.sort_dropdown.set_value(sort, quiet=True)
            self.search()

    def set_sort(self, sort: SortOrder) -> None:
        """Choose a sort order as the Sort drop-down does."""
        self.sort_dropdown.set_value(sort)

    # Paging

    def go_to_page(self, page: int) -> None:
        self.search(reset_page=False, page=page)

    def step_page(self, offset: int) -> bool:
        if self.stack.get_visible_child_name() != "results":
            return False
        return self.pager.go(self.pager.page + offset)

    def _on_page_size(self, _size: int) -> None:
        self.search()

    # Searching

    def search(self, *, reset_page: bool = True, page: int | None = None) -> None:
        if self._info is not None and not self._info.available:
            return
        if page is not None:
            self.page_index = page
        elif reset_page:
            self.page_index = 0
        query = self.query()
        self._searched_text = query.text
        self._generation += 1
        generation = self._generation
        if not query.browsing:
            self.searching = False
            self.pager.set_busy(False)
            self.result_page = None
            self.results = []
            self.table.set_rows([], self.mode, [])
            self.unmatched = []
            self.unmatched_banner.set_revealed(False)
            self.close_detail()
            self.stack.set_visible_child_name("welcome")
            return
        backend = self._host.backend
        self.searching = True
        self.pager.set_busy(True)
        text = query.text

        def work() -> tuple[ResultPage, list[LocalFile]]:
            result = backend.search_page(query)
            unmatched: list[LocalFile] = []
            if text and query.page == 0:
                try:
                    unmatched = backend.unmatched_files(UNMATCHED_LIMIT, text)
                except Exception as error:  # noqa: BLE001 - the page matters more
                    LOG.add("search", f"Unmatched files could not be searched: {error}")
            return result, unmatched

        run_in_thread(
            work,
            lambda result: self._show_page(generation, *result),
            lambda error: self._search_failed(generation, error),
            name="search",
        )

    def _search_failed(self, generation: int, error: BaseException) -> None:
        if generation != self._generation:
            return
        self.searching = False
        self.pager.set_busy(False)
        self.no_results.set_title("Search Failed")
        self.no_results.set_description(
            f"{fmt.escape(str(error))}\nThe details are in the Diagnostic Log in the main menu."
        )
        self.clear_filters_button.set_visible(False)
        self.stack.set_visible_child_name("empty")

    def _show_page(self, generation: int, page: ResultPage, unmatched: list[LocalFile]) -> None:
        if generation != self._generation:
            return  # a newer search has started since
        if page.total and not page.rows and page.query.page > 0:
            # Fewer results than before, for example after a download: go to the last page.
            self.search(reset_page=False, page=page.pages - 1)
            return
        self.searching = False
        self.pager.set_busy(False)
        self.result_page = page
        self.results = list(page.rows)
        if page.query.page == 0:
            self.unmatched = list(unmatched)
        self._show_unmatched_banner()
        keys = self.detail.keys() if self.split_view.get_show_sidebar() else ()
        self.table.set_rows(page.rows, page.query.mode, fmt.query_words(page.query.text))
        self.table.set_sort(page.query.sort)
        if page.total:
            self.pager.update(page)
            self.stack.set_visible_child_name("results")
            if keys and not self.table.select_key(keys):
                self.close_detail()
            self._update_action_bar()
            return
        self.close_detail()
        self.no_results.set_title("No Results")
        if page.query.text:
            quoted = fmt.escape(page.query.text)
            text = f"Nothing in the catalogue matches “{quoted}”."
        else:
            text = "No disc in the catalogue has everything the filters ask for."
        advice = "Check the spelling, try fewer words, or search for a crew and disc number."
        if self.filters_active():
            advice = "Some filters are on. Clear them to search everything."
        self.no_results.set_description(f"{text} {advice}")
        self.clear_filters_button.set_visible(self.filters_active())
        self.stack.set_visible_child_name("empty")

    def refresh(self) -> None:
        """Search again on the same page, for example after a download."""
        self.search(reset_page=False)

    # Unmatched library files

    def _show_unmatched_banner(self) -> None:
        count = len(self.unmatched)
        self.unmatched_banner.set_title(
            f"{fmt.plural(count, 'file')} in your library "
            f"{'matches' if count == 1 else 'match'} no catalogue disc but "
            f"{'matches' if count == 1 else 'match'} this search"
        )
        self.unmatched_banner.set_revealed(bool(count))

    def show_unmatched(self) -> Adw.Dialog:
        dialog = Adw.Dialog(title="Unmatched Files", content_width=520, content_height=420)
        view = Adw.ToolbarView()
        view.add_top_bar(Adw.HeaderBar())
        page = Adw.PreferencesPage()
        group = Adw.PreferencesGroup(
            description=(
                "These images match no disc in the catalogue. They were found by file name, "
                "volume label or the names of the files on them."
            )
        )
        for local in self.unmatched:
            row = plain_row(title=local_name(local), subtitle=fmt.local_file_location(local))
            row.set_subtitle_lines(2)
            row.set_activatable(True)
            row.set_tooltip_text("Show the details of this file")
            row.connect("activated", lambda _row, file=local: self.show_file(file))
            button = text_button("Add to Queue", None, style="flat")
            button.set_tooltip_text(f"Add {local_name(local)} to the queue")
            button.connect("clicked", lambda _b, file=local: self._queue_file(file))
            row.add_suffix(button)
            group.add(row)
        page.add(group)
        view.set_content(page)
        dialog.set_child(view)
        dialog.present(self.get_root())
        self.unmatched_dialog = dialog
        return dialog

    def _queue_file(self, local: LocalFile) -> None:
        self._host.add_to_queue([item_from_local(local)])

    def show_file(self, local: LocalFile) -> None:
        """Show an unmatched library file in the details pane."""
        if self.unmatched_dialog is not None:
            self.unmatched_dialog.close()
        self._detail_generation += 1  # a disc still loading must not replace the file
        self.table.unselect()
        self.split_view.set_show_sidebar(True)
        self.detail.show_local(local)

    # Selection and details

    def show_row(self, row: ResultRow) -> None:
        showing = self.split_view.get_show_sidebar()
        if showing and self.detail.key() == row.key:
            return
        self.split_view.set_show_sidebar(True)
        if showing and self.detail.show_row(row):
            return
        self._load_detail(row.disk.id, row.content_id, loading=True)

    def _load_detail(self, disk_id: int, content_id: int | None, *, loading: bool) -> None:
        self._detail_generation += 1
        generation = self._detail_generation
        if loading:
            self.detail.show_loading()
        backend = self._host.backend

        def work():
            detail = backend.detail(disk_id)
            alternates = []
            if detail.virus is not None and detail.virus.infected:
                alternates = backend.clean_alternates(disk_id)
            return detail, alternates

        def shown(result) -> None:
            if generation == self._detail_generation:
                detail, alternates = result
                self.detail.show_disk(detail, content_id, alternates)

        def failed(error: BaseException) -> None:
            if generation == self._detail_generation:
                self._host.toast(f"The disc could not be shown: {error}")
                self.close_detail()

        run_in_thread(work, shown, failed, name="detail")

    def reload_detail(self) -> None:
        """Load the open disc again, for example after it was downloaded or cleaned."""
        if self.detail.detail is None or not self.split_view.get_show_sidebar():
            return
        self._load_detail(self.detail.detail.disk.id, self.detail.content_id, loading=False)

    def _pane_title_selected(self, content_id: int | None) -> None:
        """A title was picked in the pane: select its row when it is on this page."""
        if content_id is not None:
            self.table.select_key((f"title:{content_id}",))

    def close_detail(self) -> None:
        self.split_view.set_show_sidebar(False)
        self.table.unselect()
        self.detail.detail = None
        self.detail.local = None
        self.detail.content_id = None

    def _on_search_changed(self, entry: Gtk.SearchEntry) -> None:
        """Search when the words changed. The entry also reports a change it
        never had, a moment after it is built, and a search then would go
        back to the first page the user may already have left."""
        if entry.get_text().strip() != self._searched_text:
            self.search()

    def _on_entry_activate(self, _entry) -> None:
        """Enter in the search box opens the first result."""
        if self.results:
            self.table.select_index(0)
            self.table.view.grab_focus()

    # Ticked rows

    def _page_signature(self) -> tuple:
        query = self.result_page.query if self.result_page is not None else self.query()
        return (replace(query, page=0), query.page)

    def set_checked(self, row: ResultRow, checked: bool) -> None:
        if checked:
            self.checked[row.key] = (row, self._page_signature())
        else:
            self.checked.pop(row.key, None)
        self._update_action_bar()

    def clear_checked(self) -> None:
        self.checked.clear()
        self.table.refresh_checks()
        self._update_action_bar()

    def selection_counts(self) -> tuple[int, int, int]:
        """Ticked rows, the discs they are on and the pages they were ticked on."""
        rows = [row for row, _page in self.checked.values()]
        discs = len({row.disk.id for row in rows})
        pages = len({page for _row, page in self.checked.values()})
        return len(rows), discs, pages

    def _update_action_bar(self) -> None:
        rows, discs, pages = self.selection_counts()
        self.selected_label.set_text(fmt.selection_text(rows, discs, pages))
        self.action_bar.set_revealed(rows > 0)
        self._host.selection_changed(rows)

    def checked_items(self):
        return queue_items_for_rows(row for row, _page in self.checked.values())

    def selected_items(self):
        """Ticked rows, or the disc in the details pane when nothing is ticked."""
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

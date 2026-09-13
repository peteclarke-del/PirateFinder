"""Drive the real window with the fake backend.

Each test builds a MainWindow over a FakeBackend, works it the way a person
would (typing, ticking, pressing buttons, answering the insert prompt) and
checks what the window and the backend end up with. Needs a display; skipped
without one. Every wait is bounded.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

# Settings, queue and history belong to whoever runs the tests.
_HOME = Path(tempfile.mkdtemp(prefix="piratefinder-ui-test-"))
for _variable in ("XDG_CONFIG_HOME", "XDG_DATA_HOME"):
    os.environ[_variable] = str(_HOME / _variable.lower())

from tests.gtk_support import require_pygobject  # noqa: E402

require_pygobject()

import gi  # noqa: E402

from piratefinder.ui.application import PirateFinderApplication  # noqa: E402

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, GLib, Gtk  # noqa: E402

from piratefinder.greaseweazle.caps import CapsState, CapsStatus  # noqa: E402
from piratefinder.jobs.queue import MAX_COPIES, new_item  # noqa: E402
from piratefinder.models import (  # noqa: E402
    Availability,
    DeviceStatus,
    DiskKind,
    LocalFile,
    Platform,
    Query,
    ResultMode,
    SortOrder,
    WriteStatus,
)
from piratefinder.settings import Settings  # noqa: E402
from piratefinder.ui import formatting as fmt  # noqa: E402
from piratefinder.ui.backend import UpdateOffer, fetch_media, set_fetch_media  # noqa: E402
from piratefinder.ui.fake_backend import LIBRARY, FakeBackend  # noqa: E402
from piratefinder.ui.help_content import HELP_TOPICS  # noqa: E402

HAVE_DISPLAY = bool(Gtk.init_check()) and Gdk.Display.get_default() is not None
TIMEOUT = 10.0
_APPLICATION: list[PirateFinderApplication] = []


def application() -> PirateFinderApplication:
    """The one application the window tests share; GLib names an application once."""
    if not _APPLICATION:
        app = PirateFinderApplication(
            "com.github.pclarke.PirateFinderTests", unique=False, backend_factory=FakeBackend
        )
        app.register(None)
        _APPLICATION.append(app)
    return _APPLICATION[0]


def pump(seconds: float = 0.0) -> None:
    context = GLib.MainContext.default()
    deadline = time.monotonic() + seconds
    while True:
        while context.pending():
            context.iteration(False)
        if time.monotonic() >= deadline:
            return
        time.sleep(0.01)


def wait_until(predicate, message: str, timeout: float = TIMEOUT) -> None:
    context = GLib.MainContext.default()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        while context.pending():
            context.iteration(False)
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError(f"Timed out waiting: {message}")


def widgets_in(widget: Gtk.Widget) -> list[Gtk.Widget]:
    """Every widget below ``widget``, depth first."""
    found = []
    child = widget.get_first_child()
    while child is not None:
        found.append(child)
        found.extend(widgets_in(child))
        child = child.get_next_sibling()
    return found


def labels_in(widget: Gtk.Widget) -> list[str]:
    """The text (or markup) of every label below ``widget``."""
    return [child.get_label() for child in widgets_in(widget) if isinstance(child, Gtk.Label)]


def automation_titles() -> int:
    """How many titles the fake catalogue has on Automation discs."""
    return FakeBackend().search_page(Query(crew="Automation")).total


def _controllers(widget: Gtk.Widget) -> list:
    model = widget.observe_controllers()
    return [model.get_item(index) for index in range(model.get_n_items())]


def row_titles(list_box: Gtk.ListBox) -> list[str]:
    titles = []
    child = list_box.get_first_child()
    while child is not None:
        titles.append(child.get_title())
        child = child.get_next_sibling()
    return titles


@unittest.skipUnless(HAVE_DISPLAY, "needs a display")
class WindowTests(unittest.TestCase):
    app: PirateFinderApplication

    @classmethod
    def setUpClass(cls) -> None:
        cls.app = application()

    def setUp(self) -> None:
        from piratefinder.ui.window import MainWindow

        self.folder = Path(tempfile.mkdtemp(dir=_HOME))
        self.settings = Settings(path=self.folder / "settings.json")
        self.settings.download_folder = str(self.folder / "downloads")
        self.backend = FakeBackend(self.settings)
        self.window = MainWindow(application=self.app, backend=self.backend)
        self.window.present()
        wait_until(lambda: self.window.device is not None, "the first device probe")
        wait_until(
            lambda: self.backend.boot_rechecks == 1 and not self.window.library_page.scanning,
            "the boot block check at start",
        )

    def tearDown(self) -> None:
        window = self.window
        if window.queue_page.running:
            window.queue_page.cancel_session()
            wait_until(lambda: not window.queue_page.running, "the session to stop")
        while (dialog := window.get_visible_dialog()) is not None:
            dialog.force_close()
            pump()
        window.close()
        pump(0.05)

    # Helpers

    def search(self, text: str, count: int) -> None:
        """Type ``text`` and wait for ``count`` rows matching in all."""
        page = self.window.find_page
        page.search_entry.set_text(text)
        self.wait_total(count, f"{count} results for {text!r}")

    def wait_total(self, count: int, message: str = "") -> None:
        page = self.window.find_page
        wait_until(
            lambda: (
                not page.searching
                and page.result_page is not None
                and page.result_page.query == page.query()
                and page.result_page.total == count
            ),
            message or f"{count} results",
        )

    def wait_query(self, **wanted) -> None:
        """Wait until the page on screen answers a query with these fields."""
        page = self.window.find_page

        def done() -> bool:
            shown = page.result_page
            if page.searching or shown is None:
                return False
            return all(getattr(shown.query, name) == value for name, value in wanted.items())

        wait_until(done, f"a page for {wanted}")

    def browse_automation(self, page_size: int = 50) -> None:
        page = self.window.find_page
        page.pager.size_dropdown.set_selected(fmt.PAGE_SIZES.index(page_size))
        page.crew_dropdown.set_value("Automation")
        self.wait_query(crew="Automation", page_size=page_size, page=0)

    def closes_at_once(self, dialog: Adw.Dialog) -> mock.Mock:
        """Record ``dialog.close()`` and close it without its animation.

        A dialog closes at the end of its animation, and a display with no
        one watching (Broadway without a browser) never runs animations.
        """
        closing = mock.Mock(side_effect=dialog.force_close)
        dialog.close = closing
        return closing

    def open_row(self, index: int = 0) -> None:
        page = self.window.find_page
        page.table.select_index(index)
        row = page.results[index]
        wait_until(
            lambda: page.detail.detail is not None and page.detail.key() == row.key,
            "the details pane",
        )

    def check_for(self, key: str) -> Gtk.CheckButton:
        table = self.window.find_page.table
        wait_until(
            lambda: any(item.row.key == key for item in table._checks.values()),
            f"the tick box of {key}",
        )
        return next(check for check, item in table._checks.items() if item.row.key == key)

    def answer_prompt(self, response: str, heading: str = "", reason: str = "") -> None:
        page = self.window.queue_page

        def ready() -> bool:
            dialog = page.insert_dialog
            if dialog is None:
                return False
            return heading in dialog.get_heading() and reason in dialog.get_body()

        wait_until(ready, f"an insert prompt {heading!r} {reason!r}")
        self.assertTrue(page.answer_insert_prompt(response))

    def answer_alert(self, heading: str, response: str) -> None:
        wait_until(
            lambda: isinstance(self.window.get_visible_dialog(), Adw.AlertDialog),
            f"the alert {heading!r}",
        )
        dialog = self.window.get_visible_dialog()
        self.assertEqual(dialog.get_heading(), heading)
        dialog.emit("response", response)
        dialog.force_close()
        pump(0.05)

    # Find: searching and browsing

    def test_titles_are_listed_with_the_matching_words_in_bold(self) -> None:
        page = self.window.find_page
        self.assertEqual(page.stack.get_visible_child_name(), "welcome")
        self.search("rick", 3)
        self.assertEqual(page.stack.get_visible_child_name(), "results")
        self.assertEqual({row.title for row in page.results}, {"Rick Dangerous"})
        self.assertEqual(
            [row.disk.label for row in page.results],
            ["Pompey Pirates 1", "Pompey Pirates 51", "Skid Row Compact 12"],
        )
        self.assertTrue(page.table.columns["title"].get_visible())
        self.assertFalse(page.table.columns["contents"].get_visible())
        wait_until(lambda: "<b>Rick</b> Dangerous" in labels_in(page.table.view), "bold words")
        self.assertEqual(page.pager.range_label.get_text(), "3 titles")

    def test_the_filters_keep_their_width_and_wrap_when_narrow(self) -> None:
        page = self.window.find_page
        filters = page.filter_box

        def lines() -> int:
            """How many lines the visible filters take, by their vertical centres."""
            centres = []
            for child in filters.children:
                if child.get_visible():
                    ok, bounds = child.compute_bounds(filters)
                    self.assertTrue(ok)
                    self.assertGreater(bounds.get_width(), 0)
                    centres.append(bounds.get_y() + bounds.get_height() / 2)
            return len({round(centre / 8) for centre in centres})

        wait_until(lambda: filters.get_width() > 0, "the filter bar to be drawn")
        pump(0.2)
        self.assertEqual(lines(), 1, "one line in a wide window")
        width = filters.get_width()
        natural = filters.measure(Gtk.Orientation.HORIZONTAL, -1)[1]
        self.assertLessEqual(natural, width)
        self.assertGreater(
            filters.measure(Gtk.Orientation.VERTICAL, natural // 2)[0],
            filters.measure(Gtk.Orientation.VERTICAL, natural)[0],
        )

    def test_free_text_matches_crew_year_and_title_together(self) -> None:
        page = self.window.find_page
        self.search("automation necron 1990", 1)
        (row,) = page.results
        self.assertEqual((row.title, row.disk.label), ("Necron", "Automation 250"))

    def test_filters_narrow_the_results_and_go_back_to_the_first_page(self) -> None:
        page = self.window.find_page
        self.browse_automation(page_size=50)
        everything = page.result_page.total
        self.assertEqual(everything, automation_titles())
        self.assertTrue(page.clear_filters_bar_button.get_visible())
        page.pager.next_button.emit("clicked")
        self.wait_query(page=1)
        page.year_dropdown.set_value(1991)
        self.wait_query(year=1991, page=0)
        self.assertTrue(all(row.disk.year == 1991 for row in page.results))
        self.assertLess(page.result_page.total, everything)
        page.platform_dropdown.set_value(Platform.AMIGA)
        self.wait_query(platform=Platform.AMIGA)
        self.assertEqual(page.stack.get_visible_child_name(), "empty")
        self.assertTrue(page.clear_filters_button.get_visible())
        page.clear_filters_button.emit("clicked")
        self.assertFalse(page.filters_active())
        self.assertFalse(page.clear_filters_bar_button.get_visible())
        # No text and no filters: nothing to browse.
        wait_until(lambda: page.stack.get_visible_child_name() == "welcome", "the welcome page")

    def test_type_and_disc_kind_filters_use_the_catalogue_facets(self) -> None:
        page = self.window.find_page
        wait_until(lambda: len(page.crew_dropdown.choices) > 1, "the facets")
        facets = self.backend.facets()
        self.assertEqual(page.crew_dropdown.choices[1:], [name for name, _n in facets.crews])
        self.assertEqual(page.type_dropdown.choices[1:], [name for name, _n in facets.categories])
        page.type_dropdown.set_value("Demos")
        self.wait_query(category="Demos")
        self.assertTrue(all(row.disk.category == "Demos" for row in page.results))
        page.kind_dropdown.set_value(DiskKind.PACK)
        self.wait_query(kinds=frozenset({DiskKind.PACK}))
        self.assertTrue(all(row.disk.kind == DiskKind.PACK for row in page.results))
        self.assertTrue(page.results)

    def test_a_crew_is_browsed_by_disc_number_without_text(self) -> None:
        page = self.window.find_page
        page.set_mode(ResultMode.DISCS)
        page.crew_dropdown.set_value("Automation")
        page.set_sort(SortOrder.DISC)
        self.wait_query(crew="Automation", sort=SortOrder.DISC, mode=ResultMode.DISCS)
        self.assertEqual(page.search_entry.get_text(), "")
        numbers = [row.disk.number for row in page.results]
        self.assertEqual(numbers, sorted(numbers))
        self.assertEqual(page.results[0].disk.label, "Automation 250")
        self.assertTrue(page.table.columns["contents"].get_visible())
        self.assertFalse(page.table.columns["title"].get_visible())
        # The header shows the same order as the Sort drop-down.
        sorter = page.table.view.get_sorter()
        self.assertIs(sorter.get_primary_sort_column(), page.table.columns["disc"])

    def test_the_example_crew_is_never_the_unknown_crew(self) -> None:
        from piratefinder.archive_layout import UNKNOWN_CREW
        from piratefinder.models import Facets, Series
        from piratefinder.ui.find_page import largest_crew

        facets = Facets(crews=((UNKNOWN_CREW, 900), ("Automation", 800), ("D-Bug", 200)))
        self.assertEqual(largest_crew(facets), "Automation")
        self.assertEqual(largest_crew(Facets(crews=((UNKNOWN_CREW, 9),))), "")
        # A crew with a numbered menu series is offered before a bigger one without.
        dbug = Series("dbug", "D-Bug", Platform.ATARI_ST, DiskKind.MENU, "D-Bug")
        self.assertEqual(largest_crew(facets, (dbug,)), "D-Bug")

    def test_the_welcome_page_offers_searches_that_find_something(self) -> None:
        page = self.window.find_page
        wait_until(lambda: page.examples, "example searches")
        kinds = [kind for kind, _text in page.examples]
        self.assertIn("crew", kinds)
        index = kinds.index("crew")
        crew = page.examples[index][1]
        page.examples_label.emit("activate-link", f"example:{index}")
        self.wait_query(crew=crew, sort=SortOrder.DISC)
        self.assertTrue(page.results)

    def test_column_headers_sort_on_the_server_and_follow_the_drop_down(self) -> None:
        page = self.window.find_page
        self.browse_automation()
        table = page.table
        table.view.sort_by_column(table.columns["year"], Gtk.SortType.DESCENDING)
        self.wait_query(sort=SortOrder.YEAR_DESC, page=0)
        self.assertEqual(page.sort_dropdown.value, SortOrder.YEAR_DESC)
        self.assertEqual(self.backend.queries[-1].sort, SortOrder.YEAR_DESC)
        years = [row.disk.year for row in page.results]
        self.assertEqual(years, sorted(years, reverse=True))
        # The model is not sorted by GTK: it keeps the backend's order.
        self.assertEqual(table.rows, page.results)

        table.view.sort_by_column(table.columns["crew"], Gtk.SortType.ASCENDING)
        self.wait_query(sort=SortOrder.CREW)
        # Crew sorts one way only; a second click goes back to relevance.
        table.view.sort_by_column(table.columns["crew"], Gtk.SortType.DESCENDING)
        self.wait_query(sort=SortOrder.RELEVANCE)
        self.assertIsNone(table.view.get_sorter().get_primary_sort_column())

        page.set_sort(SortOrder.TITLE)
        self.wait_query(sort=SortOrder.TITLE)
        sorter = table.view.get_sorter()
        self.assertIs(sorter.get_primary_sort_column(), table.columns["title"])
        titles = [row.title.casefold() for row in page.results]
        self.assertEqual(titles, sorted(titles))

    def test_the_pager_moves_between_pages_and_changes_the_page_size(self) -> None:
        page = self.window.find_page
        pager = page.pager
        self.browse_automation(page_size=50)
        total = automation_titles()
        pages = -(-total // 50)
        self.assertGreaterEqual(pages, 3)
        self.assertEqual(pager.range_label.get_text(), f"1 to 50 of {total:,} titles")
        self.assertEqual(pager.of_label.get_text(), f"of {pages}")
        self.assertFalse(pager.previous_button.get_sensitive())
        pager.next_button.emit("clicked")
        self.wait_query(page=1)
        self.assertEqual(pager.range_label.get_text(), f"51 to 100 of {total:,} titles")
        pager.last_button.emit("clicked")
        self.wait_query(page=pages - 1)
        first = (pages - 1) * 50 + 1
        self.assertEqual(pager.range_label.get_text(), f"{first} to {total} of {total} titles")
        self.assertEqual(len(page.results), total - first + 1)
        self.assertFalse(pager.next_button.get_sensitive())
        pager.page_entry.set_text("2")
        pager.page_entry.emit("activate")
        self.wait_query(page=1)
        self.assertTrue(page.step_page(-1))  # Ctrl+Page Up
        self.wait_query(page=0)
        self.assertEqual(pager.page_entry.get_text(), "1")
        pager.first_button.emit("clicked")  # already there: nothing happens
        pager.size_dropdown.set_selected(fmt.PAGE_SIZES.index(100))
        self.wait_query(page_size=100, page=0)
        self.assertEqual(pager.of_label.get_text(), f"of {-(-total // 100)}")

    def test_ctrl_page_down_turns_the_page(self) -> None:
        page = self.window.find_page
        self.browse_automation(page_size=50)
        page.table.view.grab_focus()
        trigger = Gtk.ShortcutTrigger.parse_string("<Control>Page_Down")
        controller = next(c for c in _controllers(page) if isinstance(c, Gtk.ShortcutController))
        shortcut = next(
            controller.get_item(i)
            for i in range(controller.get_n_items())
            if controller.get_item(i).get_trigger().equal(trigger)
        )
        shortcut.get_action().activate(Gtk.ShortcutActionFlags.EXCLUSIVE, page, None)
        self.wait_query(page=1)

    def test_titles_and_discs_modes(self) -> None:
        page = self.window.find_page
        self.search("rick", 3)
        self.assertTrue(all(row.content_id is not None for row in page.results))
        page.discs_button.set_active(True)
        self.wait_query(mode=ResultMode.DISCS, page=0)
        self.assertEqual(page.result_page.total, 3)
        self.assertTrue(all(row.content_id is None for row in page.results))
        self.assertIn("Rick Dangerous", page.results[0].summary)
        self.assertEqual(page.pager.range_label.get_text(), "3 discs")
        page.titles_button.set_active(True)
        self.wait_query(mode=ResultMode.TITLES)

    def test_ticks_persist_across_pages_and_queue_each_disc_once(self) -> None:
        page = self.window.find_page
        self.browse_automation(page_size=50)
        first, second = page.results[0], page.results[1]
        self.assertEqual(first.disk.id, second.disk.id)  # two titles on Automation 250
        self.check_for(first.key).set_active(True)
        self.check_for(second.key).set_active(True)
        self.assertTrue(page.action_bar.get_revealed())
        self.assertEqual(page.selected_label.get_text(), "2 titles selected on 1 disc")
        page.pager.next_button.emit("clicked")
        self.wait_query(page=1)
        third = page.results[0]
        # Space ticks the selected row.
        page.table.select_index(0, notify=False)
        page.table._on_key(None, Gdk.KEY_space, 0, Gdk.ModifierType(0))
        self.assertIn(third.key, page.checked)
        self.assertEqual(
            page.selected_label.get_text(), "3 titles selected on 2 discs across 2 pages"
        )
        page.pager.previous_button.emit("clicked")
        self.wait_query(page=0)
        self.assertTrue(self.check_for(first.key).get_active())
        page.add_checked_button.emit("clicked")
        self.assertEqual(
            [item.label for item in self.backend.queue_items()],
            ["Automation 250", third.disk.label],
        )
        self.assertEqual(self.window.pages["queue"].get_badge_number(), 2)
        self.assertFalse(page.action_bar.get_revealed())
        self.assertFalse(self.check_for(first.key).get_active())

    def test_discs_can_be_ticked_and_queued_once(self) -> None:
        page = self.window.find_page
        page.set_mode(ResultMode.DISCS)
        discs = FakeBackend().search_page(Query(text="automation", mode=ResultMode.DISCS)).total
        self.search("automation", discs)
        self.check_for("disk:1").set_active(True)
        self.check_for("disk:2").set_active(True)
        self.assertEqual(page.selected_label.get_text(), "2 selected")
        page.add_checked_button.emit("clicked")
        self.assertEqual(
            [item.label for item in self.backend.queue_items()],
            ["Automation 250", "Automation 251"],
        )
        # The same disc is never queued twice.
        self.window.add_to_queue([new_item("Automation 250", None, disk_id=1)])
        self.assertEqual(len(self.backend.queue_items()), 2)

    def test_a_slow_search_never_replaces_a_newer_one(self) -> None:
        page = self.window.find_page
        self.backend.search_delay = 0.3
        page.search_entry.set_text("automation")
        page.search()
        page.search_entry.set_text("speedball")
        page.search()
        wait_until(lambda: not page.searching, "the searches to finish")
        pump(0.5)
        self.assertEqual([row.disk.label for row in page.results], ["Speedball 2"])

    def test_unmatched_library_files_are_offered_for_a_search(self) -> None:
        page = self.window.find_page
        page.search_entry.set_text("menu17")
        wait_until(lambda: page.unmatched_banner.get_revealed(), "the unmatched files bar")
        self.assertEqual(page.stack.get_visible_child_name(), "empty")
        dialog = page.show_unmatched()
        pump(0.05)
        buttons = [w for w in widgets_in(dialog) if isinstance(w, Gtk.Button)]
        add = next(button for button in buttons if button.get_label() == "Add to Queue")
        add.emit("clicked")
        (item,) = self.backend.queue_items()
        self.assertEqual(item.local.display_name, "menu17.st")
        # A row opens the file in the details pane.
        (row,) = [w for w in widgets_in(dialog) if isinstance(w, Adw.ActionRow)]
        row.emit("activated")
        pump(0.05)
        detail = page.detail
        self.assertTrue(page.split_view.get_show_sidebar())
        self.assertEqual((detail.local, detail.detail), (item.local, None))
        self.assertEqual(detail.title.get_text(), "menu17.st")
        self.assertTrue(detail.write_button.get_sensitive())
        self.assertEqual(detail.queue_item().local, item.local)
        wait_until(lambda: "Boot Block" in detail.fact_values, "the file's boot block")
        self.assertIn("TOS boot loader", detail.fact_values["Boot Block"])
        dialog.force_close()

    # Find: the details pane

    def test_a_boot_block_that_is_not_a_virus_is_a_plain_fact(self) -> None:
        detail = self.window.find_page.detail
        detail.show_disk(self.backend.detail(4))  # its library copy holds an immuniser
        self.assertIn("immuniser", detail.fact_values["Boot Block"])
        self.assertFalse(detail.virus_box.get_visible())
        (value,) = [
            label
            for label in widgets_in(detail.facts)
            if isinstance(label, Gtk.Label)
            and label.get_label() == detail.fact_values["Boot Block"]
        ]
        for style in ("error", "warning", "virus-card"):
            self.assertFalse(value.has_css_class(style))
        detail.show_disk(self.backend.detail(1))
        self.assertNotIn("Boot Block", detail.fact_values)
        detail.show_disk(self.backend.detail(9))  # a boot block virus has the card instead
        self.assertNotIn("Boot Block", detail.fact_values)
        self.assertTrue(detail.virus_box.get_visible())

    def test_scroll_text_and_laid_out_notes_are_in_a_fixed_width_font(self) -> None:
        detail = self.window.find_page.detail
        detail.show_disk(self.backend.detail(1))
        self.assertEqual(
            detail.subtitle.get_text(), "Automation • March 1990 • Atari ST • Menu disk"
        )
        self.assertTrue(detail.scroll_expander.get_visible())
        self.assertIn("AUTOMATION MENU 250", detail.scroll_label.get_label())
        self.assertTrue(detail.scroll_label.has_css_class("monospace"))
        (notes,) = [w for w in widgets_in(detail.notes_box) if isinstance(w, Gtk.Label)]
        self.assertFalse(notes.has_css_class("monospace"), "ordinary prose wraps as usual")

        detail.show_disk(self.backend.detail(13))
        self.assertFalse(detail.scroll_expander.get_visible())
        fonts = {
            label.get_label(): label.has_css_class("monospace")
            for label in widgets_in(detail.trivia_list)
            if isinstance(label, Gtk.Label) and label.get_label()
        }
        menu = next(text for text in fonts if "THE POMPEY PIRATES" in text)
        self.assertTrue(fonts[menu])
        fact = next(text for text in fonts if text.startswith("The first disc"))
        self.assertFalse(fonts[fact])

    def test_a_title_opens_the_pane_highlighted_with_its_picture_and_trivia(self) -> None:
        page = self.window.find_page
        detail = page.detail
        self.search("rick", 3)
        self.open_row(0)
        self.assertTrue(page.split_view.get_show_sidebar())
        self.assertEqual(detail.title.get_text(), "Rick Dangerous")
        self.assertEqual(detail.subtitle.get_text(), "on Pompey Pirates 1")
        self.assertEqual(detail.fact_values["Released"], "17 June 1989")
        self.assertEqual(detail.fact_values["Crew"], "Pompey Pirates")
        rick = detail.content_id
        row = detail._title_rows[rick]
        self.assertTrue(row.has_css_class("current-title"))
        self.assertTrue(row.marker.get_visible())
        self.assertEqual(
            sum(r.has_css_class("current-title") for r in detail._title_rows.values()), 1
        )
        media = detail.media
        self.assertEqual(media.current.content_id, rick)
        self.assertIn("Rick Dangerous", media.caption.get_text())
        wait_until(lambda: media.picture.get_paintable() is not None, "the picture")
        self.assertIn("PirateFinder test data", media.credit.get_label())
        wait_until(lambda: not detail.summaries_loading, "the Wikipedia summary")
        credits = " ".join(labels_in(detail.trivia_list))
        self.assertIn("From Wikipedia, Rick Dangerous, CC BY-SA 4.0", credits)
        self.assertIn("trainer menu", credits)  # the catalogue's note on this title
        self.assertIn("About the Crew", detail.crew_group.get_title())
        self.assertIn("Portsmouth", detail.crew_notes.get_text())

        # Another title on the disc: its picture and trivia replace Rick's.
        xenon = next(c.id for c in detail.detail.contents if c.title == "Xenon")
        detail._title_rows[xenon].emit("activated")
        self.assertEqual(detail.title.get_text(), "Xenon")
        self.assertTrue(detail._title_rows[xenon].has_css_class("current-title"))
        self.assertFalse(row.has_css_class("current-title"))
        self.assertEqual(media.current.content_id, xenon)
        wait_until(lambda: not detail.summaries_loading, "the summaries")
        self.assertNotIn("Wikipedia", " ".join(labels_in(detail.trivia_list)))
        # Stepping through the pictures reaches the disc's menu screen.
        media.next_button.emit("clicked")
        self.assertIsNone(media.current.content_id)
        self.assertIn("Menu screen of Pompey Pirates 1", media.caption.get_text())

        detail.close_button.emit("clicked")
        self.assertFalse(page.split_view.get_show_sidebar())

    def test_the_pane_stays_open_while_its_row_is_on_the_page(self) -> None:
        page = self.window.find_page
        self.search("rick", 3)
        self.open_row(1)
        key = page.results[1].key
        page.platform_dropdown.set_value(Platform.ATARI_ST)
        self.wait_query(platform=Platform.ATARI_ST)
        self.assertTrue(page.split_view.get_show_sidebar())
        self.assertEqual(page.table.selected_row().key, key)
        page.platform_dropdown.set_value(Platform.AMIGA)
        self.wait_query(platform=Platform.AMIGA)
        self.assertFalse(page.split_view.get_show_sidebar())

    def test_pictures_and_summaries_are_not_fetched_when_switched_off(self) -> None:
        set_fetch_media(self.settings, False)
        page = self.window.find_page
        self.search("rick", 3)
        self.open_row(0)
        media = page.detail.media
        self.assertIsNone(media.current)
        self.assertEqual(media.stack.get_visible_child_name(), "placeholder")
        self.assertEqual(media.placeholder_title.get_text(), "Pictures Are Off")
        self.assertTrue(media.preferences_button.get_visible())
        pump(0.2)
        self.assertEqual(self.backend.media_requests, [])
        self.assertEqual(self.backend.summary_requests, [])

    def test_a_disc_without_pictures_shows_a_placeholder(self) -> None:
        page = self.window.find_page
        self.search("stunt car", 1)
        self.open_row(0)
        media = page.detail.media
        self.assertEqual(media.placeholder_title.get_text(), "No Pictures")

    def test_detail_pane_writes_the_chosen_dump(self) -> None:
        page = self.window.find_page
        self.search("pompey 51", 2)
        self.open_row(0)
        detail = page.detail
        self.assertEqual(detail.subtitle.get_text(), "on Pompey Pirates 51")
        # Best Available, the main dump and the [a] alternate.
        self.assertEqual(len(detail.dump_rows.rows), 3)
        detail.dump_rows.rows[2].activate()
        detail.add_button.emit("clicked")
        (item,) = self.backend.queue_items()
        self.assertEqual((item.disk_id, item.image_id), (4, 41))
        detail.close_button.emit("clicked")
        self.assertFalse(page.split_view.get_show_sidebar())

    def test_edit_details_corrects_a_disc_and_revert_to_catalogue_undoes_it(self) -> None:
        page = self.window.find_page
        self.search("automation necron 1990", 1)
        self.open_row(0)
        detail = page.detail
        actions = detail._actions
        self.assertTrue(actions.lookup_action("edit-details").get_enabled())
        self.assertFalse(actions.lookup_action("revert-details").get_enabled())
        actions.activate_action("edit-details", None)
        dialog = detail.edit_dialog
        wait_until(lambda: self.window.get_visible_dialog() is dialog, "the Edit Details dialog")
        self.assertFalse(dialog.revert_group.get_visible(), "nothing to revert yet")
        self.assertEqual(dialog.entries["label"].get_text(), "Automation 250")
        dialog.entries["label"].set_text("Automation 250 (Side A)")
        dialog.entries["date"].set_text("1990-13")
        self.assertFalse(dialog.save_button.get_sensitive())
        self.assertEqual(dialog.problem.get_text(), "1990-13 is not a date in the calendar.")
        self.assertTrue(dialog.entries["date"].has_css_class("error"))
        dialog.entries["date"].set_text("1990-06-21")
        self.assertTrue(dialog.save_button.get_sensitive())
        self.assertFalse(dialog.problem_group.get_visible())
        dialog.entries["crew"].set_text("The Automation")
        necron = next(c.id for c in detail.detail.contents if c.title == "Necron")
        dialog.title_entries[necron].set_text("Necron (Fixed)")
        closing = self.closes_at_once(dialog)
        dialog.save_button.emit("clicked")
        wait_until(lambda: closing.called, "the dialog to close")
        wait_until(
            lambda: detail.detail is not None and detail.detail.edited != (),
            "the pane to show the corrections",
        )
        shown = detail.detail
        self.assertEqual(shown.disk.label, "Automation 250 (Side A)")
        self.assertEqual(shown.edited, ("label", "crew", "date"))
        self.assertEqual(detail.title.get_text(), "Necron (Fixed)")
        self.assertEqual(detail.fact_values["Released"], "21 June 1990")
        self.assertEqual(
            detail.edited_facts,
            {"Crew": "Automation", "Disc": "Automation 250", "Released": "March 1990"},
        )
        self.assertEqual(labels_in(detail.facts).count("Edited"), 3)
        self.assertIn("Edited", labels_in(detail.titles_group))
        wait_until(
            lambda: page.results and page.results[0].title == "Necron (Fixed)",
            "the results to show the corrected title",
        )
        self.assertEqual(page.results[0].disk.crew, "The Automation")
        # Revert to Catalogue, from the menu, asks first.
        self.assertTrue(actions.lookup_action("revert-details").get_enabled())
        actions.activate_action("revert-details", None)
        wait_until(
            lambda: isinstance(self.window.get_visible_dialog(), Adw.AlertDialog), "the question"
        )
        self.window.get_visible_dialog().emit("response", "revert")
        wait_until(lambda: not self.backend.corrections, "the corrections to go")
        wait_until(
            lambda: detail.detail is not None and detail.detail.disk.label == "Automation 250",
            "the catalogue's label",
        )
        self.assertNotIn("Edited", labels_in(detail.facts))
        self.assertFalse(actions.lookup_action("revert-details").get_enabled())

    def test_revert_to_catalogue_in_the_edit_details_dialog(self) -> None:
        self.backend.save_details(1, {"notes": "Side B is blank."})
        page = self.window.find_page
        self.search("automation necron 1990", 1)
        self.open_row(0)
        self.assertEqual(page.detail.notes_expander.get_subtitle(), "Edited")
        dialog = page.detail.edit_details()
        wait_until(lambda: self.window.get_visible_dialog() is dialog, "the Edit Details dialog")
        self.assertTrue(dialog.revert_group.get_visible())
        closing = self.closes_at_once(dialog)
        dialog.revert_button.emit("clicked")
        wait_until(
            lambda: isinstance(self.window.get_visible_dialog(), Adw.AlertDialog), "the question"
        )
        self.window.get_visible_dialog().emit("response", "revert")
        wait_until(lambda: not self.backend.corrections, "the corrections to go")
        wait_until(lambda: closing.called, "the Edit Details dialog to close")
        wait_until(lambda: page.detail.notes_expander.get_subtitle() == "", "the pane to refresh")

    def test_an_unmatched_file_has_no_details_to_edit(self) -> None:
        page = self.window.find_page
        page.show_file(self.backend.unmatched_files()[0])
        self.assertFalse(page.detail._actions.lookup_action("edit-details").get_enabled())
        self.assertIsNone(page.detail.confirm_revert())

    def test_download_only_makes_an_online_disk_local(self) -> None:
        page = self.window.find_page
        self.search("automation 251", 2)
        self.assertEqual(page.results[0].availability, Availability.ONLINE)
        self.open_row(0)
        actions = page.detail._actions
        self.assertTrue(actions.lookup_action("download").get_enabled())
        self.assertFalse(actions.lookup_action("show-in-files").get_enabled())
        actions.activate_action("download", None)
        wait_until(lambda: 2 in self.backend.local_disks, "the download")
        wait_until(
            lambda: page.results and page.results[0].availability == Availability.LOCAL,
            "the result to show the disc as local",
        )
        wait_until(lambda: not page.detail.downloading, "the download bar to go")
        self.assertIsNone(self.window.get_visible_dialog(), "no notes, so only a notification")

    def test_download_only_shows_its_notes(self) -> None:
        note = "No checksum is known for any dump of this disc, so the download was not checked."
        self.backend.download_notes = [note]
        page = self.window.find_page
        self.search("automation 251", 2)
        self.open_row(0)
        page.detail._actions.activate_action("download", None)
        wait_until(
            lambda: isinstance(self.window.get_visible_dialog(), Adw.AlertDialog), "the notes"
        )
        dialog = self.window.get_visible_dialog()
        self.assertEqual(dialog.get_heading(), "Downloaded Automation 251")
        self.assertTrue(dialog.get_body().endswith(note))
        self.assertIn("/Automation 251.st.", dialog.get_body())
        dialog.force_close()
        pump(0.05)

    def test_the_summary_and_history_show_the_notes_made_while_writing(self) -> None:
        page = self.window.queue_page
        self.window.write_now([new_item("Pompey Pirates 51", Platform.ATARI_ST, disk_id=4)])
        self.answer_prompt("write", "Pompey Pirates 51")
        wait_until(lambda: page.get_visible_child_name() == "summary", "the summary")
        row = page.summary_list.get_first_child()
        self.assertIn("Unpacked the MSA archive to a plain sector image.", row.get_subtitle())
        self.assertIn(
            "   Note: Unpacked the MSA archive to a plain sector image.",
            self.backend.report_text(page.summary),
        )
        self.window.show_page("history")
        history = self.window.history_page
        wait_until(lambda: len(history.sessions) == 1, "the history page")
        subtitles = [
            child.get_subtitle()
            for child in widgets_in(history)
            if isinstance(child, Adw.ActionRow) and child.get_title() == "Pompey Pirates 51"
        ]
        self.assertTrue(any("Unpacked the MSA archive" in text for text in subtitles), subtitles)

    # Find: viruses

    def test_a_boot_virus_is_removed_before_writing_when_asked(self) -> None:
        page = self.window.find_page
        self.search("skid row compact 13", 2)
        self.assertEqual(page.results[0].virus, "SCA")
        self.assertTrue(page.table.columns["virus"].get_visible())
        self.open_row(0)
        detail = page.detail
        self.assertTrue(detail.virus_box.get_visible())
        self.assertEqual(detail.virus_heading.get_text(), "Virus Found: SCA")
        self.assertIn("standard AmigaDOS boot block", detail.virus_body.get_text())
        self.assertEqual(
            detail.virus_source.get_text(), "Identified by PirateFinder's built-in signatures"
        )
        self.assertTrue(detail.remove_virus_row.get_visible())
        self.assertTrue(detail.remove_virus_row.get_active())
        self.assertTrue(detail.clean_row.get_visible())
        self.assertFalse(detail.alternate_row.get_visible())
        detail.remove_virus_row.set_active(False)
        detail.add_button.emit("clicked")
        (item,) = self.backend.queue_items()
        self.assertIs(item.clean_virus, False)
        self.assertIn("virus left in place", self.window.queue_page.item_subtitle(item))
        self.backend.queue_clear()
        detail.remove_virus_row.set_active(True)
        detail.add_button.emit("clicked")
        (item,) = self.backend.queue_items()
        self.assertIs(item.clean_virus, True)

    def test_clean_stored_image_asks_first_and_cleans_the_file(self) -> None:
        page = self.window.find_page
        self.search("skid row compact 13", 2)
        self.open_row(0)
        detail = page.detail
        detail.clean_button.emit("clicked")
        self.answer_alert("Clean the Stored Image?", "cancel")
        pump(0.1)
        self.assertEqual(self.backend.cleaned, [])
        detail.clean_button.emit("clicked")
        self.answer_alert("Clean the Stored Image?", "clean")
        wait_until(lambda: self.backend.cleaned, "the file to be cleaned")
        self.assertEqual(self.backend.cleaned[0].path, f"{LIBRARY}/Amiga/src13.zip")
        wait_until(lambda: not detail.virus_box.get_visible(), "the warning to go")
        wait_until(lambda: not page.results[0].virus, "the results to lose the flag")

    def test_clean_stored_image_cleans_the_file_the_writer_would_use(self) -> None:
        page = self.window.find_page
        self.search("skid row compact 13", 2)
        self.open_row(0)
        detail = page.detail
        shown = detail.detail
        written = shown.write_local
        # Another infected copy listed first is not the one the writer takes.
        other = replace(written, path=f"{LIBRARY}/Amiga/A Copy.adf", format="adf")
        detail.show_disk(replace(shown, local_files=(other, written)), detail.content_id)
        self.assertEqual(detail.clean_row.get_subtitle(), fmt.local_file_location(written))
        detail.clean_button.emit("clicked")
        self.answer_alert("Clean the Stored Image?", "clean")
        wait_until(lambda: self.backend.cleaned, "the file to be cleaned")
        self.assertEqual(self.backend.cleaned[0].path, written.path)

    def test_show_in_files_shows_the_file_the_writer_would_use(self) -> None:
        page = self.window.find_page
        self.search("skid row compact 13", 2)
        self.open_row(0)
        detail = page.detail
        shown = detail.detail
        written = shown.write_local
        other = replace(written, path=f"{LIBRARY}/Amiga/A Copy.adf", member="", format="adf")
        for write_local, expected in ((written, written), (None, other)):
            with self.subTest(write_local=write_local):
                listed = replace(shown, local_files=(other, written), write_local=write_local)
                detail.show_disk(listed, detail.content_id)
                with mock.patch("piratefinder.ui.detail_pane.show_in_files") as opened:
                    detail._actions.activate_action("show-in-files", None)
                opened.assert_called_once_with(detail, expected.path)

    def test_an_unmatched_file_is_queued_with_the_platform_its_format_says(self) -> None:
        detail = self.window.find_page.detail
        for name, platform in (("Game.ipf", None), ("Game.dms", Platform.AMIGA)):
            with self.subTest(name=name):
                detail.show_local(LocalFile(path=f"{LIBRARY}/{name}", format=name[-3:]))
                self.assertEqual(detail.queue_item().platform, platform)

    def test_a_file_virus_offers_the_clean_dump(self) -> None:
        page = self.window.find_page
        self.search("demo pack 41", 2)
        self.open_row(0)
        detail = page.detail
        self.assertEqual(detail.virus_heading.get_text(), "Dump Flagged with Saddam")
        self.assertIn("cannot remove it", detail.virus_body.get_text())
        self.assertEqual(detail.virus_source.get_text(), "Listed by TOSEC")
        self.assertFalse(detail.remove_virus_row.get_visible())
        self.assertFalse(detail.clean_row.get_visible())
        self.assertTrue(detail.alternate_row.get_visible())
        detail.use_clean_button.emit("clicked")
        self.assertTrue(detail._radios[101].get_active())
        self.assertTrue(detail.clean_chosen_row.get_visible())
        self.assertFalse(detail.alternate_row.get_visible())
        detail.add_button.emit("clicked")
        (item,) = self.backend.queue_items()
        self.assertEqual((item.disk_id, item.image_id), (10, 101))

    def test_no_catalogue_shows_only_the_update_page(self) -> None:
        from piratefinder.ui.window import MainWindow

        backend = FakeBackend(Settings(path=None), catalogue=False)
        window = MainWindow(application=self.app, backend=backend)
        window.present()
        pump(0.1)
        try:
            self.assertEqual(window.find_page.stack.get_visible_child_name(), "no-catalogue")
            self.assertFalse(window.find_page.search_area.get_visible())
        finally:
            window.close()
            pump(0.05)

    # Writing

    def queue_two(self) -> None:
        self.backend.queue_add(
            [
                new_item("Automation 250", Platform.ATARI_ST, disk_id=1),
                new_item("Automation 251", Platform.ATARI_ST, disk_id=2),
            ]
        )
        self.window.queue_page.refresh()

    def test_the_copies_button_allows_what_the_queue_allows(self) -> None:
        self.queue_two()
        row = self.window.queue_page.item_rows.rows[0]
        self.assertEqual(row.copies.get_adjustment().get_upper(), MAX_COPIES)
        row.copies.set_value(MAX_COPIES)
        self.assertEqual(self.backend.queue_items()[0].copies, MAX_COPIES)

    def test_a_session_with_prompts_ends_in_a_summary(self) -> None:
        self.backend.script.protect_once = {"Automation 250"}
        self.backend.script.fail_labels = {"Automation 251"}
        self.queue_two()
        page = self.window.queue_page
        self.assertEqual(page.get_visible_child_name(), "list")
        self.window.show_page("queue")
        page.start_button.emit("clicked")

        self.answer_prompt("write", "Insert a Disk for Automation 250", "Disk 1 of 2, drive A.")
        self.assertEqual(page.get_visible_child_name(), "running")
        self.assertEqual(page.running_page.get_title(), "Writing Disk 1 of 2")
        self.answer_prompt("write", "Automation 250", "write-protected")
        self.answer_prompt("write", "Insert a Disk for Automation 251", "Disk 2 of 2")
        wait_until(lambda: page.get_visible_child_name() == "summary", "the summary")

        self.assertEqual(row_titles(page.summary_list), ["Automation 250", "Automation 251"])
        self.assertEqual(page.summary_page.get_description(), "1 of 2 disks written and verified")
        self.assertEqual(page.summary_page.get_title(), "Some Disks Not Written")
        self.assertTrue(page.retry_button.get_visible())
        self.assertIn("of 160 track sides", page.track_label.get_text())
        # The written disk leaves the queue; the failed one stays for another try.
        self.assertEqual([item.label for item in self.backend.queue_items()], ["Automation 251"])
        self.assertEqual(self.window.pages["queue"].get_badge_number(), 1)
        self.assertEqual(len(self.backend.history()), 1)
        report = self.backend.report_text(page.summary)
        self.assertIn("2. Automation 251: failed", report)

        self.backend.script.fail_labels = set()
        page.retry_button.emit("clicked")
        self.answer_prompt("write", "Automation 251")
        wait_until(
            lambda: (
                page.get_visible_child_name() == "summary"
                and page.summary is not None
                and len(page.summary.items) == 1
            ),
            "the retry summary",
        )
        self.assertEqual(page.summary_page.get_title(), "All Disks Written")
        self.assertEqual(self.backend.queue_items(), [])
        page.done_button.emit("clicked")
        self.assertEqual(page.get_visible_child_name(), "empty")

    def test_stop_at_the_prompt_leaves_the_rest_in_the_queue(self) -> None:
        self.queue_two()
        page = self.window.queue_page
        page.start_button.emit("clicked")
        self.answer_prompt("stop", "Automation 250")
        wait_until(lambda: page.get_visible_child_name() == "summary", "the summary")
        statuses = [outcome.status for _label, outcome, _source in page.summary.items]
        self.assertEqual(statuses, [WriteStatus.SKIPPED, WriteStatus.SKIPPED])
        self.assertEqual(len(self.backend.queue_items()), 2)
        self.assertFalse(page.retry_button.get_visible())

    def test_cancel_while_writing(self) -> None:
        self.backend.script.track_delay = 0.01
        page = self.window.queue_page
        self.window.write_now([new_item("Automation 250", Platform.ATARI_ST, disk_id=1)])
        self.answer_prompt("write", "Automation 250")
        wait_until(lambda: page.progress.get_fraction() > 0.1, "writing to start")
        page.cancel_session()
        wait_until(lambda: page.get_visible_child_name() == "summary", "the summary")
        (_label, outcome, _source), *_ = page.summary.items
        self.assertEqual(outcome.status, WriteStatus.CANCELLED)
        self.assertEqual(self.backend.queue_items(), [], "Write Now does not queue disks")

    def test_no_device_shows_the_banner_and_asks_before_writing(self) -> None:
        self.backend.device = DeviceStatus(False, "No Greaseweazle was found.")
        self.window.probe_device()
        wait_until(lambda: self.window.banner.get_revealed(), "the banner")
        self.window.write_now([new_item("Automation 250", Platform.ATARI_ST, disk_id=1)])
        wait_until(lambda: self.window.get_visible_dialog() is not None, "the alert")
        dialog = self.window.get_visible_dialog()
        self.assertEqual(dialog.get_heading(), "No Greaseweazle Connected")
        self.assertFalse(self.window.queue_page.running)

        self.backend.device = DeviceStatus(True, "Greaseweazle F7", "F7")
        dialog.emit("response", "check")
        dialog.force_close()
        self.answer_prompt("stop", "Automation 250")
        wait_until(lambda: not self.window.queue_page.running, "the session to end")
        wait_until(lambda: not self.window.banner.get_revealed(), "the banner to go")

    def test_gw_info_runs_only_when_the_device_appears(self) -> None:
        window, backend = self.window, self.backend
        self.assertEqual(backend.probes, 1, "gw info identifies the device found at start")
        for _check in range(5):
            window.follow_device()
        pump()
        self.assertEqual(backend.probes, 1, "a device that stays needs no gw run")

        backend.present = False
        window.follow_device()
        self.assertTrue(window.banner.get_revealed())
        self.assertFalse(window.device.connected)
        self.assertEqual(backend.probes, 1, "a device that goes is noticed without gw")
        window.follow_device()
        self.assertEqual(backend.probes, 1)

        backend.present = True
        window.follow_device()
        wait_until(lambda: not window.banner.get_revealed(), "the banner to go")
        self.assertEqual(backend.probes, 2, "one gw run when the device comes back")
        window.follow_device()
        pump()
        self.assertEqual(backend.probes, 2)

    def test_an_arrival_gw_does_not_see_is_checked_once_more(self) -> None:
        window, backend = self.window, self.backend
        backend.present = False
        window.follow_device()
        backend.device = DeviceStatus(False, "No Greaseweazle is connected.")
        backend.present = True
        for expected in (2, 3, 3, 3):
            window.follow_device()
            wait_until(lambda wanted=expected: backend.probes == wanted, "gw info")
            pump(0.2)  # the answer reaches the window
            self.assertEqual(backend.probes, expected)
        self.assertTrue(window.banner.get_revealed())
        window.probe_device()  # Retry always runs gw info
        wait_until(lambda: backend.probes == 4, "the retry")

    def test_the_device_is_not_checked_while_writing(self) -> None:
        window, backend = self.window, self.backend
        backend.script.track_delay = 0.01
        window.write_now([new_item("Automation 250", Platform.ATARI_ST, disk_id=1)])
        self.answer_prompt("write", "Automation 250")
        wait_until(lambda: window.queue_page.progress.get_fraction() > 0.05, "writing")
        backend.present = False
        window.follow_device()
        window.probe_device()
        pump()
        self.assertEqual(backend.probes, 1)
        self.assertFalse(window.banner.get_revealed())
        window.queue_page.cancel_session()
        wait_until(lambda: not window.queue_page.running, "the session to stop")
        wait_until(lambda: window.banner.get_revealed(), "the device check after the session")
        self.assertEqual(backend.probes, 1, "the check after a session runs no gw")

    def test_writing_without_a_device_asks_before_running_gw(self) -> None:
        window, backend = self.window, self.backend
        backend.present = False
        window.follow_device()
        window.write_now([new_item("Automation 250", Platform.ATARI_ST, disk_id=1)])
        wait_until(lambda: window.get_visible_dialog() is not None, "the alert")
        self.assertEqual(backend.probes, 1, "the last answer is used")
        dialog = window.get_visible_dialog()
        dialog.emit("response", "check")
        dialog.force_close()
        wait_until(lambda: backend.probes == 2, "Check Again to run gw info")
        self.answer_prompt("stop", "Automation 250")
        wait_until(lambda: not window.queue_page.running, "the session to end")

    def test_changing_the_device_setting_checks_that_port(self) -> None:
        window, backend = self.window, self.backend
        self.settings.device = "/dev/ttyACM7"
        window.settings_changed("device")
        wait_until(lambda: backend.probes == 2, "gw info for the new port")
        backend.present = False
        self.settings.device = "/dev/ttyACM8"
        window.settings_changed("device")
        self.assertTrue(window.banner.get_revealed())
        self.assertEqual(backend.probes, 2)

    # Library, history and preferences

    def test_adding_a_library_folder_saves_it_and_scans(self) -> None:
        page = self.window.library_page
        folder = str(self.folder / "images")
        page.add_folder(folder)
        self.assertEqual(self.settings.library_folders, [folder])
        saved = json.loads(Path(self.settings.path).read_text(encoding="utf-8"))
        self.assertEqual(saved["library_folders"], [folder])
        wait_until(lambda: page.last_summary is not None, "the scan")
        self.assertIn("matched the catalogue", fmt.scan_toast(page.last_summary))
        self.assertEqual(len(page.folder_rows.rows), 1)
        page.remove_folder(folder)
        self.assertEqual(self.settings.library_folders, [])
        wait_until(lambda: not page.scanning, "the rescan")

    def test_history_lists_past_sessions(self) -> None:
        self.queue_two()
        self.window.queue_page.start_button.emit("clicked")
        self.answer_prompt("skip", "Automation 250")
        self.answer_prompt("skip", "Automation 251")
        wait_until(lambda: not self.window.queue_page.running, "the session to end")
        self.window.show_page("history")
        page = self.window.history_page
        wait_until(lambda: len(page.sessions) == 1, "the history page")
        self.assertEqual(page.get_visible_child_name(), "sessions")

    def test_preferences_save_every_setting(self) -> None:
        dialog = self.window.show_preferences()
        pump(0.1)
        self.assertEqual(set(dialog.provider_rows), {"internet-archive", "atari-legend", "d-bug"})

        dialog.provider_rows["atari-legend"].set_active(False)
        self.assertFalse(self.settings.providers["atari-legend"])
        saved = json.loads(Path(self.settings.path).read_text(encoding="utf-8"))
        self.assertIs(saved["providers"]["atari-legend"], False)

        dialog.retries_row.set_value(5)
        dialog.erase_row.set_active(True)
        dialog.prompt_row.set_active(False)
        dialog.check_updates_row.set_active(False)
        dialog.drive_row.set_selected(1)
        dialog.device_entry.set_text("/dev/ttyACM1")
        dialog.online_row.set_active(False)
        self.assertFalse(dialog.provider_rows["d-bug"].get_sensitive())
        self.assertFalse(dialog.media_row.get_sensitive())
        self.assertFalse(self.backend.media_enabled)
        wait_until(lambda: self.settings.device == "/dev/ttyACM1", "the device path")
        saved = json.loads(Path(self.settings.path).read_text(encoding="utf-8"))
        self.assertEqual(
            (saved["retries"], saved["pre_erase"], saved["prompt_between_disks"]),
            (5, True, False),
        )
        self.assertEqual((saved["drive"], saved["device"]), ("B", "/dev/ttyACM1"))
        self.assertIs(saved["check_catalogue_updates"], False)
        self.assertIs(saved["online_enabled"], False)
        # The Queue page shows the same drive.
        self.assertEqual(self.window.queue_page.drive_row.get_selected(), 1)
        dialog.force_close()
        pump(0.05)

        # Online downloads off: a disc only available online is now missing.
        self.search("automation 251", 2)
        self.assertEqual(self.window.find_page.results[0].availability, Availability.MISSING)

    def test_the_pictures_switch_says_what_it_controls(self) -> None:
        dialog = self.window.show_preferences()
        subtitle = dialog.media_row.get_subtitle()
        self.assertIn("Pictures and Wikipedia summaries", subtitle)
        self.assertNotIn("crew history", subtitle.split(".")[0])
        dialog.force_close()
        pump(0.05)

    def test_about_credits_the_bootblock_reader(self) -> None:
        sections = []
        original = Adw.AboutDialog.add_acknowledgement_section

        def record(dialog, name, people):
            sections.append((name, list(people)))
            return original(dialog, name, people)

        with mock.patch.object(Adw.AboutDialog, "add_acknowledgement_section", record):
            self.window.show_about()
        self.assertIn(
            (
                "Amiga Bootblock Reader",
                ["Jason and Jordan Smith https://github.com/jasonthesmith79/AmigaBootBlockReader"],
            ),
            sections,
        )
        credits = " ".join(dict(sections)["Virus Data"])
        for name in ("Matthias Gutt", "Steve Tibbett and Dan James", "Richard Karsmakers"):
            self.assertIn(name, credits)
        dialog = self.window.get_visible_dialog()
        dialog.force_close()
        pump(0.05)

    def test_preferences_switch_pictures_off_and_download_the_brainfile(self) -> None:
        dialog = self.window.show_preferences()
        pump(0.1)
        self.assertTrue(dialog.media_row.get_active())
        dialog.media_row.set_active(False)
        self.assertFalse(fetch_media(self.settings))
        saved = json.loads(Path(self.settings.path).read_text(encoding="utf-8"))
        self.assertIs(saved["fetch_media"], False)
        self.assertFalse(self.backend.media_enabled)

        wait_until(lambda: dialog.brainfile_status is not None, "the brainfile status")
        self.assertTrue(dialog.brainfile_row.get_subtitle().startswith("Not installed"))
        self.assertIn("Jason and Jordan Smith", dialog.virus_detection_group.get_description())
        dialog.brainfile_button.emit("clicked")
        wait_until(lambda: not dialog.downloading_brainfile, "the brainfile download")
        self.assertTrue(self.backend.brainfile.installed)
        self.assertEqual(dialog.brainfile_row.get_subtitle(), "Version 1.0, 120 known boot blocks")
        self.assertEqual(dialog.brainfile_button.get_label(), "_Update Brainfile")
        # The new brainfile is new virus data: the library's boot blocks are checked again.
        wait_until(
            lambda: self.backend.boot_rechecks == 2 and self.backend.boot_recheck is None,
            "the boot blocks to be checked again",
        )
        wait_until(lambda: not self.window.library_page.scanning, "the check to end")
        dialog.force_close()

    def test_boot_blocks_checked_again_are_reported_and_a_scan_waits_for_them(self) -> None:
        from piratefinder.models import BootRecheck
        from piratefinder.ui.log import LOG

        page = self.window.library_page
        self.backend.boot_recheck = BootRecheck(checked=30, changed=2)
        self.backend.script.track_delay = 0.02
        self.window.recheck_boot_blocks()
        wait_until(lambda: page.progress_row.get_visible(), "the progress of the check")
        self.assertEqual(page.progress_row.get_title(), "Checking Boot Blocks")
        self.assertTrue(page.scanning)
        self.settings.library_folders = [str(self.folder)]
        page.scan()  # asked for during the check: it runs afterwards
        wait_until(lambda: page.last_summary is not None, "the scan after the check")
        self.assertEqual(self.backend.boot_rechecks, 2)
        self.assertIn(fmt.boot_recheck_text(BootRecheck(checked=30, changed=2)), LOG.text())

    def test_preferences_install_and_remove_ipf_support(self) -> None:
        dialog = self.window.show_preferences()
        wait_until(lambda: dialog.caps_status is not None, "the IPF support status")
        self.assertIn("non-commercial", dialog.ipf_group.get_description())
        self.assertEqual(
            dialog.caps_row.get_subtitle(),
            "Not installed. IPF images cannot be written until it is.",
        )
        self.assertTrue(dialog.caps_install_button.get_visible())
        self.assertFalse(dialog.caps_remove_button.get_visible())

        # The licence is shown first, and declining it downloads nothing.
        dialog.caps_install_button.emit("clicked")
        wait_until(
            lambda: isinstance(self.window.get_visible_dialog(), Adw.AlertDialog), "the licence"
        )
        licence = " ".join(labels_in(self.window.get_visible_dialog()))
        self.assertIn("Redistributions may not be sold", licence)
        self.assertIn("from fs-uae.net", licence)
        self.answer_alert("Accept the Licence?", "cancel")
        self.assertFalse(dialog.installing_caps)
        self.assertIs(self.backend.caps.state, CapsState.MISSING)

        dialog.caps_install_button.emit("clicked")
        self.answer_alert("Accept the Licence?", "accept")
        wait_until(lambda: not dialog.installing_caps, "the install")
        self.assertIs(self.backend.caps.state, CapsState.INSTALLED)
        self.assertEqual(
            dialog.caps_row.get_subtitle(),
            "Version 5.1.3, installed by PirateFinder. IPF images can be written.",
        )
        self.assertFalse(dialog.caps_install_button.get_visible())
        self.assertTrue(dialog.caps_remove_button.get_visible())

        dialog.caps_remove_button.emit("clicked")
        wait_until(lambda: dialog.caps_install_button.get_visible(), "the removal")
        self.assertIs(self.backend.caps.state, CapsState.MISSING)
        self.assertFalse(dialog.caps_remove_button.get_visible())

        # A failed install says why in a sentence and offers Install again.
        refusal = (
            "The file downloaded from fs-uae.net is not the one PirateFinder expects: its size "
            "or checksum is different, so nothing was installed."
        )
        self.backend.caps_error = refusal
        dialog.caps_install_button.emit("clicked")
        self.answer_alert("Accept the Licence?", "accept")
        wait_until(lambda: not dialog.installing_caps, "the failed install")
        self.assertEqual(dialog.caps_row.get_subtitle(), refusal)
        self.assertTrue(dialog.caps_install_button.get_visible())
        dialog.force_close()
        pump(0.05)

    def assert_ipf_support_says(self, status: CapsStatus, text: str) -> None:
        """Preferences show ``status`` with ``text`` and offer neither Install nor Remove."""
        self.backend.caps = status
        dialog = self.window.show_preferences()
        wait_until(lambda: dialog.caps_status is not None, "the IPF support status")
        self.assertEqual(dialog.caps_row.get_subtitle(), text)
        self.assertFalse(dialog.caps_install_button.get_visible())
        self.assertFalse(dialog.caps_remove_button.get_visible())
        dialog.force_close()

    def test_ipf_support_found_on_the_system_is_not_offered(self) -> None:
        self.assert_ipf_support_says(
            CapsStatus(CapsState.SYSTEM, path="libcapsimage.so.5", machine="x86_64"),
            "Found on this computer (libcapsimage.so.5). IPF images can be written.",
        )

    def test_ipf_support_without_a_build_says_so(self) -> None:
        self.assert_ipf_support_says(
            CapsStatus(CapsState.UNSUPPORTED, machine="aarch64"),
            "Not installed, and PirateFinder has no build of it for this computer's processor "
            "(aarch64); there are builds for 64-bit PCs and 32-bit ARM only. A copy installed "
            "by other means is used when it is found.",
        )

    def test_the_library_lists_files_with_viruses_and_cleans_them(self) -> None:
        self.window.show_page("library")
        page = self.window.library_page
        page.refresh()
        wait_until(lambda: len(page.infected) == 2, "the infected files")
        self.assertTrue(page.virus_group.get_visible())
        subtitles = [row.get_subtitle() for row in page.virus_rows.rows]
        self.assertTrue(any(text.startswith("Virus: SCA") for text in subtitles), subtitles)
        self.assertTrue(any(text.startswith("Virus: Byte Bandit") for text in subtitles))
        # The unmatched file with a virus carries the warning sign too.
        unmatched = {row.get_title(): row for row in page.unmatched_rows.rows}
        icons = [w for w in widgets_in(unmatched["DISK2.ADF"]) if isinstance(w, Gtk.Image)]
        self.assertTrue(any(icon.get_icon_name() == "dialog-warning-symbolic" for icon in icons))
        page.clean_buttons[(f"{LIBRARY}/Amiga/src13.zip", "")].emit("clicked")
        self.answer_alert("Clean the Stored Image?", "clean")
        wait_until(lambda: len(page.infected) == 1, "the list to lose the cleaned file")
        self.assertEqual(self.backend.cleaned[0].path, f"{LIBRARY}/Amiga/src13.zip")

    def test_update_catalogue_installs_and_refreshes_the_welcome_page(self) -> None:
        self.backend.update_offer = UpdateOffer("2026-09-10", 5_000_000)
        self.window.update_catalogue()
        updater = self.window.updater
        wait_until(lambda: updater.state.phase == "available", "the update check")
        updater.install(updater.state.offer)
        wait_until(lambda: updater.state.phase == "done", "the update")
        self.assertIn("10 Sep 2026", self.window.find_page.welcome.get_description())

    def test_a_failed_update_check_says_why_and_not_up_to_date(self) -> None:
        self.backend.update_error = "api.github.com could not be reached: timed out."
        self.window.update_catalogue()
        updater = self.window.updater
        wait_until(lambda: updater.state.phase == "failed", "the update check to fail")
        expected = (
            "Could not check for a newer catalogue: api.github.com could not be reached: timed out."
        )
        self.assertEqual(updater.state.message, expected)
        preferences = self.window.show_preferences()
        self.assertEqual(preferences.update_row.get_subtitle(), expected)
        self.backend.update_error = ""
        updater.check()
        wait_until(lambda: updater.state.phase == "current", "a check that works")
        self.assertTrue(updater.state.message.startswith("The catalogue is up to date"))
        preferences.force_close()
        pump(0.05)

    def test_menu_items_open_their_windows(self) -> None:
        window = self.window
        for action, accelerator in (
            ("win.focus-search", "<Control>f"),
            ("win.help", "F1"),
            ("win.preferences", "<Control>comma"),
            ("win.write-selected", "<Control>Return"),
            ("app.quit", "<Control>q"),
        ):
            self.assertIn(accelerator, self.app.get_accels_for_action(action))
        window.activate_action("win.help", None)
        help_window = window._help_window
        self.assertTrue(help_window.get_visible())
        rows = 0
        while help_window.help_view.topic_list.get_row_at_index(rows) is not None:
            rows += 1
        self.assertEqual(rows, len(HELP_TOPICS))
        help_window.help_view.show_topic("troubleshooting")
        help_window.close()

        window.activate_action("win.about", None)
        pump(0.05)
        self.assertIsInstance(window.get_visible_dialog(), Adw.AboutDialog)
        window.get_visible_dialog().force_close()
        window.activate_action("win.diagnostic-log", None)
        pump(0.05)
        self.assertEqual(window.get_visible_dialog().get_title(), "Diagnostic Log")
        window.get_visible_dialog().force_close()
        window.activate_action("win.show-page", GLib.Variant("s", "library"))
        self.assertEqual(window.stack.get_visible_child_name(), "library")
        window.activate_action("win.focus-search", None)
        self.assertEqual(window.stack.get_visible_child_name(), "find")


@unittest.skipUnless(HAVE_DISPLAY, "needs a display")
class CorrectionsInTheRealWindowTests(unittest.TestCase):
    """Edit Details in the window over the real backend, catalogue and user database:
    the search, the filters and their choices follow the corrections."""

    @classmethod
    def setUpClass(cls) -> None:
        from tests.test_catalogue_search import write_find_sample

        cls.app = application()
        cls.catalogue_folder = Path(tempfile.mkdtemp(dir=_HOME))
        cls.catalogue_path = cls.catalogue_folder / "catalogue.sqlite"
        write_find_sample(cls.catalogue_path)

    def setUp(self) -> None:
        from piratefinder.catalogue.store import Catalogue
        from piratefinder.library.userdb import UserDatabase
        from piratefinder.ui.real_backend import RealBackend
        from piratefinder.ui.window import MainWindow

        folder = Path(tempfile.mkdtemp(dir=_HOME))
        settings = Settings(path=folder / "settings.json")
        settings.download_folder = str(folder / "downloads")
        settings.online_enabled = False  # no pictures, summaries or update check
        settings.check_catalogue_updates = False
        self.backend = RealBackend(
            settings, UserDatabase.open(folder / "user.sqlite"), Catalogue.open(self.catalogue_path)
        )
        self.backend.device_present = lambda: False  # no gw info from a test
        self.window = MainWindow(application=self.app, backend=self.backend)
        self.window.present()

    def tearDown(self) -> None:
        while (dialog := self.window.get_visible_dialog()) is not None:
            dialog.force_close()
            pump()
        self.window.close()
        pump(0.05)

    def shown(self, **wanted) -> list[tuple[str, str]]:
        """Wait for the page answering a query with these fields; its (disc, title) rows."""
        page = self.window.find_page

        def done() -> bool:
            result = page.result_page
            return (
                not page.searching
                and result is not None
                and result.query == page.query()
                and all(getattr(result.query, name) == value for name, value in wanted.items())
            )

        wait_until(done, f"a page for {wanted}")
        return [(row.disk.label, row.title) for row in page.result_page.rows]

    def edit(self, text: str, change) -> None:
        """Search for ``text``, open the first row's Edit Details, change it and save."""
        page = self.window.find_page
        page.search_entry.set_text(text)
        self.shown(text=text)
        page.table.select_index(0)
        wait_until(lambda: page.detail.detail is not None, "the details pane")
        dialog = page.detail.edit_details()
        wait_until(lambda: self.window.get_visible_dialog() is dialog, "the Edit Details dialog")
        change(dialog)
        closing = mock.Mock(side_effect=dialog.force_close)
        dialog.close = closing
        dialog.save_button.emit("clicked")
        wait_until(lambda: closing.called, "the corrections to be saved")

    def test_a_corrected_crew_is_a_filter_choice_that_finds_the_disc(self) -> None:
        page = self.window.find_page
        wait_until(lambda: "Image Works" in page.crew_dropdown.choices, "the crew choices")
        self.edit("speedball", lambda dialog: dialog.entries["crew"].set_text("Zorglub Crew"))
        wait_until(
            lambda: "Zorglub Crew" in page.crew_dropdown.choices, "the corrected crew to be listed"
        )
        self.assertNotIn("Image Works", page.crew_dropdown.choices)
        page.search_entry.set_text("")
        page.crew_dropdown.set_value("Zorglub Crew")
        self.assertEqual(self.shown(crew="Zorglub Crew", text=""), [("Speedball", "Speedball")])
        self.assertEqual(page.result_page.rows[0].disk.crew, "Zorglub Crew")

    def test_a_renamed_title_is_found_by_its_new_name(self) -> None:
        page = self.window.find_page

        def rename(dialog) -> None:
            tetris = next(c.id for c in dialog.detail.contents if c.title == "Tetris")
            dialog.title_entries[tetris].set_text("Blockout Deluxe")

        self.edit("tetris", rename)
        page.search_entry.set_text("blockout deluxe")
        self.assertEqual(self.shown(text="blockout deluxe"), [("Automation 9", "Blockout Deluxe")])
        self.assertEqual(page.result_page.rows[0].matched, ("Blockout Deluxe",))


def tearDownModule() -> None:
    shutil.rmtree(_HOME, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()

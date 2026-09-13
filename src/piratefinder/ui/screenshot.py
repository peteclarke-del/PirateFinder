"""Render the documentation screenshots from the real window.

    PYTHONPATH=src python3 -m piratefinder.ui.screenshot [--out docs/images]
        [--catalogue build/catalogue.sqlite] [--help]

The window is built with the fake backend, walked to each state and drawn
through GTK's own renderer, so nothing depends on a compositor, a theme or a
screenshot tool, and the pictures can be made again whenever the window
changes. Each state is saved in light (``name.png``) and dark
(``name-dark.png``); the light pictures the User Guide shows
(``help_content.help_images()``) are also copied into the package, and
pictures it no longer shows are removed from there. Needs a display; a
Broadway display with no browser attached serves too (see ``render`` and
``Shots.capture_narrow``).

With ``--catalogue`` the Find states search that catalogue when this version
can read it, and fall back to the synthetic discs otherwise. Pictures in the
details pane are always the fake backend's synthetic ones, never downloaded
screenshots.
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
import tempfile
from dataclasses import replace
from pathlib import Path

# A picture of the window must not show, or change, the user's own settings.
_SCRATCH = Path(tempfile.mkdtemp(prefix="piratefinder-shots-"))
for _variable in ("XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME"):
    os.environ[_variable] = str(_SCRATCH / _variable.lower())

import gi  # noqa: E402

from .application import PirateFinderApplication  # noqa: E402

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Gtk  # noqa: E402

from ..jobs.queue import new_item  # noqa: E402
from ..models import (  # noqa: E402
    Availability,
    DiskKind,
    Platform,
    ResultMode,
    SessionSummary,
    SortOrder,
    WriteOutcome,
    WriteProgress,
    WriteStatus,
)
from ..settings import Settings  # noqa: E402
from .backend import set_fetch_media  # noqa: E402
from .fake_backend import (  # noqa: E402
    BOOT_VIRUS,
    BOOT_VIRUS_DISK,
    LIBRARY,
    SYNTHETIC,
    FakeBackend,
    synthetic_media,
)
from .find_page import largest_crew  # noqa: E402
from .help_content import help_images  # noqa: E402
from .widgets import set_fraction  # noqa: E402

ROOT = Path(__file__).resolve().parents[3]
HELP_DIRECTORY = Path(__file__).resolve().parent.parent / "data" / "help"
WIDTH = 1280
HEIGHT = 800
# The narrow picture, drawn in a window of its own (see Shots.capture_narrow),
# small enough for a 1024 by 768 screen.
NARROW_WIDTH = 560
NARROW_HEIGHT = 720
# The states whose light pictures the User Guide shows.
HELP_STATES = frozenset(Path(name).stem for name in help_images())
# The credit shown under the invented pictures, so nobody takes them for real ones.
SYNTHETIC_NAME = "Illustration for this guide"
# Discs the details and virus pictures show when the catalogue has them. Any
# other disc with the same features serves when it does not.
DETAIL_DISC = ("pp 51", "Pompey Pirates 51", "Falcon: The Mission Disk - Volume 2")
TRIVIA_TITLE = "Falcon"  # a title of that disc with a fact from Atari Legend
FLAGGED_DISCS = ("Blitzkrieg & Bomb Jack & Bubble Bobble & Buggy Boy",)


def settle(milliseconds: int = 300) -> None:
    """Let real time pass so the frame clock ticks and workers report back."""
    context = GLib.MainContext.default()
    done: list[bool] = []
    GLib.timeout_add(milliseconds, lambda: done.append(True) and False)
    while not done:
        context.iteration(True)


# A width between the two breakpoints of the main window (600 and 900 sp).
BETWEEN_BREAKPOINTS = 750
# Frames waited for before a window that draws nothing counts as broken.
RENDER_ATTEMPTS = 10
# The font resolution the pictures are drawn at, the usual desktop default.
SCREEN_DPI = 96
# Layouts made before each picture; a breakpoint that changes the window needs a second.
LAYOUT_PASSES = 3


def render(window: Gtk.Window, path: Path, width: int = WIDTH, height: int = HEIGHT) -> None:
    if width < BETWEEN_BREAKPOINTS < window.get_width():
        # Some compositors will not shrink a wide window straight to a phone
        # width while its details pane is beside the results; a stop between
        # the breakpoints lets the layout collapse first.
        window.set_default_size(BETWEEN_BREAKPOINTS, height)
        settle(450)
    window.set_default_size(width, height)
    node = None
    for _attempt in range(RENDER_ATTEMPTS):
        # A display that paints only when asked, such as Broadway with no
        # browser attached, may not have drawn the window yet: wait a frame more.
        window.queue_draw()
        settle(450)
        if (window.get_width(), window.get_height()) != (width, height):
            # The display cannot give the window the picture's size (Broadway
            # without a browser has a 1024 by 768 screen), and then GTK does
            # not lay it out again: lay it out at that size here, again after
            # the events a layout sets off, so what a breakpoint changed is
            # laid out as well.
            for _pass in range(LAYOUT_PASSES):
                window.allocate(width, height, -1, None)
                context = GLib.MainContext.default()
                while context.pending():
                    context.iteration(False)
            window.allocate(width, height, -1, None)
        paintable = Gtk.WidgetPaintable.new(window)
        snapshot = Gtk.Snapshot()
        paintable.snapshot(snapshot, width, height)
        node = snapshot.to_node()
        if node is not None:
            break
    if node is None:
        raise RuntimeError(f"{path.name}: the window rendered nothing")
    texture = window.get_renderer().render_texture(node, None)
    texture.save_to_png(str(path))
    print(f"wrote {path}")


def scroll_to(scroller: Gtk.ScrolledWindow, widget: Gtk.Widget, above: int) -> None:
    """Scroll so ``widget`` starts ``above`` pixels below the top of ``scroller``.

    The scrolled child is a viewport, so positions in it are already offset
    by the current scroll position.
    """
    adjustment = scroller.get_vadjustment()
    result = widget.translate_coordinates(scroller.get_child(), 0, 0)
    if result:
        adjustment.set_value(max(0, result[-1] + adjustment.get_value() - above))


CATALOGUE: Path | None = None  # set by --catalogue


class CatalogueShotsBackend(FakeBackend):
    """The fake backend, except that searching reads a real catalogue.

    Search results and disc details in the pictures then show what is really
    in the catalogue, while the queue, sessions and history stay synthetic and
    reproducible. Pictures are always the fake backend's synthetic ones, so no
    downloaded screenshot ends up in the documentation.
    """

    def __init__(self, settings: Settings, catalogue: object) -> None:
        from ..library.userdb import UserDatabase
        from .real_backend import RealBackend

        super().__init__(settings)
        userdb = UserDatabase.open(_SCRATCH / "shots-user.sqlite")
        real_settings = Settings(path=None)
        real_settings.fetch_media = False
        self.real = RealBackend(real_settings, userdb, catalogue)

    def catalogue_info(self):
        info = self.real.catalogue_info()
        return replace(info, sources=(*info.sources, {"id": SYNTHETIC, "name": SYNTHETIC_NAME}))

    def search_page(self, query):
        return self.real.search_page(query)

    def facets(self):
        return self.real.facets()

    def detail(self, disk_id):
        detail = self.real.detail(disk_id)
        media = synthetic_media(detail.disk, detail.contents)
        return replace(detail, media=media)

    def summaries(self, disk_id, content_id=None):
        return []

    def clean_alternates(self, disk_id):
        return self.real.clean_alternates(disk_id)

    def save_details(self, disk_id, values, titles=None):
        self.real.save_details(disk_id, values, titles)

    def revert_details(self, disk_id):
        self.real.revert_details(disk_id)


def open_catalogue(path: Path):
    """The catalogue at ``path`` when this version can read it, else None and why."""
    from ..catalogue.store import Catalogue

    try:
        return Catalogue.open(path), ""
    except Exception as error:  # noqa: BLE001 - reported, then the fake is used
        return None, str(error)


def make_backend() -> FakeBackend:
    settings = Settings(path=None)
    settings.library_folders = [f"{LIBRARY}/Atari ST", f"{LIBRARY}/Amiga", "/mnt/nas/Floppies"]
    settings.download_folder = f"{LIBRARY}/PirateFinder"
    if CATALOGUE is not None:
        catalogue, error = open_catalogue(CATALOGUE)
        if catalogue is not None:
            return CatalogueShotsBackend(settings, catalogue)
        print(f"using the synthetic discs: {error}", file=sys.stderr)
    return FakeBackend(settings)


MSA_NOTE = "Unpacked the MSA archive to a plain sector image."  # as images.prepare says it


def sample_summary() -> SessionSummary:
    return SessionSummary(
        "2026-09-12T14:03:00",
        "2026-09-12T14:21:00",
        "A",
        (
            (
                "Automation 250",
                WriteOutcome(WriteStatus.VERIFIED, "Every track verified.", retries=1),
                "Automation 250.st",
            ),
            (
                "Pompey Pirates 51",
                WriteOutcome(WriteStatus.VERIFIED, "Every track verified.", notes=(MSA_NOTE,)),
                "PP51.msa",
            ),
            (
                "Skid Row Compact 128",
                WriteOutcome(WriteStatus.VERIFIED, "Every track verified."),
                "Internet Archive (archive.org)",
            ),
            (
                "D-Bug 100 B",
                WriteOutcome(
                    WriteStatus.FAILED,
                    "Track 34.1 did not verify after 3 retries.",
                    retries=3,
                    failed_tracks=("34.1",),
                ),
                "D-Bug (d-bug.me)",
            ),
            (
                "Speedball 2",
                WriteOutcome(WriteStatus.VERIFIED, "Every track verified."),
                "Internet Archive (archive.org)",
            ),
        ),
    )


class Shots:
    def __init__(self, app: PirateFinderApplication, out: Path, copy_help: bool) -> None:
        self.app = app
        self.out = out
        self.copy_help = copy_help

    @property
    def window(self):
        return self.app.window

    def capture(self, name: str, prepare, *, target=None, **size) -> None:
        """Draw one state in light and dark; ``target()`` names another window to draw."""
        manager = Adw.StyleManager.get_default()
        for scheme, suffix in (
            (Adw.ColorScheme.FORCE_LIGHT, ""),
            (Adw.ColorScheme.FORCE_DARK, "-dark"),
        ):
            manager.set_color_scheme(scheme)
            cleanup = prepare()
            path = self.out / f"{name}{suffix}.png"
            render(target() if target is not None else self.window, path, **size)
            if cleanup is not None:
                cleanup()
                settle(200)
            # A dialog closes at the end of its animation, which a display
            # nobody watches (Broadway without a browser) never runs; the next
            # state must not find it still open.
            while (dialog := self.window.get_visible_dialog()) is not None:
                dialog.force_close()
                settle(100)
            if not suffix and self.copy_help and name in HELP_STATES:
                HELP_DIRECTORY.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(path, HELP_DIRECTORY / f"{name}.png")
        manager.set_color_scheme(Adw.ColorScheme.DEFAULT)

    def capture_narrow(self, name: str, prepare, width: int, height: int) -> None:
        """Draw one state in a second main window made at ``width`` by ``height``.

        A display that never resizes a window once it is shown (Broadway with
        no browser) still gives a new one the size it asks for, and the window's
        breakpoints then apply as they do for a person with a narrow window.
        """
        from .window import MainWindow

        wide = self.app.window
        narrow = MainWindow(application=self.app, backend=wide.backend)
        narrow.set_default_size(width, height)
        narrow.present()
        settle(1500)
        self.app.window = narrow
        try:
            self.capture(name, prepare, width=width, height=height)
        finally:
            self.app.window = wide
            narrow.destroy()
            settle(200)

    # States

    def reset_find(self) -> None:
        page = self.window.find_page
        self.window.show_page("find")
        page.close_detail()
        page.clear_checked()
        page._quiet = True
        try:
            page.search_entry.set_text("")
            page.set_mode(ResultMode.TITLES)
            page.set_sort(SortOrder.RELEVANCE)
            page.pager.set_page_size(100)
        finally:
            page._quiet = False
        page.clear_filters()
        settle(300)

    def welcome(self):
        self.reset_find()
        settle(400)

    def find(self):
        """Titles for a search with a filter on, two ticked and one in the details pane."""
        page = self.window.find_page
        self.reset_find()
        page.platform_dropdown.set_value(Platform.ATARI_ST)
        page.search_for("rick dangerous")
        settle(900)
        rows = page.results

        # Numbered crew menus that can be written first: that is what the
        # application is for.
        def rank(index: int) -> tuple:
            row = rows[index]
            return (
                row.disk.kind is not DiskKind.MENU or row.disk.series_id is None,
                row.availability is Availability.MISSING,
                not row.disk.year,
                bool(row.disk.condition.strip()),
                index,
            )

        # Among the rows on the first screen, so the picture needs no scrolling
        # and is the same in light and dark.
        chosen = sorted(range(min(len(rows), 12)), key=rank)[:1]
        if chosen:
            # A second one a little further down, so both ticks are on screen.
            below = range(chosen[0] + 1, min(len(rows), chosen[0] + 8, 16))
            chosen += sorted(below, key=rank)[:1]
        for index in chosen:
            page.set_checked(rows[index], True)
        page.table.refresh_checks()
        if chosen:
            page.table.select_index(chosen[0])
        settle(1200)
        # Selecting scrolls the table by however far it has to; start from the top.
        page.table.get_vadjustment().set_value(0)
        settle(200)
        return self.reset_find

    def find_discs(self):
        """Every disc of the largest crew by number: the Discs mode and the pager."""
        page = self.window.find_page
        self.reset_find()
        crew = largest_crew(page.facets, self.window.catalogue_info().series)
        if not crew:
            return self.reset_find
        page.pager.set_page_size(50)
        page.set_mode(ResultMode.DISCS)
        page.browse_crew(crew)
        settle(700)
        page.go_to_page(1)
        settle(700)
        return self.reset_find

    def find_filters(self):
        """Titles of one crew and one year, sorted by title: the filter bar in use."""
        page = self.window.find_page
        self.reset_find()
        crews = {name for name, _count in page.facets.crews}
        crew = "Automation" if "Automation" in crews else largest_crew(page.facets)
        years = [year for year, _count in page.facets.years]
        page._quiet = True
        try:
            page.crew_dropdown.set_value(crew)
            page.year_dropdown.set_value(1990 if 1990 in years else (years or [None])[0])
            page.set_sort(SortOrder.TITLE)
        finally:
            page._quiet = False
        page._filters_changed()
        settle(900)
        return self.reset_find

    def find_unmatched(self):
        """A search that some library files match without matching any catalogue disc."""
        page = self.window.find_page
        self.reset_find()
        page.search_for("menu")
        settle(900)
        return self.reset_find

    def select_disc(self, text: str, label: str, mode=ResultMode.DISCS, title: str = ""):
        """Search for ``text`` and select the row of the disc called ``label``.

        ``title`` picks the row of that title on the disc. Returns the row,
        or None when the search does not find it.
        """
        page = self.window.find_page
        page.set_mode(mode)
        page.search_for(text)
        settle(900)
        for row in page.results:
            if row.disk.label == label and (not title or row.title == title):
                page.table.select_key((row.key,), notify=True)
                settle(1200)
                return row
        return None

    def details(self, title: str = ""):
        """A game on a menu disk in the details pane, from the top."""
        page = self.window.find_page
        self.reset_find()
        text, label, game = DETAIL_DISC
        if self.select_disc(text, label, ResultMode.TITLES, title or game) is None:
            page.search_for("automation 250")
            settle(900)
            page.table.select_index(0)
            settle(1200)
        return self.reset_find

    def _details_at(self, part: str, title: str = ""):
        cleanup = self.details(title)
        detail = self.window.find_page.detail
        widget = getattr(detail, part)
        if widget.get_visible():
            scroll_to(detail.scroller, widget, 12)
        settle(300)
        return cleanup

    def details_titles(self):
        """The details pane scrolled to On This Disc and About the Crew."""
        return self._details_at("titles_group")

    def details_trivia(self):
        """The details pane scrolled to Trivia, Dumps and Links, for a title with a fact."""
        return self._details_at("trivia_group", TRIVIA_TITLE)

    def details_pictures_off(self):
        """The picture area with pictures and summaries switched off."""
        window = self.window
        settings = window.backend.settings
        set_fetch_media(settings, False)
        window.settings_changed("fetch_media")
        self.details()

        def back() -> None:
            set_fetch_media(settings, True)
            window.settings_changed("fetch_media")
            self.reset_find()

        return back

    def edit_details(self):
        """The Edit Details form for a disc whose release date was corrected."""
        cleanup = self.details()
        window = self.window
        pane = window.find_page.detail
        if pane.detail is None:
            return cleanup
        disk = pane.detail.disk
        window.backend.save_details(disk.id, {"date": f"{disk.year or 1990:04d}-06-21"})
        window.find_page.reload_detail()
        settle(900)
        pane.edit_details()
        settle(600)

        def back() -> None:
            dialog = window.get_visible_dialog()
            if dialog is not None:
                dialog.force_close()
            window.backend.revert_details(disk.id)
            settle(200)
            cleanup()

        return back

    def download(self):
        """Download Only in progress, as the pane shows it."""
        cleanup = self.details()
        detail = self.window.find_page.detail
        detail.download_box.set_visible(True)
        set_fraction(detail.download_bar, 0.42, "Downloading 42%")
        settle(200)

        def back() -> None:
            detail.download_box.set_visible(False)
            cleanup()

        return back

    def virus(self):
        """The details of a disc whose dump TOSEC flags with a virus."""
        page = self.window.find_page
        self.reset_find()
        detail = page.detail
        for label in FLAGGED_DISCS:
            if self.select_disc(label, label) is not None and detail.virus_box.get_visible():
                break
        else:
            # Any flagged disc will do: browse the Amiga discs for one.
            page.set_mode(ResultMode.DISCS)
            page.pager.set_page_size(500)
            page.platform_dropdown.set_value(Platform.AMIGA)
            settle(900)
            candidates = []
            for _page in range(20):
                candidates += [row for row in page.results if row.virus]
                if len(candidates) >= 12 or not page.step_page(1):
                    break
                settle(700)
            # Discs with titles first, then those that can be written.
            candidates.sort(
                key=lambda row: (not row.summary, row.availability is Availability.MISSING)
            )
            page.platform_dropdown.set_value(None)
            page.pager.set_page_size(100)
            for row in candidates[:12]:
                if self.select_disc(row.disk.label, row.disk.label) is not None and (
                    detail.virus_box.get_visible()
                ):
                    break
        if detail.virus_box.get_visible():
            scroll_to(detail.scroller, detail.title, 12)
        settle(300)
        return self.reset_find

    def _boot_virus_backend(self) -> FakeBackend:
        """The fake backend, with one disc whose ADF in the library holds a boot virus."""
        backend = FakeBackend(self.window.backend.settings)
        path = f"{LIBRARY}/Amiga/Skid Row Compact 13.adf"
        backend.local_disks[BOOT_VIRUS_DISK] = path
        backend.set_infected({(path, ""): BOOT_VIRUS})
        return backend

    def virus_boot(self):
        """A removable boot block virus on a library file: Remove Before Writing.

        No real catalogue disc is known to carry one in a library that is
        empty, so this state borrows the fake backend's synthetic disc.
        """
        window = self.window
        real = window.backend
        fake = self._boot_virus_backend()
        window.backend = fake
        self.reset_find()
        self.select_disc("skid row compact 13", "Skid Row Compact 13")
        detail = window.find_page.detail
        if detail.virus_box.get_visible():
            scroll_to(detail.scroller, detail.title, 12)
        settle(300)

        def back() -> None:
            window.backend = real
            fake.close()
            self.reset_find()

        return back

    def clean_dialog(self):
        """The question asked before a stored image is cleaned."""
        cleanup = self.virus_boot()
        detail = self.window.find_page.detail
        if detail.clean_row.get_visible():
            detail.clean_button.emit("clicked")
        settle(400)

        def back() -> None:
            dialog = self.window.get_visible_dialog()
            if dialog is not None:
                dialog.force_close()
            settle(200)
            cleanup()

        return back

    def no_device(self):
        """The bar under the header when no Greaseweazle answers."""
        window = self.window
        backend = window.backend
        backend.present = False
        self.reset_find()
        window.follow_device()
        settle(500)

        def back() -> None:
            backend.present = True
            window.follow_device()
            settle(300)

        return back

    def no_results(self):
        page = self.window.find_page
        self.reset_find()
        page.search_for("zzkq")
        settle(400)
        return self.reset_find

    def queue(self):
        window = self.window
        backend = window.backend
        backend.queue_clear()
        backend.queue_add(
            [
                new_item("Automation 250", Platform.ATARI_ST, disk_id=1),
                new_item("Pompey Pirates 51", Platform.ATARI_ST, disk_id=4),
                new_item("Skid Row Compact 128", Platform.AMIGA, disk_id=8, copies=2),
                new_item("D-Bug 100 B", Platform.ATARI_ST, disk_id=7),
            ]
        )
        window.queue_page.refresh()
        window.show_page("queue")
        settle(300)

    def queue_running(self):
        window = self.window
        page = window.queue_page
        window.show_page("queue")
        item = new_item("Pompey Pirates 51", Platform.ATARI_ST, disk_id=4)
        item.notes = [MSA_NOTE]
        page.set_visible_child_name("running")
        page.cancel_button.set_sensitive(True)
        page._retries = 0
        page.session_stage(item, 1, 5, "write", f"Writing {item.label}")
        page.session_progress(item, WriteProgress(0.43, 34, 1, 69, 160, 1, "T34.1: Writing"))
        page.session_progress(item, WriteProgress(0.43, 34, 1, 69, 160, 0, "T34.1: Writing"))

        def back() -> None:
            page.refresh()
            page.set_visible_child_name("list")

        return back

    def insert_prompt(self):
        window = self.window
        page = window.queue_page
        window.show_page("queue")
        page.set_visible_child_name("running")
        item = new_item("Skid Row Compact 128", Platform.AMIGA, disk_id=8)
        page.cancel_button.set_sensitive(True)
        # As in a session: the image is prepared, then the floppy asked for.
        page.session_stage(item, 2, 5, "prepare", f"Preparing {item.label}")
        page.session_stage(item, 2, 5, "insert", f"Insert a disk for {item.label}")
        from .bridge import PendingAnswer

        page.session_ask_insert(item, 2, 5, "", PendingAnswer())
        settle(300)

        def close() -> None:
            page.answer_insert_prompt("stop")
            page._cancelling = False
            page.refresh()
            page.set_visible_child_name("list")

        return close

    def insert_protected(self):
        """The insert prompt again, after the floppy turned out to be write-protected."""
        window = self.window
        page = window.queue_page
        window.show_page("queue")
        page.set_visible_child_name("running")
        item = new_item("Automation 250", Platform.ATARI_ST, disk_id=1)
        reason = "The disk is write-protected. Close the write-protect hole and try again."
        page.cancel_button.set_sensitive(True)
        # The write stopped at once, as it does on a write-protected disk.
        page.session_stage(item, 0, 5, "write", f"Writing {item.label}")
        page.session_stage(item, 0, 5, "insert", reason)
        from .bridge import PendingAnswer

        page.session_ask_insert(item, 0, 5, reason, PendingAnswer())
        settle(300)

        def close() -> None:
            page.answer_insert_prompt("stop")
            page._cancelling = False
            page.refresh()
            page.set_visible_child_name("list")

        return close

    def summary(self):
        window = self.window
        page = window.queue_page
        window.show_page("queue")
        summary = sample_summary()
        items = []
        for label, outcome, _source in summary.items:
            item = new_item(label, Platform.ATARI_ST, disk_id=1)
            item.outcome = outcome
            items.append(item)
        page._session_items = items
        page.summary = summary
        page._show_summary(summary)

        def back() -> None:
            page.summary = None
            page.refresh()
            page.set_visible_child_name("list")

        return back

    def library(self):
        window = self.window
        window.show_page("library")
        window.library_page.refresh()
        window.library_page.scroll_to_top()
        settle(400)

    def library_viruses(self):
        """The Library page scrolled to the files with a virus."""
        window = self.window
        window.show_page("library")
        page = window.library_page
        page.refresh()
        settle(500)
        scroller = page.virus_group.get_ancestor(Gtk.ScrolledWindow)
        if scroller is not None and page.virus_group.get_visible():
            # Twice: the rows the refresh brought back change the layout.
            for _time in range(2):
                scroll_to(scroller, page.virus_group, 24)
                settle(300)

    def history(self):
        window = self.window
        window.backend.sessions[:] = [sample_summary()]
        window.show_page("history")
        window.history_page.refresh()
        settle(400)
        rows = window.history_page.rows.rows
        if rows:
            rows[0].set_expanded(True)

    def preferences(self, page: str = "general"):
        window = self.window
        self.reset_find()
        dialog = window.show_preferences()
        dialog.set_visible_page_name(page)
        settle(300)
        return dialog.force_close

    def about_update(self):
        """The About window after Check for Application Updates found a newer version."""
        from ..app_update import AppRelease
        from .app_updater import AppUpdater

        window = self.window
        self.reset_find()
        version = "0.3.0"
        package = f"PirateFinder_{version}_ubuntu-24.04_amd64.deb"
        window.backend.app_release = AppRelease(
            version,
            f"v{version}",
            f"PirateFinder {version}",
            f"https://github.com/peteclarke-del/PirateFinder/releases/tag/v{version}",
            package_name=package,
            package_url=f"https://github.com/peteclarke-del/PirateFinder/releases/download/v{version}/{package}",
            package_size=24_000_000,
            sums_url="https://github.com/peteclarke-del/PirateFinder/releases/download/v0.3.0/SHA256SUMS",
        )
        window.show_about()
        window.app_updater.check()
        settle(500)

        def close() -> None:
            if window.about_dialog is not None:
                window.about_dialog.force_close()
            window.backend.app_release = None
            window.app_updater = AppUpdater(window)
            settle(200)

        return close

    def preferences_greaseweazle(self):
        return self.preferences("greaseweazle")

    def preferences_catalogue(self):
        return self.preferences("catalogue")

    def preferences_virus(self):
        """The Catalogue page of Preferences scrolled to Virus Detection."""
        close = self.preferences("catalogue")
        dialog = self.window.show_preferences()
        group = dialog.virus_detection_group
        scroller = group.get_ancestor(Gtk.ScrolledWindow)
        if scroller is not None:
            scroll_to(scroller, group, 200)
        settle(300)
        return close

    def preferences_ipf(self):
        """The Greaseweazle page of Preferences scrolled to IPF Support, not installed."""
        close = self.preferences("greaseweazle")
        dialog = self.window.show_preferences()
        group = dialog.ipf_group
        scroller = group.get_ancestor(Gtk.ScrolledWindow)
        if scroller is not None:
            scroll_to(scroller, group, 200)
        settle(300)
        return close

    def help_window(self):
        """The User Guide window, open at Finding Discs."""
        self.reset_find()
        self.window.show_help("searching")
        settle(600)
        guide = self.window._help_window

        def close() -> None:
            guide.set_visible(False)
            settle(200)

        return close

    def shortcuts(self):
        """The Keyboard Shortcuts window."""
        from .diagnostics import shortcuts_window

        self._shortcuts = shortcuts_window(self.window)
        self._shortcuts.present()
        settle(600)

        def close() -> None:
            self._shortcuts.destroy()
            settle(200)

        return close

    def no_catalogue(self):
        window = self.window
        page = window.find_page
        from .backend import CatalogueInfo

        page.set_catalogue(CatalogueInfo(False))

        def back() -> None:
            page.set_catalogue(window.catalogue_info())

        return back


def prune_help_pictures() -> None:
    """Remove pictures the User Guide no longer shows from the package, and name
    any it shows that no state drew."""
    for path in HELP_DIRECTORY.glob("*.png"):
        if path.stem not in HELP_STATES:
            path.unlink()
            print(f"removed {path}")
    for name in help_images():
        if not (HELP_DIRECTORY / name).is_file():
            print(f"the User Guide shows {name}, which no state drew", file=sys.stderr)


def run(out: Path, copy_help: bool) -> int:
    out.mkdir(parents=True, exist_ok=True)
    app = PirateFinderApplication(
        "com.github.pclarke.PirateFinderShots", unique=False, backend_factory=make_backend
    )
    status = {"code": 0}

    def on_activate(application) -> None:
        # Transitions finish at once, so no picture catches one half way.
        settings = Gtk.Settings.get_default()
        settings.set_property("gtk-enable-animations", False)
        # Lengths in sp follow the font resolution, which a display without a
        # desktop session (Broadway) does not give; without it every clamp
        # would shrink its page to the narrowest it can be.
        settings.set_property("gtk-xft-dpi", SCREEN_DPI * 1024)
        try:
            shots = Shots(application, out, copy_help)
            window = application.window
            window.set_default_size(WIDTH, HEIGHT)
            settle(800)
            shots.capture("welcome", shots.welcome)
            shots.capture("no-device", shots.no_device)
            shots.capture("find", shots.find)
            shots.capture("find-discs", shots.find_discs)
            shots.capture("find-filters", shots.find_filters)
            shots.capture("find-unmatched", shots.find_unmatched)
            shots.capture("details", shots.details)
            shots.capture("details-titles", shots.details_titles)
            shots.capture("details-trivia", shots.details_trivia)
            shots.capture("details-pictures-off", shots.details_pictures_off)
            shots.capture("edit-details", shots.edit_details)
            shots.capture("download", shots.download)
            shots.capture("virus", shots.virus)
            shots.capture("virus-boot", shots.virus_boot)
            shots.capture("clean-dialog", shots.clean_dialog)
            shots.capture("no-results", shots.no_results)
            shots.capture("queue", shots.queue)
            shots.capture("queue-running", shots.queue_running)
            shots.capture("insert-prompt", shots.insert_prompt)
            shots.capture("insert-protected", shots.insert_protected)
            shots.capture("summary", shots.summary)
            shots.capture("library", shots.library)
            shots.capture("library-viruses", shots.library_viruses)
            shots.capture("history", shots.history)
            shots.capture("preferences", shots.preferences)
            shots.capture("preferences-greaseweazle", shots.preferences_greaseweazle)
            shots.capture("preferences-catalogue", shots.preferences_catalogue)
            shots.capture("preferences-virus", shots.preferences_virus)
            shots.capture("preferences-ipf", shots.preferences_ipf)
            shots.capture("no-catalogue", shots.no_catalogue)
            shots.capture("about-update", shots.about_update)
            shots.capture_narrow("find-narrow", shots.find, NARROW_WIDTH, NARROW_HEIGHT)
            shots.capture(
                "help",
                shots.help_window,
                target=lambda: window._help_window,
                width=1000,
                height=720,
            )
            shots.capture(
                "shortcuts", shots.shortcuts, target=lambda: shots._shortcuts, width=900, height=600
            )
            if copy_help:
                prune_help_pictures()
        except Exception:  # noqa: BLE001 - report and quit
            import traceback

            traceback.print_exc()
            status["code"] = 1
        finally:
            application.quit()

    app.connect_after("activate", on_activate)
    app.run([])
    shutil.rmtree(_SCRATCH, ignore_errors=True)
    return status["code"]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path, default=ROOT / "docs" / "images")
    parser.add_argument(
        "--no-help-copy", action="store_true", help="do not copy pictures into the package"
    )
    parser.add_argument(
        "--catalogue",
        type=Path,
        help="search a real catalogue.sqlite instead of the synthetic disks",
    )
    args = parser.parse_args(argv)
    global CATALOGUE
    CATALOGUE = args.catalogue
    return run(args.out, not args.no_help_copy)


if __name__ == "__main__":
    sys.exit(main())

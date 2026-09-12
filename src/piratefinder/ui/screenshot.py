"""Render the documentation screenshots from the real window.

    PYTHONPATH=src python3 -m piratefinder.ui.screenshot [--out docs/images]
        [--catalogue build/catalogue.sqlite] [--help]

The window is built with the fake backend, walked to each state and drawn
through GTK's own renderer, so nothing depends on a compositor, a theme or a
screenshot tool, and the pictures can be made again whenever the window
changes. Each state is saved in light (``name.png``) and dark
(``name-dark.png``); the light pictures are also copied into the package for
the User Guide. Needs a display.
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
import tempfile
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

from ..models import (  # noqa: E402
    DiskKind,
    Platform,
    SessionSummary,
    WriteOutcome,
    WriteProgress,
    WriteStatus,
)
from ..settings import Settings  # noqa: E402
from . import formatting as fmt  # noqa: E402
from .fake_backend import LIBRARY, FakeBackend  # noqa: E402

ROOT = Path(__file__).resolve().parents[3]
HELP_DIRECTORY = Path(__file__).resolve().parent.parent / "data" / "help"
WIDTH = 1180
HEIGHT = 780
HELP_STATES = ("find", "queue-running", "summary", "library", "preferences")


def settle(milliseconds: int = 300) -> None:
    """Let real time pass so the frame clock ticks and workers report back."""
    context = GLib.MainContext.default()
    done: list[bool] = []
    GLib.timeout_add(milliseconds, lambda: done.append(True) and False)
    while not done:
        context.iteration(True)


def render(window: Gtk.Window, path: Path, width: int = WIDTH, height: int = HEIGHT) -> None:
    window.set_default_size(width, height)
    window.queue_draw()
    settle(450)
    paintable = Gtk.WidgetPaintable.new(window)
    snapshot = Gtk.Snapshot()
    paintable.snapshot(snapshot, width, height)
    node = snapshot.to_node()
    if node is None:
        raise RuntimeError(f"{path.name}: the window rendered nothing")
    texture = window.get_renderer().render_texture(node, None)
    texture.save_to_png(str(path))
    print(f"wrote {path}")


CATALOGUE: Path | None = None  # set by --catalogue


class CatalogueShotsBackend(FakeBackend):
    """The fake backend, except that searching reads a real catalogue.

    Search results and disk details in the pictures then show what is really
    on each disk, while the queue, sessions and history stay synthetic and
    reproducible.
    """

    def __init__(self, settings: Settings, catalogue_path: Path) -> None:
        from ..catalogue.store import Catalogue
        from ..library.userdb import UserDatabase
        from .real_backend import RealBackend

        super().__init__(settings)
        userdb = UserDatabase.open(_SCRATCH / "shots-user.sqlite")
        self.real = RealBackend(Settings(path=None), userdb, Catalogue.open(catalogue_path))

    def catalogue_info(self):
        return self.real.catalogue_info()

    def search(self, text, filters):
        return self.real.search(text, filters)

    def detail(self, disk_id):
        return self.real.detail(disk_id)


def make_backend() -> FakeBackend:
    settings = Settings(path=None)
    settings.library_folders = [f"{LIBRARY}/Atari ST", f"{LIBRARY}/Amiga", "/mnt/nas/Floppies"]
    settings.download_folder = f"{LIBRARY}/PirateFinder"
    if CATALOGUE is not None:
        return CatalogueShotsBackend(settings, CATALOGUE)
    return FakeBackend(settings)


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
                WriteOutcome(WriteStatus.VERIFIED, "Every track verified."),
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

    def capture(self, name: str, prepare, **size) -> None:
        manager = Adw.StyleManager.get_default()
        for scheme, suffix in (
            (Adw.ColorScheme.FORCE_LIGHT, ""),
            (Adw.ColorScheme.FORCE_DARK, "-dark"),
        ):
            manager.set_color_scheme(scheme)
            cleanup = prepare()
            path = self.out / f"{name}{suffix}.png"
            render(self.window, path, **size)
            if cleanup is not None:
                cleanup()
                settle(200)
            if not suffix and self.copy_help and name in HELP_STATES:
                HELP_DIRECTORY.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(path, HELP_DIRECTORY / f"{name}.png")
        manager.set_color_scheme(Adw.ColorScheme.DEFAULT)

    # States

    def welcome(self):
        window = self.window
        window.show_page("find")
        window.find_page.close_detail()
        window.find_page.search_for("")
        settle(400)

    def find(self):
        window = self.window
        page = window.find_page
        window.show_page("find")
        page.search_for("rick dangerous")
        settle(900)
        page.clear_checked()
        # Show a menu disk in the detail pane: that is what the app is for.
        menus = [
            index
            for index, result in enumerate(page.results)
            if result.disk is not None and result.disk.kind is DiskKind.MENU
        ]
        # A numbered crew menu first, when the catalogue has one.
        menus.sort(key=lambda index: page.results[index].disk.series_id is None)
        chosen = menus[:2] or list(range(min(2, len(page.results))))
        for index in chosen:
            page.set_checked(page.results[index], True)
        page._rebind()
        page.selection.set_selected(chosen[0] if chosen else 0)
        settle(600)
        return page.clear_checked

    def no_results(self):
        window = self.window
        window.show_page("find")
        window.find_page.close_detail()
        window.find_page.search_for("zzkq")
        settle(400)

    def queue(self):
        window = self.window
        backend = window.backend
        backend.queue_clear()
        backend.queue_add(
            [
                fmt.new_queue_item("Automation 250", Platform.ATARI_ST, disk_id=1),
                fmt.new_queue_item("Pompey Pirates 51", Platform.ATARI_ST, disk_id=4),
                fmt.new_queue_item("Skid Row Compact 128", Platform.AMIGA, disk_id=8, copies=2),
                fmt.new_queue_item("D-Bug 100 B", Platform.ATARI_ST, disk_id=7),
            ]
        )
        window.queue_page.refresh()
        window.show_page("queue")
        settle(300)

    def queue_running(self):
        window = self.window
        page = window.queue_page
        window.show_page("queue")
        item = fmt.new_queue_item("Pompey Pirates 51", Platform.ATARI_ST, disk_id=4)
        item.notes = ["Converted from MSA to ST for writing."]
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
        item = fmt.new_queue_item("Skid Row Compact 128", Platform.AMIGA, disk_id=8)
        page.session_stage(item, 2, 5, "insert", "Insert a disk for Skid Row Compact 12")
        from .bridge import PendingAnswer

        page.session_ask_insert(item, 2, 5, "", PendingAnswer())
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
            item = fmt.new_queue_item(label, Platform.ATARI_ST, disk_id=1)
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
        settle(400)

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
        window.show_page("find")
        window.find_page.close_detail()
        window.find_page.search_for("")
        dialog = window.show_preferences()
        dialog.set_visible_page_name(page)
        settle(300)
        return dialog.force_close

    def preferences_greaseweazle(self):
        return self.preferences("greaseweazle")

    def preferences_catalogue(self):
        return self.preferences("catalogue")

    def no_catalogue(self):
        window = self.window
        page = window.find_page
        from .backend import CatalogueInfo

        page.set_catalogue(CatalogueInfo(False))

        def back() -> None:
            page.set_catalogue(window.catalogue_info())

        return back


def run(out: Path, copy_help: bool) -> int:
    out.mkdir(parents=True, exist_ok=True)
    app = PirateFinderApplication(
        "com.github.pclarke.PirateFinderShots", unique=False, backend_factory=make_backend
    )
    status = {"code": 0}

    def on_activate(application) -> None:
        try:
            shots = Shots(application, out, copy_help)
            window = application.window
            window.set_default_size(WIDTH, HEIGHT)
            settle(800)
            shots.capture("welcome", shots.welcome)
            shots.capture("find", shots.find)
            shots.capture("no-results", shots.no_results)
            shots.capture("queue", shots.queue)
            shots.capture("queue-running", shots.queue_running)
            shots.capture("insert-prompt", shots.insert_prompt)
            shots.capture("summary", shots.summary)
            shots.capture("library", shots.library)
            shots.capture("history", shots.history)
            shots.capture("preferences", shots.preferences)
            shots.capture("preferences-greaseweazle", shots.preferences_greaseweazle)
            shots.capture("preferences-catalogue", shots.preferences_catalogue)
            shots.capture("no-catalogue", shots.no_catalogue)
            shots.capture("find-narrow", shots.find, width=560, height=820)
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

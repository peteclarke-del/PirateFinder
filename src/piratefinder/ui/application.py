"""GTK application lifecycle."""

from __future__ import annotations

import os

# Terminals opened by Snap-packaged editors export paths to that snap's GTK
# loaders. A system Python that loads one of them can crash the moment GTK
# renders its first icon, so the snap's variables are dropped before GTK is
# imported. This has to run before ``import gi``.
if "SNAP_INSTANCE_NAME" in os.environ or "/snap/" in os.environ.get("GTK_EXE_PREFIX", ""):
    for _name in (
        "GDK_PIXBUF_MODULE_FILE",
        "GDK_PIXBUF_MODULEDIR",
        "GTK_EXE_PREFIX",
        "GTK_PATH",
        "GTK_IM_MODULE_FILE",
        "GIO_MODULE_DIR",
        "LOCPATH",
        "GSETTINGS_SCHEMA_DIR",
    ):
        if "snap" in os.environ.get(_name, ""):
            os.environ.pop(_name, None)
    _library_path = os.environ.get("LD_LIBRARY_PATH", "")
    if "/snap/" in _library_path:
        _kept = [part for part in _library_path.split(":") if part and "/snap/" not in part]
        if _kept:
            os.environ["LD_LIBRARY_PATH"] = ":".join(_kept)
        else:
            os.environ.pop("LD_LIBRARY_PATH", None)

import traceback  # noqa: E402
from collections.abc import Callable  # noqa: E402
from pathlib import Path  # noqa: E402

import gi  # noqa: E402

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gio, GLib, Gtk  # noqa: E402

from ..branding import APPLICATION_ID, APPLICATION_NAME  # noqa: E402
from .backend import Backend, UnavailableBackend  # noqa: E402
from .log import LOG, capture_backend_logging  # noqa: E402

ICON_DIRECTORY = Path(__file__).resolve().parent.parent / "data" / "icons"

ACCELERATORS = {
    "win.focus-search": ["<Control>f"],
    "app.quit": ["<Control>q"],
    "win.help": ["F1"],
    "win.preferences": ["<Control>comma"],
    "win.write-selected": ["<Control>Return"],
    "win.add-selected": ["<Control>plus", "<Control>equal"],
    "win.show-page::find": ["<Alt>1"],
    "win.show-page::queue": ["<Alt>2"],
    "win.show-page::library": ["<Alt>3"],
    "win.show-page::history": ["<Alt>4"],
    "win.shortcuts": ["<Control>question"],
}


def register_icons() -> None:
    """Let GTK find the application icon when it is not installed into a theme.

    The icons live inside the package in the hicolor layout, so this works from
    a source checkout and from an installed copy alike.
    """
    display = Gdk.Display.get_default()
    if display is None or not ICON_DIRECTORY.is_dir():
        return
    theme = Gtk.IconTheme.get_for_display(display)
    if str(ICON_DIRECTORY) not in theme.get_search_path():
        theme.add_search_path(str(ICON_DIRECTORY))


def _default_backend() -> Backend:
    if os.environ.get("PIRATEFINDER_FAKE_BACKEND"):
        from .fake_backend import FakeBackend

        return FakeBackend()
    return Backend.create()


class PirateFinderApplication(Adw.Application):
    """The PirateFinder desktop application."""

    def __init__(
        self,
        application_id: str = APPLICATION_ID,
        *,
        unique: bool = True,
        backend_factory: Callable[[], Backend] | None = None,
    ) -> None:
        # Tests pass their own id and NON_UNIQUE: with the shared id a copy
        # already running would take the activation and the test would do
        # nothing at all.
        flags = Gio.ApplicationFlags.HANDLES_COMMAND_LINE
        if not unique:
            flags |= Gio.ApplicationFlags.NON_UNIQUE
        GLib.set_application_name(APPLICATION_NAME)
        super().__init__(application_id=application_id, flags=flags)
        self._backend_factory = backend_factory or _default_backend
        self.window = None
        # Set by the window after an update, so main() starts the new version.
        self.restart_requested = False
        quit_action = Gio.SimpleAction.new("quit", None)
        quit_action.connect("activate", lambda _action, _parameter: self._quit())
        self.add_action(quit_action)
        for action, accelerators in ACCELERATORS.items():
            self.set_accels_for_action(action, accelerators)

    def do_activate(self) -> None:
        if self.window is None:
            from .window import MainWindow

            capture_backend_logging()
            register_icons()
            try:
                backend = self._backend_factory()
            except Exception as error:  # noqa: BLE001 - still open a window
                LOG.add("startup", "".join(traceback.format_exception(error)).strip())
                backend = UnavailableBackend(str(error))
            try:
                self.window = MainWindow(application=self, backend=backend)
            except Exception:
                # Without a window the application would wait for nothing.
                traceback.print_exc()
                self.quit()
                return
            self.window.set_icon_name(APPLICATION_ID)
        self.window.present()

    def do_command_line(self, _command_line) -> int:
        self.activate()
        return 0

    def _quit(self) -> None:
        if self.window is not None:
            # Closing the window asks first when a disk is being written.
            self.window.close()
        else:
            self.quit()

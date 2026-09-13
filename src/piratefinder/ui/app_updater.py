"""Checking for, downloading and installing a newer PirateFinder, from the About window.

The window keeps one ``AppUpdater``, so an update in progress carries on when
the About window is closed, shows again when it is reopened, and cannot be
started twice. Nothing is checked until the user presses Check for
Application Updates; a check that fails says why and never says "newest".
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gtk  # noqa: E402

from .. import __version__  # noqa: E402
from ..app_update import AppRelease  # noqa: E402
from ..branding import APPLICATION_NAME  # noqa: E402
from ..jobs.cancellation import Cancellation  # noqa: E402
from ..online.releases import UpdateCancelled  # noqa: E402
from . import formatting as fmt  # noqa: E402
from .bridge import Latest, run_in_thread  # noqa: E402
from .log import LOG  # noqa: E402
from .widgets import alert, open_uri, set_fraction  # noqa: E402

CHECK_LABEL = "_Check for Application Updates"
BUSY_WHILE_WRITING = f"{APPLICATION_NAME} can be updated once the disks have been written"
RESTART_WHILE_WRITING = f"{APPLICATION_NAME} can restart once the disks have been written"


@dataclass(frozen=True, slots=True)
class AppUpdateState:
    # "idle", "checking", "current", "available", "downloading", "installing",
    # "installed" or "failed"
    phase: str
    message: str = ""
    fraction: float | None = None
    release: AppRelease | None = None

    @property
    def busy(self) -> bool:
        return self.phase in ("checking", "downloading", "installing")


def system_name(distro: str, arch: str) -> str:
    """ "Ubuntu 24.04 amd64" for ("ubuntu-24.04", "amd64")."""
    name, _, version = distro.partition("-")
    return f"{name.capitalize()} {version} {arch}".replace("  ", " ").strip()


class AppUpdater:
    """The application update, shared by the About window and the main window."""

    def __init__(self, host) -> None:
        self._host = host
        self.state = AppUpdateState("idle")
        self._listeners: list[Callable[[AppUpdateState], None]] = []
        self._cancel: Cancellation | None = None

    def subscribe(self, listener: Callable[[AppUpdateState], None]) -> None:
        self._listeners.append(listener)

    def unsubscribe(self, listener: Callable[[AppUpdateState], None]) -> None:
        if listener in self._listeners:
            self._listeners.remove(listener)

    def _set(self, state: AppUpdateState) -> None:
        self.state = state
        for listener in list(self._listeners):
            listener(state)

    def available_text(self, release: AppRelease) -> str:
        text = f"{release.name} is available. You have version {__version__}."
        if release.installable:
            return text
        target = self._host.backend.app_update_target()
        if target is None:
            return (
                f"{text} This copy runs from its source code, so it cannot update itself: "
                "update the source, or install a package from the release page."
            )
        return (
            f"{text} The release has no package for {system_name(target.distro, target.arch)}; "
            "the release page lists the packages it has."
        )

    def check(self) -> None:
        if self.state.busy:
            return
        self._set(AppUpdateState("checking", "Asking GitHub for the newest version"))
        backend = self._host.backend

        def found(release: AppRelease | None) -> None:
            if release is None:
                newest = f"{APPLICATION_NAME} {__version__} is the newest version"
                self._set(AppUpdateState("current", newest))
                return
            LOG.add("update", f"{release.name} is available")
            self._set(AppUpdateState("available", self.available_text(release), release=release))

        def failed(error: BaseException) -> None:
            message = f"Could not check for a newer version: {error}"
            LOG.add("update", message)
            self._set(AppUpdateState("failed", message))

        run_in_thread(backend.check_app_update, found, failed, name="app-update-check")

    def install(self, release: AppRelease) -> None:
        if self.state.busy:
            return
        if self._host.session_running():
            # The package replaces the gw the session is running.
            self._set(AppUpdateState("available", BUSY_WHILE_WRITING, release=release))
            return
        self._cancel = Cancellation()
        cancel = self._cancel
        self._set(AppUpdateState("downloading", "Downloading the package", 0.0, release))
        backend = self._host.backend

        def progress(done: int, total: int | None) -> None:
            if self.state.phase == "downloading":
                fraction = done / total if total else None
                text = fmt.download_text(done, total)
                self._set(AppUpdateState("downloading", text, fraction, release))

        latest = Latest(progress)

        def downloaded(package) -> None:
            self._cancel = None
            self._set(
                AppUpdateState(
                    "installing",
                    f"Installing {release.name}. The system asks for your password.",
                    None,
                    release,
                )
            )
            run_in_thread(
                lambda: backend.install_app_update(package),
                lambda _result: installed(),
                failed,
                name="app-update-install",
            )

        def installed() -> None:
            LOG.add("update", f"Installed {release.name}")
            message = f"{release.name} is installed. Restart {APPLICATION_NAME} to use it."
            self._set(AppUpdateState("installed", message, release=release))
            self._host.app_update_installed(release)

        def failed(error: BaseException) -> None:
            self._cancel = None
            LOG.add("update", f"The update to {release.name} stopped: {error}")
            if isinstance(error, UpdateCancelled) or cancel.cancelled:
                text = str(error) if isinstance(error, UpdateCancelled) else ""
                message = text or "The update was cancelled."
                self._set(AppUpdateState("available", message, release=release))
                return
            self._set(AppUpdateState("failed", f"The update failed: {error}", release=release))

        run_in_thread(
            lambda: backend.download_app_update(release, latest.post, cancel),
            downloaded,
            failed,
            name="app-update-download",
        )

    def cancel(self) -> None:
        if self._cancel is not None:
            self._cancel.cancel()


class AppUpdateControls(Gtk.Box):
    """The About window's button, status line and progress for the application update."""

    def __init__(self, updater: AppUpdater, host) -> None:
        super().__init__(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=6,
            halign=Gtk.Align.CENTER,
            margin_top=6,
        )
        self.add_css_class("app-update")
        self._updater = updater
        self._host = host
        self.button = Gtk.Button(use_underline=True, halign=Gtk.Align.CENTER)
        self.button.add_css_class("pill")
        self.button.connect("clicked", self._on_button)
        self.append(self.button)
        progress_line = Gtk.Box(spacing=6)
        self.progress = Gtk.ProgressBar(show_text=True, hexpand=True, valign=Gtk.Align.CENTER)
        self.progress.set_size_request(220, -1)
        progress_line.append(self.progress)
        self.cancel_button = Gtk.Button(label="_Cancel", use_underline=True)
        self.cancel_button.connect("clicked", lambda _button: updater.cancel())
        progress_line.append(self.cancel_button)
        self.progress_line = progress_line
        self.append(progress_line)
        self.spinner = Gtk.Spinner(halign=Gtk.Align.CENTER)
        self.append(self.spinner)
        self.status = Gtk.Label(
            wrap=True,
            justify=Gtk.Justification.CENTER,
            max_width_chars=44,
            selectable=True,
        )
        self.status.add_css_class("dim-label")
        self.append(self.status)
        self.page_button = Gtk.Button(
            label="Release _Page", use_underline=True, halign=Gtk.Align.CENTER
        )
        self.page_button.add_css_class("flat")
        self.page_button.connect("clicked", self._on_page)
        self.append(self.page_button)
        updater.subscribe(self.show)
        self.show(updater.state)

    def detach(self) -> None:
        """Stop following the updater, when the About window closes."""
        self._updater.unsubscribe(self.show)

    def show(self, state: AppUpdateState) -> None:
        release = state.release
        self.status.set_text(state.message)
        self.status.set_visible(bool(state.message))
        self.progress_line.set_visible(state.phase == "downloading")
        if state.phase == "downloading":
            set_fraction(self.progress, state.fraction, "")
        installing = state.phase == "installing"
        self.spinner.set_visible(installing)
        if installing:
            self.spinner.start()
        else:
            self.spinner.stop()
        self.button.set_visible(state.phase not in ("downloading", "installing"))
        self.button.set_sensitive(not state.busy)
        suggested = state.phase in ("available", "installed") and bool(
            release is not None and (release.installable or state.phase == "installed")
        )
        if suggested:
            self.button.add_css_class("suggested-action")
        else:
            self.button.remove_css_class("suggested-action")
        self.page_button.set_visible(
            release is not None and bool(release.page_url) and state.phase != "installed"
        )
        if state.phase == "checking":
            self.button.set_label("Checking")
        elif state.phase == "available" and release is not None:
            if release.installable:
                self.button.set_label(f"_Update to {release.version}")
            else:
                self.button.set_label("Open Release _Page")
                self.page_button.set_visible(False)
        elif state.phase == "installed":
            self.button.set_label(f"_Restart {APPLICATION_NAME}")
        else:
            self.button.set_label(CHECK_LABEL)

    def _on_button(self, _button) -> None:
        state = self._updater.state
        release = state.release
        if state.phase == "installed":
            self._host.restart()
        elif state.phase == "available" and release is not None:
            if release.installable:
                self._confirm(release)
            else:
                self._on_page()
        else:
            self._updater.check()

    def _on_page(self, *_args) -> None:
        release = self._updater.state.release
        if release is not None and release.page_url:
            open_uri(self, release.page_url)

    def _confirm(self, release: AppRelease) -> None:
        target = self._host.backend.app_update_target()
        system = system_name(target.distro, target.arch) if target else "this system"
        size = f" ({fmt.human_size(release.package_size)})" if release.package_size else ""
        body = (
            f"Version {release.version} is available; you have {__version__}. The package for "
            f"{system}{size} is downloaded from GitHub, checked against the release's "
            "checksums and installed, which asks for your password. Your settings, library, "
            "queue and history are kept."
        )
        if release.notes:
            body += f"\n\n{release.notes}"

        def respond(response: str) -> None:
            if response == "update":
                self._updater.install(release)

        alert(
            self,
            f"Update {APPLICATION_NAME}?",
            body,
            (("cancel", "_Cancel", ""), ("update", "_Download and Install", "suggested")),
            respond,
        )


def attach_to_about(about: Adw.AboutDialog, controls: Gtk.Widget) -> bool:
    """Put ``controls`` under the version on the About window's first page.

    Adw.AboutDialog has no place for extra widgets, so this finds its version
    button by the "app-version" style class and adds the controls after it.
    False when it is not there, which a test catches on the supported
    libadwaita releases.
    """
    root = about.get_child() or about
    version = _find(root, lambda widget: widget.has_css_class("app-version"))
    parent = version.get_parent() if version is not None else None
    if not isinstance(parent, Gtk.Box):
        LOG.add("update", "The About window has no version button to put the update button under")
        return False
    parent.insert_child_after(controls, version)
    return True


def _find(widget: Gtk.Widget, wanted: Callable[[Gtk.Widget], bool]) -> Gtk.Widget | None:
    if wanted(widget):
        return widget
    child = widget.get_first_child()
    while child is not None:
        found = _find(child, wanted)
        if found is not None:
            return found
        child = child.get_next_sibling()
    return None

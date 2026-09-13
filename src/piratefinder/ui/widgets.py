"""Small pieces shared by every page, so dialogs and controls behave alike."""

from __future__ import annotations

import html
from collections.abc import Callable, Sequence
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gio, GLib, Gtk  # noqa: E402

from .log import LOG  # noqa: E402


def icon_button(
    icon_name: str,
    tooltip: str,
    callback: Callable[[Gtk.Button], None] | None = None,
    *,
    flat: bool = True,
) -> Gtk.Button:
    """An icon-only button with a tooltip and the same text as its accessible name."""
    button = Gtk.Button(icon_name=icon_name, tooltip_text=tooltip, valign=Gtk.Align.CENTER)
    button.update_property([Gtk.AccessibleProperty.LABEL], [tooltip])
    if flat:
        button.add_css_class("flat")
    if callback is not None:
        button.connect("clicked", callback)
    return button


def text_button(
    label: str,
    callback: Callable[[Gtk.Button], None] | None = None,
    *,
    style: str = "",
    tooltip: str = "",
) -> Gtk.Button:
    """A labelled button; ``label`` may contain a mnemonic underscore."""
    button = Gtk.Button.new_with_mnemonic(label)
    button.set_valign(Gtk.Align.CENTER)
    for css in style.split():
        button.add_css_class(css)
    if tooltip:
        button.set_tooltip_text(tooltip)
    if callback is not None:
        button.connect("clicked", callback)
    return button


def plain_row(row_type: type = Adw.ActionRow, title: str = "", subtitle: str = "", **props):
    """A preferences row whose title and subtitle are plain text, never markup.

    Rows parse markup by default, and titles from the catalogue carry "&" and
    "<" ("Sooty & Sweep", URLs with query strings). Markup is switched off
    before any text is set, because text given to the constructor is parsed
    before a later ``set_use_markup(False)`` can take effect.
    """
    row = row_type(**props)
    row.set_use_markup(False)
    if title:
        row.set_title(title)
    if subtitle:
        row.set_subtitle(subtitle)
    return row


def dim_label(text: str = "", *, css: str = "dim-label", **properties) -> Gtk.Label:
    label = Gtk.Label(label=text, xalign=0, wrap=True, **properties)
    for name in css.split():
        label.add_css_class(name)
    return label


def copy_text(widget: Gtk.Widget, text: str) -> None:
    widget.get_clipboard().set(text)


def toast(widget: Gtk.Widget, message: str, *, timeout: int = 4) -> None:
    """Show a toast in the nearest toast overlay, or the window's."""
    item = Adw.Toast(title=html.escape(message, quote=False), timeout=timeout)
    overlay = widget.get_ancestor(Adw.ToastOverlay)
    if overlay is not None:
        overlay.add_toast(item)
        return
    root = widget.get_root()
    if root is not None and hasattr(root, "add_toast"):
        root.add_toast(item)


def show_in_files(widget: Gtk.Widget, path: str) -> None:
    """Open the folder holding ``path`` in the file manager with the file selected."""
    target = Gio.File.new_for_path(path)
    launcher = Gtk.FileLauncher.new(target)

    def done(source, result) -> None:
        try:
            source.open_containing_folder_finish(result)
        except GLib.Error as error:
            LOG.add("files", f"Could not open the folder of {path}: {error.message}")
            toast(widget, "The folder could not be opened")

    launcher.open_containing_folder(widget.get_root(), None, done)


def open_uri(widget: Gtk.Widget, uri: str) -> None:
    launcher = Gtk.UriLauncher.new(uri)

    def done(source, result) -> None:
        try:
            source.launch_finish(result)
        except GLib.Error as error:
            LOG.add("links", f"Could not open {uri}: {error.message}")
            toast(widget, "The link could not be opened")

    launcher.launch(widget.get_root(), None, done)


def choose_folder(
    widget: Gtk.Widget, title: str, initial: str, on_chosen: Callable[[str], None]
) -> None:
    """Ask for a folder with the portal-friendly Gtk.FileDialog."""
    dialog = Gtk.FileDialog(title=title, modal=True)
    if initial and Path(initial).is_dir():
        dialog.set_initial_folder(Gio.File.new_for_path(initial))

    def done(source, result) -> None:
        try:
            chosen = source.select_folder_finish(result)
        except GLib.Error:
            return  # cancelled
        if chosen is not None and chosen.get_path():
            on_chosen(chosen.get_path())

    dialog.select_folder(widget.get_root(), None, done)


def save_text_file(
    widget: Gtk.Widget, title: str, name: str, text: str, on_saved: Callable[[str], None]
) -> None:
    dialog = Gtk.FileDialog(title=title, modal=True, initial_name=name)

    def done(source, result) -> None:
        try:
            chosen = source.save_finish(result)
        except GLib.Error:
            return
        if chosen is None or not chosen.get_path():
            return
        try:
            Path(chosen.get_path()).write_text(text, encoding="utf-8")
        except OSError as error:
            alert(widget, "Could Not Save the File", str(error))
            return
        on_saved(chosen.get_path())

    dialog.save(widget.get_root(), None, done)


def alert(
    widget: Gtk.Widget,
    heading: str,
    body: str,
    responses: Sequence[tuple[str, str, str]] = (("close", "_Close", ""),),
    on_response: Callable[[str], None] | None = None,
    *,
    default: str = "",
    close: str = "",
    extra: Gtk.Widget | None = None,
) -> Adw.AlertDialog:
    """An Adw.AlertDialog; responses are (id, mnemonic label, "suggested"/"destructive")."""
    dialog = Adw.AlertDialog(heading=heading, body=body)
    for response_id, label, appearance in responses:
        dialog.add_response(response_id, label)
        if appearance == "suggested":
            dialog.set_response_appearance(response_id, Adw.ResponseAppearance.SUGGESTED)
        elif appearance == "destructive":
            dialog.set_response_appearance(response_id, Adw.ResponseAppearance.DESTRUCTIVE)
    first = responses[0][0]
    dialog.set_default_response(default or responses[-1][0])
    dialog.set_close_response(close or first)
    if extra is not None:
        dialog.set_extra_child(extra)
    if on_response is not None:
        dialog.connect("response", lambda _dialog, response: on_response(response))
    dialog.present(widget.get_root() if widget is not None else None)
    return dialog


def clear_children(box: Gtk.Widget) -> None:
    child = box.get_first_child()
    while child is not None:
        following = child.get_next_sibling()
        box.remove(child)
        child = following


class GroupRows:
    """Rows added to an Adw.PreferencesGroup that can be replaced together."""

    def __init__(self, group: Adw.PreferencesGroup) -> None:
        self.group = group
        self.rows: list[Gtk.Widget] = []

    def clear(self) -> None:
        for row in self.rows:
            self.group.remove(row)
        self.rows.clear()

    def add(self, row: Gtk.Widget) -> Gtk.Widget:
        self.group.add(row)
        self.rows.append(row)
        return row


def status_icon(icon_name: str, style: str = "", tooltip: str = "") -> Gtk.Image:
    image = Gtk.Image(icon_name=icon_name, valign=Gtk.Align.CENTER)
    if style:
        image.add_css_class(style)
    if tooltip:
        image.set_tooltip_text(tooltip)
        image.update_property([Gtk.AccessibleProperty.LABEL], [tooltip])
    return image


def progress_bar(text: str = "") -> Gtk.ProgressBar:
    bar = Gtk.ProgressBar(show_text=True, valign=Gtk.Align.CENTER)
    bar.set_text(text or "0%")
    return bar


def set_fraction(bar: Gtk.ProgressBar, fraction: float | None, text: str = "") -> None:
    """Set a progress bar, pulsing when the fraction is unknown."""
    if fraction is None:
        bar.pulse()
        bar.set_text(text or "")
        return
    fraction = max(0.0, min(1.0, fraction))
    bar.set_fraction(fraction)
    bar.set_text(text or f"{fraction * 100:.0f}%")


def is_dark() -> bool:
    return Adw.StyleManager.get_default().get_dark()


# A few styles the stock Adwaita classes do not cover. Colours come from the
# theme's named colours, so they follow light and dark and keep their contrast.
STYLE = """
.virus-card {
  background-color: alpha(@error_color, 0.10);
  border-radius: 12px;
  padding: 12px;
}
.media-frame {
  background-color: alpha(currentColor, 0.07);
  border-radius: 12px;
}
.current-title {
  background-color: alpha(@accent_bg_color, 0.16);
}
.current-title label.title {
  font-weight: bold;
}
.facts {
  padding: 12px 14px;
}
.pager {
  padding: 4px 8px;
}
.pane-resize {
  min-width: 5px;
}
.pane-resize:hover {
  background-color: alpha(currentColor, 0.08);
}
"""
_style_installed = False


def install_style() -> None:
    """Load ``STYLE`` for the default display once."""
    global _style_installed
    display = Gdk.Display.get_default()
    if _style_installed or display is None:
        return
    provider = Gtk.CssProvider()
    provider.load_from_string(STYLE)
    Gtk.StyleContext.add_provider_for_display(
        display, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
    )
    _style_installed = True


def link_markup(text: str, url: str = "") -> str:
    """Escaped Pango markup for ``text``, as a link when there is a ``url``."""
    body = html.escape(text or "", quote=False)
    if not url:
        return body
    return f'<a href="{html.escape(url, quote=True)}">{body}</a>'


def caption_label(text: str = "", *, dim: bool = True, **properties) -> Gtk.Label:
    """Small secondary text. Labels that carry links are never dimmed, so the
    link colour keeps its contrast."""
    properties.setdefault("xalign", 0)
    properties.setdefault("wrap", True)
    label = Gtk.Label(label=text, **properties)
    label.add_css_class("caption")
    if dim:
        label.add_css_class("dim-label")
    return label


def set_accessible_label(widget: Gtk.Widget, text: str) -> None:
    widget.update_property([Gtk.AccessibleProperty.LABEL], [text])


def display_available() -> bool:
    return Gdk.Display.get_default() is not None

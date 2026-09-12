"""The Diagnostic Log dialog and the Keyboard Shortcuts window."""

from __future__ import annotations

from xml.sax.saxutils import escape as xml_escape

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Gtk  # noqa: E402

from .help_content import SHORTCUTS  # noqa: E402
from .log import LOG, DiagnosticLog  # noqa: E402
from .widgets import copy_text, icon_button, save_text_file, text_button  # noqa: E402

EMPTY_TEXT = "Nothing has been logged in this session."


class DiagnosticLogDialog(Adw.Dialog):
    """Recent Greaseweazle output and backend messages, updated while open."""

    def __init__(self, log: DiagnosticLog = LOG) -> None:
        super().__init__(title="Diagnostic Log", content_width=760, content_height=520)
        self._log = log
        self._pending = False
        view = Adw.ToolbarView()
        header = Adw.HeaderBar()
        self.copy_button = icon_button("edit-copy-symbolic", "Copy", self._on_copy, flat=True)
        header.pack_start(self.copy_button)
        self.save_button = icon_button("document-save-symbolic", "Save…", self._on_save, flat=True)
        header.pack_start(self.save_button)
        self.clear_button = text_button("C_lear", self._on_clear, style="flat")
        header.pack_end(self.clear_button)
        view.add_top_bar(header)

        self.text_view = Gtk.TextView(
            editable=False,
            cursor_visible=False,
            monospace=True,
            wrap_mode=Gtk.WrapMode.WORD_CHAR,
            top_margin=12,
            bottom_margin=12,
            left_margin=12,
            right_margin=12,
        )
        self.text_view.update_property([Gtk.AccessibleProperty.LABEL], ["Diagnostic log"])
        scroller = Gtk.ScrolledWindow(vexpand=True)
        scroller.set_child(self.text_view)
        self._scroller = scroller
        view.set_content(scroller)
        note = Gtk.Label(
            label="Kept in memory only. Disk contents are never included.",
            margin_top=6,
            margin_bottom=6,
        )
        note.add_css_class("caption")
        note.add_css_class("dim-label")
        view.add_bottom_bar(note)
        self.set_child(view)
        self._fill()
        log.subscribe(self._changed)
        self.connect("closed", lambda _dialog: log.unsubscribe(self._changed))

    def _fill(self) -> bool:
        self._pending = False
        text = self._log.text()
        self.text_view.get_buffer().set_text(text or EMPTY_TEXT)
        for button in (self.copy_button, self.save_button, self.clear_button):
            button.set_sensitive(bool(text))
        adjustment = self._scroller.get_vadjustment()
        GLib.idle_add(lambda: adjustment.set_value(adjustment.get_upper()) and False)
        return GLib.SOURCE_REMOVE

    def _changed(self) -> None:
        # Called on any thread.
        if not self._pending:
            self._pending = True
            GLib.timeout_add(200, self._fill)

    def _on_copy(self, _button) -> None:
        copy_text(self, self._log.text())

    def _on_save(self, _button) -> None:
        save_text_file(
            self,
            "Save the Diagnostic Log",
            "piratefinder-log.txt",
            self._log.text(),
            lambda _path: None,
        )

    def _on_clear(self, _button) -> None:
        self._log.clear()


def shortcuts_window(parent: Gtk.Window) -> Gtk.ShortcutsWindow:
    """The standard Keyboard Shortcuts window, built from ``help_content.SHORTCUTS``."""
    groups = []
    for title, entries in SHORTCUTS:
        shortcuts = "".join(
            '<child><object class="GtkShortcutsShortcut">'
            f'<property name="accelerator">{xml_escape(accelerator)}</property>'
            f'<property name="title">{xml_escape(text)}</property>'
            "</object></child>"
            for accelerator, text in entries
        )
        groups.append(
            '<child><object class="GtkShortcutsGroup">'
            f'<property name="title">{xml_escape(title)}</property>{shortcuts}</object></child>'
        )
    document = (
        '<interface><object class="GtkShortcutsWindow" id="window">'
        '<property name="modal">1</property>'
        '<child><object class="GtkShortcutsSection">'
        '<property name="section-name">shortcuts</property>'
        f'<property name="max-height">12</property>{"".join(groups)}</object></child>'
        "</object></interface>"
    )
    builder = Gtk.Builder.new_from_string(document, -1)
    window = builder.get_object("window")
    window.set_transient_for(parent)
    return window

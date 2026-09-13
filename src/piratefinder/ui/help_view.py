"""The User Guide window, built from ``help_content``."""

from __future__ import annotations

import html
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gtk, Pango  # noqa: E402

from .help_content import HELP_TOPICS, HelpSection, HelpTopic  # noqa: E402
from .widgets import clear_children  # noqa: E402

HELP_IMAGES = Path(__file__).resolve().parent.parent / "data" / "help"


def help_markup(text: str) -> str:
    """Pango markup for guide text: text between backticks in a monospaced font."""
    parts = text.split("`")
    return "".join(
        f"<tt>{html.escape(part, quote=False)}</tt>"
        if index % 2
        else html.escape(part, quote=False)
        for index, part in enumerate(parts)
    )


def _text(text: str, *, max_width: int = 88, **properties) -> Gtk.Label:
    label = Gtk.Label(
        xalign=0,
        wrap=True,
        wrap_mode=Pango.WrapMode.WORD_CHAR,
        selectable=True,
        max_width_chars=max_width,
        **properties,
    )
    label.set_markup(help_markup(text))
    return label


class HelpView(Gtk.Box):
    """A topic list beside the text of the selected topic."""

    def __init__(self) -> None:
        super().__init__(orientation=Gtk.Orientation.HORIZONTAL, vexpand=True)
        self.topic_list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.SINGLE)
        self.topic_list.add_css_class("navigation-sidebar")
        self.topic_list.update_property([Gtk.AccessibleProperty.LABEL], ["Topics"])
        self.topic_list.connect("row-selected", self._on_selected)
        for index, topic in enumerate(HELP_TOPICS):
            row = Gtk.ListBoxRow()
            row.set_child(
                Gtk.Label(
                    label=topic.title,
                    xalign=0,
                    margin_top=10,
                    margin_bottom=10,
                    margin_start=12,
                    margin_end=12,
                )
            )
            row.topic_index = index
            self.topic_list.append(row)
        navigation = Gtk.ScrolledWindow(width_request=220)
        navigation.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        navigation.set_child(self.topic_list)
        self.append(navigation)
        self.append(Gtk.Separator(orientation=Gtk.Orientation.VERTICAL))

        self.article = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=12,
            margin_top=24,
            margin_bottom=32,
            margin_start=32,
            margin_end=32,
        )
        scroller = Gtk.ScrolledWindow(hexpand=True)
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroller.set_child(Adw.Clamp(maximum_size=760, child=self.article))
        # The text gets its natural height, so each picture is as wide as the
        # text and as tall as that width needs, rather than its smallest size.
        scroller.get_child().set_vscroll_policy(Gtk.ScrollablePolicy.NATURAL)
        self._scroller = scroller
        self.append(scroller)
        self.topic_list.select_row(self.topic_list.get_row_at_index(0))

    def show_topic(self, slug: str) -> None:
        for index, topic in enumerate(HELP_TOPICS):
            if topic.slug == slug:
                self.topic_list.select_row(self.topic_list.get_row_at_index(index))
                return

    def _on_selected(self, _list, row: Gtk.ListBoxRow | None) -> None:
        if row is not None:
            self._render(HELP_TOPICS[row.topic_index])

    def _render(self, topic: HelpTopic) -> None:
        clear_children(self.article)
        title = Gtk.Label(label=topic.title, xalign=0, wrap=True)
        title.add_css_class("title-1")
        self.article.append(title)
        summary = Gtk.Label(label=topic.summary, xalign=0, wrap=True)
        summary.add_css_class("dim-label")
        self.article.append(summary)
        self._picture(topic.screenshot, topic.screenshot_alt)
        for section in topic.sections:
            self._section(section)
        self._scroller.get_vadjustment().set_value(0)

    def _section(self, section: HelpSection) -> None:
        if section.heading:
            heading = Gtk.Label(label=section.heading, xalign=0, wrap=True, margin_top=10)
            heading.add_css_class("title-3")
            self.article.append(heading)
        for paragraph in section.paragraphs:
            self.article.append(_text(paragraph))
        if section.terms:
            self.article.append(self._terms(section))
        if section.bullets:
            self.article.append(self._list(section.bullets, numbered=False))
        if section.steps:
            self.article.append(self._list(section.steps, numbered=True))
        self._picture(section.screenshot, section.screenshot_alt)

    @staticmethod
    def _terms(section: HelpSection) -> Gtk.Widget:
        grid = Gtk.Grid(column_spacing=18, row_spacing=8, margin_top=2, margin_bottom=2)
        row = 0
        if any(section.columns):
            for column, text in enumerate(section.columns):
                label = Gtk.Label(label=text, xalign=0)
                label.add_css_class("caption-heading")
                label.add_css_class("dim-label")
                grid.attach(label, column, row, 1, 1)
            row += 1
        for term, text in section.terms:
            key = Gtk.Label(
                xalign=0,
                valign=Gtk.Align.START,
                wrap=True,
                wrap_mode=Pango.WrapMode.WORD_CHAR,
                max_width_chars=24,
                selectable=True,
            )
            key.set_markup(f"<b>{help_markup(term)}</b>")
            grid.attach(key, 0, row, 1, 1)
            grid.attach(
                _text(text, max_width=60, hexpand=True, valign=Gtk.Align.START), 1, row, 1, 1
            )
            row += 1
        return grid

    @staticmethod
    def _list(items: tuple[str, ...], *, numbered: bool) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        for number, text in enumerate(items, start=1):
            line = Gtk.Box(spacing=10)
            marker = Gtk.Label(
                label=str(number) if numbered else "\N{BULLET}",
                valign=Gtk.Align.START,
                width_chars=2,
            )
            if numbered:
                marker.add_css_class("heading")
            line.append(marker)
            line.append(_text(text, max_width=82, hexpand=True))
            box.append(line)
        return box

    def _picture(self, name: str, alternative: str) -> None:
        image = HELP_IMAGES / name if name else None
        if image is None or not image.is_file():
            return
        picture = Gtk.Picture.new_for_filename(str(image))
        picture.set_alternative_text(alternative)
        picture.set_tooltip_text(alternative)
        picture.set_content_fit(Gtk.ContentFit.CONTAIN)
        picture.set_can_shrink(True)
        picture.set_size_request(-1, 200)
        self.article.append(Gtk.Frame(child=picture, margin_top=6))
        caption = Gtk.Label(label=alternative, xalign=0, wrap=True)
        caption.add_css_class("caption")
        caption.add_css_class("dim-label")
        self.article.append(caption)


class HelpWindow(Adw.Window):
    """The User Guide in a window of its own, so it can stay open while working."""

    def __init__(self, parent: Gtk.Window) -> None:
        super().__init__(title="User Guide", default_width=1000, default_height=720)
        self.set_transient_for(parent)
        self.set_hide_on_close(True)
        view = Adw.ToolbarView()
        header = Adw.HeaderBar()
        header.set_title_widget(Adw.WindowTitle(title="User Guide", subtitle="PirateFinder"))
        view.add_top_bar(header)
        self.help_view = HelpView()
        view.set_content(self.help_view)
        self.set_content(view)
        self.set_size_request(360, 360)

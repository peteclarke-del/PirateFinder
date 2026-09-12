"""The User Guide window, built from ``help_content``."""

from __future__ import annotations

from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gtk  # noqa: E402

from .help_content import HELP_TOPICS, HelpTopic  # noqa: E402
from .widgets import clear_children  # noqa: E402

HELP_IMAGES = Path(__file__).resolve().parent.parent / "data" / "help"


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

        image = HELP_IMAGES / topic.screenshot if topic.screenshot else None
        if image is not None and image.is_file():
            picture = Gtk.Picture.new_for_filename(str(image))
            picture.set_alternative_text(topic.screenshot_alt)
            picture.set_tooltip_text(topic.screenshot_alt)
            picture.set_content_fit(Gtk.ContentFit.CONTAIN)
            picture.set_can_shrink(True)
            picture.set_size_request(-1, 300)
            frame = Gtk.Frame(child=picture, margin_top=6)
            self.article.append(frame)

        for section in topic.sections:
            heading = Gtk.Label(label=section.heading, xalign=0, wrap=True, margin_top=10)
            heading.add_css_class("title-3")
            self.article.append(heading)
            for paragraph in section.paragraphs:
                self.article.append(
                    Gtk.Label(
                        label=paragraph, xalign=0, wrap=True, selectable=True, max_width_chars=88
                    )
                )
            if section.steps:
                steps = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
                for number, text in enumerate(section.steps, start=1):
                    line = Gtk.Box(spacing=10)
                    marker = Gtk.Label(label=str(number), valign=Gtk.Align.START, width_chars=2)
                    marker.add_css_class("heading")
                    line.append(marker)
                    line.append(
                        Gtk.Label(
                            label=text,
                            xalign=0,
                            wrap=True,
                            hexpand=True,
                            selectable=True,
                            max_width_chars=82,
                        )
                    )
                    steps.append(line)
                self.article.append(steps)
        self._scroller.get_vadjustment().set_value(0)


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

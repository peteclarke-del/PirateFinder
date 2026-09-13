"""Shared widgets, built for real; skipped without a display."""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
import unittest
from pathlib import Path

from tests.gtk_support import require_pygobject  # noqa: E402

require_pygobject()

# The application module drops a Snap terminal's library paths before GTK
# starts; without that, GTK loads the snap's modules and can crash later.
import piratefinder.ui.application  # noqa: E402, F401
from piratefinder.ui.widgets import display_available  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]

# GTK reports markup errors through structured logging, which a Python log
# handler does not see, so the rows are built in a child process and its
# standard error is read instead.
_BUILD_ROWS = textwrap.dedent(
    """
    import gi
    gi.require_version("Gtk", "4.0")
    gi.require_version("Adw", "1")
    from gi.repository import Adw
    from piratefinder.ui.widgets import plain_row

    Adw.init()
    title = "Sooty & Sweep <Menu>"
    subtitle = "https://d-bug.me/newsearch.php?cgroup=Automation&menu=250"
    for row_type in (Adw.ActionRow, Adw.ExpanderRow, Adw.SwitchRow):
        row = plain_row(row_type, title=title, subtitle=subtitle)
        assert not row.get_use_markup()
        assert (row.get_title(), row.get_subtitle()) == (title, subtitle)
    print("built")
    """
)


@unittest.skipUnless(display_available(), "needs a display")
class PlainRowTests(unittest.TestCase):
    def test_catalogue_text_is_never_parsed_as_markup(self) -> None:
        environment = dict(os.environ, PYTHONPATH=f"{ROOT / 'src'}{os.pathsep}{ROOT}")
        result = subprocess.run(
            [sys.executable, "-c", _BUILD_ROWS],
            capture_output=True,
            text=True,
            env=environment,
            timeout=60,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("built", result.stdout)
        self.assertNotIn("from markup", result.stderr, "GTK parsed catalogue text as markup")


class HelpMarkupTests(unittest.TestCase):
    """The guide's text becomes Pango markup that parses, with code in monospace."""

    def test_code_spans_are_monospace_and_the_rest_is_escaped(self) -> None:
        from piratefinder.ui.help_view import help_markup

        self.assertEqual(help_markup("Run `gw info` & see"), "Run <tt>gw info</tt> &amp; see")

    def test_every_text_of_the_guide_parses(self) -> None:
        import gi

        gi.require_version("Pango", "1.0")
        from gi.repository import Pango

        from piratefinder.ui.help_content import HELP_TOPICS
        from piratefinder.ui.help_view import help_markup

        for topic in HELP_TOPICS:
            for section in topic.sections:
                texts = [*section.paragraphs, *section.steps, *section.bullets]
                texts += [text for pair in section.terms for text in pair]
                for text in texts:
                    with self.subTest(topic=topic.slug, text=text[:40]):
                        Pango.parse_markup(f"<b>{help_markup(text)}</b>", -1, "\0")


# Every topic of the User Guide is drawn in a child process, for the same
# reason as above: a markup error would only show on standard error.
_SHOW_TOPICS = textwrap.dedent(
    """
    import gi
    gi.require_version("Gtk", "4.0")
    gi.require_version("Adw", "1")
    from gi.repository import Adw
    from piratefinder.ui.help_content import HELP_TOPICS
    from piratefinder.ui.help_view import HelpView

    Adw.init()
    view = HelpView()
    for topic in HELP_TOPICS:
        view.show_topic(topic.slug)
        children = 0
        child = view.article.get_first_child()
        while child is not None:
            children += 1
            child = child.get_next_sibling()
        assert children > 3, topic.slug
    print("shown")
    """
)


@unittest.skipUnless(display_available(), "needs a display")
class HelpViewTests(unittest.TestCase):
    def test_every_topic_is_drawn_without_markup_errors(self) -> None:
        environment = dict(os.environ, PYTHONPATH=f"{ROOT / 'src'}{os.pathsep}{ROOT}")
        result = subprocess.run(
            [sys.executable, "-c", _SHOW_TOPICS],
            capture_output=True,
            text=True,
            env=environment,
            timeout=60,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("shown", result.stdout)
        self.assertNotIn("markup", result.stderr.lower(), result.stderr)


if __name__ == "__main__":
    unittest.main()

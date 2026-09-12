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


if __name__ == "__main__":
    unittest.main()

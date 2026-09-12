"""Skip the GTK test modules where PyGObject is missing, except where it is required.

PyGObject comes from the distribution, not from pip, so a plain virtual
environment has no ``gi``. CI installs the distribution's GTK stack and sets
``PIRATEFINDER_REQUIRE_GTK`` so that a broken install fails the run instead of
skipping the window tests without anyone noticing.
"""

from __future__ import annotations

import os
import unittest


def require_pygobject() -> None:
    try:
        import gi  # noqa: F401
    except ImportError as error:
        if os.environ.get("PIRATEFINDER_REQUIRE_GTK"):
            raise
        raise unittest.SkipTest(f"PyGObject is not installed: {error}") from error

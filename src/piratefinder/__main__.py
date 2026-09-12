"""Command-line entry point for the desktop application."""

from __future__ import annotations

import sys


def main() -> int:
    """Run the GTK application."""
    from .ui.application import PirateFinderApplication

    return PirateFinderApplication().run(sys.argv)


if __name__ == "__main__":
    raise SystemExit(main())

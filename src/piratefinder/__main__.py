"""Command-line entry point for the desktop application."""

from __future__ import annotations

import os
import sys


def restart_command(argv: list[str]) -> list[str]:
    """The command that starts PirateFinder again, as ``python3 -m piratefinder``."""
    return [sys.executable, "-m", "piratefinder", *argv[1:]]


def main() -> int:
    """Run the GTK application, and start it again after an update when asked."""
    from .ui.application import PirateFinderApplication

    application = PirateFinderApplication()
    status = application.run(sys.argv)
    if application.restart_requested:
        # The process is replaced, so the new version loads every module afresh.
        # The environment, with the launcher's PYTHONPATH and PATH, is kept.
        command = restart_command(sys.argv)
        os.execv(command[0], command)
    return status


if __name__ == "__main__":
    raise SystemExit(main())

"""Where PirateFinder keeps its files, following the XDG base directories."""

from __future__ import annotations

import os
from pathlib import Path

APP_DIR_NAME = "piratefinder"
CATALOGUE_FILE = "catalogue.sqlite"
SYSTEM_CATALOGUE = Path("/usr/share/piratefinder") / CATALOGUE_FILE
SOURCE_TREE_CATALOGUE = Path(__file__).resolve().parents[2] / "build" / CATALOGUE_FILE


def _xdg(variable: str, fallback: str) -> Path:
    value = os.environ.get(variable, "")
    base = Path(value) if value and Path(value).is_absolute() else Path.home() / fallback
    return base / APP_DIR_NAME


def config_dir() -> Path:
    return _xdg("XDG_CONFIG_HOME", ".config")


def data_dir() -> Path:
    return _xdg("XDG_DATA_HOME", ".local/share")


def cache_dir() -> Path:
    return _xdg("XDG_CACHE_HOME", ".cache")


def settings_path() -> Path:
    return config_dir() / "settings.json"


def user_database_path() -> Path:
    return data_dir() / "user.sqlite"


def updated_catalogue_path() -> Path:
    """A newer catalogue downloaded by the application itself."""
    return data_dir() / CATALOGUE_FILE


def default_download_folder() -> Path:
    return Path.home() / "Floppy Images" / "PirateFinder"


def catalogue_candidates() -> list[Path]:
    """Catalogue files in the order they are preferred.

    An explicit ``PIRATEFINDER_CATALOGUE`` wins. Otherwise the newest of an
    in-application update, the packaged copy and a source-tree build is used;
    see ``piratefinder.catalogue.store.locate_catalogue``.
    """
    candidates: list[Path] = []
    explicit = os.environ.get("PIRATEFINDER_CATALOGUE", "")
    if explicit:
        candidates.append(Path(explicit))
    candidates += [updated_catalogue_path(), SYSTEM_CATALOGUE, SOURCE_TREE_CATALOGUE]
    return candidates

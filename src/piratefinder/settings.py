"""User settings, kept as JSON under ``~/.config/piratefinder``.

The file is written atomically with mode 0600. Loading never fails: missing
keys take their defaults, values of the wrong type are ignored, keys written by
a newer version are kept and written back, and a file that cannot be parsed is
copied to ``settings.json.bak`` before the defaults are used.
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import tempfile
from collections.abc import Iterable
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any

from . import paths
from .branding import RELEASES_API

DEFAULT_FEED_URL = RELEASES_API
FILE_MODE = 0o600


def _default_download_folder() -> str:
    return str(paths.default_download_folder())


@dataclass
class Settings:
    """Everything the user can change in Preferences."""

    library_folders: list[str] = field(default_factory=list)
    download_folder: str = field(default_factory=_default_download_folder)
    online_enabled: bool = True
    providers: dict[str, bool] = field(default_factory=dict)  # missing ids count as enabled
    drive: str = "A"
    device: str = ""
    retries: int = 3
    pre_erase: bool = False
    check_catalogue_updates: bool = True
    catalogue_feed_url: str = DEFAULT_FEED_URL
    prompt_between_disks: bool = True
    # "Download screenshots and background information": pictures and
    # Wikipedia summaries for the details pane, fetched only when shown.
    fetch_media: bool = True
    path: Path | None = field(default=None, repr=False, compare=False)
    extra: dict[str, Any] = field(default_factory=dict, repr=False, compare=False)

    @classmethod
    def load(cls, path: Path | None = None) -> Settings:
        """Read settings from ``path`` (default ``paths.settings_path()``), never failing."""
        target = Path(path) if path is not None else paths.settings_path()
        settings = cls(path=target)
        try:
            text = target.read_text(encoding="utf-8")
        except OSError:
            return settings
        try:
            data = json.loads(text)
            if not isinstance(data, dict):
                raise ValueError("settings file is not a JSON object")
        except ValueError:
            _keep_backup(target)
            return settings
        settings._apply(data)
        return settings

    def save(self, path: Path | None = None) -> Path:
        """Write the settings atomically with mode 0600 and return the file written."""
        target = Path(path) if path is not None else (self.path or paths.settings_path())
        self.path = target
        data = dict(self.extra)
        data.update(self.to_dict())
        text = json.dumps(data, indent=2, sort_keys=True) + "\n"
        _atomic_write(target, text)
        return target

    def to_dict(self) -> dict[str, Any]:
        """The saved settings as a plain dictionary."""
        return {
            item.name: _copy(getattr(self, item.name))
            for item in fields(self)
            if item.name not in ("path", "extra")
        }

    def provider_enabled(self, provider: str) -> bool:
        """Whether one provider may be used, ignoring the global online switch."""
        return bool(self.providers.get(provider, True))

    def enabled_providers(self, all_ids: Iterable[str]) -> list[str]:
        """The ids from ``all_ids`` that may be used; empty when online use is off."""
        if not self.online_enabled:
            return []
        return [provider for provider in all_ids if self.provider_enabled(provider)]

    def _apply(self, data: dict[str, Any]) -> None:
        known = {item.name for item in fields(self)} - {"path", "extra"}
        defaults = Settings()
        for key, value in data.items():
            if key not in known:
                self.extra[key] = value
                continue
            cleaned = _clean(key, value, getattr(defaults, key))
            if cleaned is not None:
                setattr(self, key, cleaned)


def _clean(key: str, value: Any, default: Any) -> Any:
    """``value`` when it has the type of ``default``, otherwise None."""
    if key == "library_folders":
        if isinstance(value, list):
            return [item for item in value if isinstance(item, str) and item]
        return None
    if key == "providers":
        if isinstance(value, dict):
            return {
                str(name): enabled for name, enabled in value.items() if isinstance(enabled, bool)
            }
        return None
    if isinstance(default, bool):
        return value if isinstance(value, bool) else None
    if isinstance(default, int):
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            return value
        return None
    if isinstance(default, str):
        return value if isinstance(value, str) else None
    return None


def _copy(value: Any) -> Any:
    if isinstance(value, list):
        return list(value)
    if isinstance(value, dict):
        return dict(value)
    return value


def _keep_backup(target: Path) -> None:
    """Copy an unreadable settings file aside so the user can recover it."""
    with contextlib.suppress(OSError):
        backup = target.with_name(target.name + ".bak")
        shutil.copyfile(target, backup)
        os.chmod(backup, FILE_MODE)


def _atomic_write(target: Path, text: str) -> None:
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    handle, temporary = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
    try:
        os.fchmod(handle, FILE_MODE)
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(temporary)
        raise

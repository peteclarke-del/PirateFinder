from __future__ import annotations

import json
import os
import shutil
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from piratefinder import paths
from piratefinder.settings import DEFAULT_FEED_URL, Settings


class SettingsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.folder = Path(tempfile.mkdtemp(prefix="pf-settings-"))
        self.addCleanup(shutil.rmtree, self.folder, True)
        self.path = self.folder / "config" / "settings.json"

    def test_defaults_when_the_file_is_missing(self) -> None:
        settings = Settings.load(self.path)
        self.assertEqual(settings.library_folders, [])
        self.assertEqual(settings.download_folder, str(paths.default_download_folder()))
        self.assertTrue(settings.online_enabled)
        self.assertEqual(settings.drive, "A")
        self.assertEqual(settings.retries, 3)
        self.assertFalse(settings.pre_erase)
        self.assertTrue(settings.check_catalogue_updates)
        self.assertEqual(settings.catalogue_feed_url, DEFAULT_FEED_URL)
        self.assertTrue(settings.prompt_between_disks)
        self.assertFalse(self.path.exists())

    def test_default_path_follows_xdg_config_home(self) -> None:
        with mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": str(self.folder)}):
            settings = Settings.load()
            settings.drive = "B"
            written = settings.save()
        self.assertEqual(written, self.folder / "piratefinder" / "settings.json")
        self.assertEqual(Settings.load(written).drive, "B")

    def test_round_trip_with_private_mode(self) -> None:
        settings = Settings.load(self.path)
        settings.library_folders = ["/nas/st", "/nas/amiga"]
        settings.providers = {"internet-archive": False}
        settings.retries = 5
        settings.online_enabled = False
        settings.save()
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
        loaded = Settings.load(self.path)
        self.assertEqual(loaded, settings)
        leftovers = [name for name in os.listdir(self.path.parent) if name != "settings.json"]
        self.assertEqual(leftovers, [])

    def test_wrong_types_fall_back_and_unknown_keys_survive(self) -> None:
        self.path.parent.mkdir(parents=True)
        self.path.write_text(
            json.dumps(
                {
                    "retries": "many",
                    "drive": 7,
                    "library_folders": ["/ok", 3, ""],
                    "providers": {"a": True, "b": "no"},
                    "online_enabled": 1,
                    "future_option": {"x": 1},
                }
            )
        )
        settings = Settings.load(self.path)
        self.assertEqual(settings.retries, 3)
        self.assertEqual(settings.drive, "A")
        self.assertEqual(settings.library_folders, ["/ok"])
        self.assertEqual(settings.providers, {"a": True})
        self.assertTrue(settings.online_enabled)
        settings.save()
        self.assertEqual(json.loads(self.path.read_text())["future_option"], {"x": 1})

    def test_corrupt_file_is_kept_as_backup(self) -> None:
        self.path.parent.mkdir(parents=True)
        self.path.write_text("{not json")
        settings = Settings.load(self.path)
        self.assertEqual(settings.retries, 3)
        backup = self.path.with_name("settings.json.bak")
        self.assertEqual(backup.read_text(), "{not json")
        settings.save()
        self.assertEqual(json.loads(self.path.read_text())["drive"], "A")
        self.assertEqual(backup.read_text(), "{not json")

    def test_json_that_is_not_an_object_is_treated_as_corrupt(self) -> None:
        self.path.parent.mkdir(parents=True)
        self.path.write_text("[1, 2]")
        Settings.load(self.path)
        self.assertTrue(self.path.with_name("settings.json.bak").exists())

    def test_enabled_providers(self) -> None:
        settings = Settings(providers={"d-bug": False, "atari-legend": True})
        ids = ["atari-legend", "d-bug", "internet-archive"]
        self.assertEqual(settings.enabled_providers(ids), ["atari-legend", "internet-archive"])
        settings.online_enabled = False
        self.assertEqual(settings.enabled_providers(ids), [])

    def test_failed_write_leaves_the_old_file(self) -> None:
        settings = Settings.load(self.path)
        settings.save()
        before = self.path.read_text()
        settings.drive = "B"
        with mock.patch("os.replace", side_effect=OSError("disk full")), self.assertRaises(OSError):
            settings.save()
        self.assertEqual(self.path.read_text(), before)
        self.assertEqual(sorted(os.listdir(self.path.parent)), ["settings.json"])


if __name__ == "__main__":
    unittest.main()

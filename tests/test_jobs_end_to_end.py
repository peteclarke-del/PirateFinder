"""Scan, match, search and write with the real modules and a fake gw command.

The fake gw is a small Python script that prints the output of a successful
``gw write`` and records its arguments. Images and the catalogue are
synthetic; the download test serves a zip from a local HTTP server.
"""

from __future__ import annotations

import functools
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from piratefinder.catalogue.store import Catalogue
from piratefinder.finder import Finder
from piratefinder.jobs.history import History
from piratefinder.jobs.queue import item_from_result
from piratefinder.jobs.session import QuietEvents, WriteSession
from piratefinder.library.library import Library
from piratefinder.library.userdb import UserDatabase
from piratefinder.models import Availability, SearchFilters, WriteStatus
from piratefinder.settings import Settings
from tests.test_library_helpers import CatalogueBuilder, QuietHandler, hashes, make_st_image, serve

FAKE_GW = """\
#!{python}
import json, sys
with open({log!r}, "a") as log:
    log.write(json.dumps(sys.argv[1:]) + "\\n")
print("Writing c=0-79:h=0-1")
for cylinder in range(80):
    for head in range(2):
        print(f"T{{cylinder}}.{{head}}: Writing Track")
print("All tracks verified")
"""


class Events(QuietEvents):
    def __init__(self) -> None:
        self.stages: list[str] = []
        self.prompts = 0
        self.progress = 0

    def on_stage(self, item, index, total, stage, message) -> None:
        self.stages.append(stage)

    def on_write_progress(self, item, progress) -> None:
        self.progress += 1

    def ask_insert(self, item, index, total, reason="") -> str:
        self.prompts += 1
        return "write"


class EndToEndTests(unittest.TestCase):
    def setUp(self) -> None:
        self.folder = Path(tempfile.mkdtemp(prefix="pf-e2e-"))
        self.addCleanup(shutil.rmtree, self.folder, True)
        self.log = self.folder / "gw.log"
        gw = self.folder / "gw"
        gw.write_text(FAKE_GW.format(python=sys.executable, log=str(self.log)))
        gw.chmod(0o755)
        environment = {
            "PIRATEFINDER_GW": str(gw),
            "XDG_CACHE_HOME": str(self.folder / "cache"),
            "XDG_DATA_HOME": str(self.folder / "data"),
            "XDG_CONFIG_HOME": str(self.folder / "config"),
        }
        for patcher in (
            mock.patch.dict(os.environ, environment),
            mock.patch.object(tempfile, "tempdir", str(self.scratch())),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.raw = make_st_image("end to end", files=("ZANYGOLF.PRG",))
        self.library_folder = self.folder / "library"
        self.library_folder.mkdir()
        self.settings = Settings(
            library_folders=[str(self.library_folder)],
            download_folder=str(self.folder / "Floppy Images"),
        )
        self.settings.save(self.folder / "config" / "settings.json")

    def scratch(self) -> Path:
        folder = self.folder / "tmp"
        folder.mkdir(exist_ok=True)
        return folder

    def open_world(self, location_url: str = "") -> tuple[Catalogue, Library, Finder]:
        builder = CatalogueBuilder(self.folder / "catalogue.sqlite")
        builder.disk(
            1, "Crew 1", series=("crew", "Crew"), number=1, contents=["Zany Golf", "Other"]
        )
        builder.image(10, 1, "Crew 1 (1990)(Crew).st", data=self.raw, hash_kinds=("md5",))
        if location_url:
            builder.location(
                1,
                1,
                "test-archive",
                location_url,
                image_id=10,
                container="zip",
                member="Crew 1 (1990)(Crew).st",
                hash_kind="md5",
                hash_value=hashes(self.raw)["md5"],
            )
        catalogue = Catalogue.open(builder.close())
        self.addCleanup(catalogue.close)
        userdb = UserDatabase.open(self.folder / "data" / "piratefinder" / "user.sqlite")
        self.addCleanup(userdb.close)
        library = Library(userdb, catalogue)
        return catalogue, library, Finder(catalogue, library, self.settings)

    def written_arguments(self) -> list[list[str]]:
        return [json.loads(line) for line in self.log.read_text().splitlines()]

    def test_local_image_is_found_and_written(self) -> None:
        (self.library_folder / "menu.st").write_bytes(self.raw)
        _catalogue, library, finder = self.open_world()
        scan = library.scan(self.settings.library_folders)
        self.assertEqual((scan.matched, scan.errors), (1, ()))

        results = finder.search("zany golf", SearchFilters())
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].availability, Availability.LOCAL)
        self.assertEqual(results[0].disk.label, "Crew 1")

        events = Events()
        item = item_from_result(results[0])
        summary = WriteSession(finder, library, self.settings, [item], events).run()

        label, outcome, source = summary.items[0]
        self.assertEqual(outcome.status, WriteStatus.VERIFIED, outcome.diagnostic)
        self.assertEqual(source, str(self.library_folder / "menu.st"))
        self.assertEqual(events.prompts, 1)
        self.assertEqual(events.progress, 161)
        arguments = self.written_arguments()[0]
        self.assertEqual(arguments[0], "write")
        self.assertIn("--drive=A", arguments)
        self.assertIn("--retries=3", arguments)
        self.assertEqual(History(library.userdb).sessions(), [summary])
        self.assertEqual(list(self.scratch().iterdir()), [], "temporary folders are removed")

    def test_image_is_downloaded_checked_saved_and_written(self) -> None:
        site = self.folder / "site"
        site.mkdir()
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("Crew 1 (1990)(Crew).st", self.raw)
            archive.writestr("readme.txt", "synthetic test set")
        (site / "crew-set.zip").write_bytes(buffer.getvalue())
        with serve(functools.partial(QuietHandler, directory=str(site))) as base:
            _catalogue, library, finder = self.open_world(f"{base}/crew-set.zip")
            library.scan(self.settings.library_folders)
            results = finder.search("zany", SearchFilters())
            self.assertEqual(results[0].availability, Availability.ONLINE)

            events = Events()
            summary = WriteSession(
                finder, library, self.settings, [item_from_result(results[0])], events
            ).run()

        outcome = summary.items[0][1]
        self.assertEqual(outcome.status, WriteStatus.VERIFIED, outcome.diagnostic)
        self.assertIn("download", events.stages)
        saved = (
            Path(self.settings.download_folder)
            / "Atari ST"
            / "Games"
            / "Crew"
            / "Crew 1 (1990)(Crew).st"
        )
        self.assertEqual(saved.read_bytes(), self.raw)
        self.assertEqual([f.path for f in library.files_for_disk(1)], [str(saved)])
        self.assertEqual(finder.search("zany", SearchFilters())[0].availability, Availability.LOCAL)
        cache = self.folder / "cache" / "piratefinder" / "downloads"
        self.assertEqual(list(cache.rglob("*")) if cache.exists() else [], [])


if __name__ == "__main__":
    unittest.main()

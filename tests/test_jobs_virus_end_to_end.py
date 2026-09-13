"""A boot block virus from scan to floppy, with the real modules and a fake gw.

A synthetic ADF carries a synthetic virus boot block that the test brainfile
names. The library scan finds it, the details pane reports it, the session
writes a cleaned copy while the stored file stays as it was, and cleaning the
stored file keeps a backup. The fake gw copies each image it is asked to
write so the test can look at what reached the floppy.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from unittest import mock

from piratefinder.catalogue.store import Catalogue
from piratefinder.finder import Finder
from piratefinder.images import virus
from piratefinder.jobs.queue import item_from_disk
from piratefinder.jobs.session import QuietEvents, WriteSession
from piratefinder.library.library import Library
from piratefinder.library.userdb import UserDatabase
from piratefinder.models import Platform, VirusStatus, WriteStatus
from piratefinder.settings import Settings
from tests.test_images_virus import VirusEnvironment, amiga_disk, crc_virus
from tests.test_library_helpers import CatalogueBuilder

FAKE_GW = """\
#!{python}
import json, shutil, sys
from pathlib import Path
log = Path({log!r})
count = len(log.read_text().splitlines()) if log.exists() else 0
with log.open("a") as stream:
    stream.write(json.dumps(sys.argv[1:]) + "\\n")
shutil.copyfile(sys.argv[-1], Path({captured!r}) / f"written-{{count}}.img")
print("Writing c=0-79:h=0-1")
for cylinder in range(80):
    for head in range(2):
        print(f"T{{cylinder}}.{{head}}: Writing Track")
print("All tracks verified")
"""


class VirusEndToEndTests(VirusEnvironment, unittest.TestCase):
    def setUp(self) -> None:
        self.folder = self.use_environment(brainfile=True)
        self.captured = self.folder / "captured"
        self.captured.mkdir()
        self.log = self.folder / "gw.log"
        gw = self.folder / "gw"
        gw.write_text(
            FAKE_GW.format(python=sys.executable, log=str(self.log), captured=str(self.captured))
        )
        gw.chmod(0o755)
        scratch = self.folder / "tmp"
        scratch.mkdir()
        environment = {
            "PIRATEFINDER_GW": str(gw),
            "XDG_CACHE_HOME": str(self.folder / "cache"),
            "XDG_CONFIG_HOME": str(self.folder / "config"),
        }
        for patcher in (
            mock.patch.dict(os.environ, environment),
            mock.patch.object(tempfile, "tempdir", str(scratch)),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.infected = amiga_disk(crc_virus())
        library_folder = self.folder / "library"
        library_folder.mkdir()
        self.stored = library_folder / "crew1.adf"
        self.stored.write_bytes(self.infected)
        self.settings = Settings(
            library_folders=[str(library_folder)],
            download_folder=str(self.folder / "Floppy Images"),
        )
        builder = CatalogueBuilder(self.folder / "catalogue.sqlite")
        builder.disk(1, "Crew 1", platform=Platform.AMIGA, series=("crew", "Crew"), number=1)
        builder.image(10, 1, "Crew 1 (1990)(Crew).adf", data=self.infected)
        self.catalogue = Catalogue.open(builder.close())
        self.addCleanup(self.catalogue.close)
        userdb = UserDatabase.open(self.folder / "user.sqlite")
        self.addCleanup(userdb.close)
        self.library = Library(userdb, self.catalogue)
        self.finder = Finder(self.catalogue, self.library, self.settings)

    def written(self) -> list[bytes]:
        return [path.read_bytes() for path in sorted(self.captured.iterdir())]

    def test_found_reported_cleaned_when_writing_and_cleaned_in_place(self) -> None:
        summary = self.library.scan(self.settings.library_folders)
        self.assertEqual((summary.matched, summary.errors), (1, ()))
        local = self.library.files_for_disk(1)[0]
        self.assertEqual(local.virus, "Synthetic CRC")

        detail = self.finder.detail(1)
        report = detail.virus
        self.assertEqual(
            (report.status, report.name, report.removable, report.source),
            (VirusStatus.VIRUS, "Synthetic CRC", True, "Amiga Bootblock Reader"),
        )

        item = item_from_disk(detail.disk)
        self.assertTrue(item.clean_virus, "removal before writing is the default")
        outcome = WriteSession(self.finder, self.library, self.settings, [item], QuietEvents())
        result = outcome.run().items[0][1]
        self.assertEqual(result.status, WriteStatus.VERIFIED, result.diagnostic)
        written = self.written()[0]
        self.assertEqual(written[:1024], virus.standard_boot_block(0))
        self.assertEqual(written[1024:], self.infected[1024:])
        self.assertEqual(self.stored.read_bytes(), self.infected, "the stored image is unchanged")
        self.assertIn(
            "Removed the Synthetic CRC boot block virus before writing; the stored image is "
            "unchanged.",
            item.notes,
        )
        self.assertIn("--format=amiga.amigados", json.loads(self.log.read_text().splitlines()[0]))

        keep = item_from_disk(detail.disk)
        keep.clean_virus = False
        WriteSession(self.finder, self.library, self.settings, [keep], QuietEvents()).run()
        self.assertEqual(self.written()[1], self.infected)

        cleaned = self.finder.clean_file(local)
        self.assertEqual(cleaned.path, str(self.stored))
        self.assertEqual(self.stored.read_bytes(), virus.clean(self.infected, Platform.AMIGA))
        backup = self.stored.with_name("crew1.adf.bak")
        self.assertEqual(backup.read_bytes(), self.infected)
        self.assertEqual((cleaned.disk_id, cleaned.virus), (1, ""))
        after = self.finder.detail(1)
        self.assertEqual([f.path for f in after.local_files], [str(self.stored)])
        self.assertEqual(after.virus.status, VirusStatus.CLEAN)
        # A new scan leaves the cleaned copy with its disk and does not index the backup.
        self.library.scan(self.settings.library_folders)
        self.assertEqual(self.library.files_for_disk(1), [cleaned])


if __name__ == "__main__":
    unittest.main()

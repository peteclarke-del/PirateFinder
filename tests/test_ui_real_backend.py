"""The interface's adapter over the real library, queue, history and session.

These tests use the application's own modules with a user database in a
temporary folder and no catalogue, which is how PirateFinder starts before a
catalogue has been downloaded. No display is needed.
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path

_HOME = Path(tempfile.mkdtemp(prefix="piratefinder-ui-real-"))
for _variable in ("XDG_CONFIG_HOME", "XDG_DATA_HOME"):
    os.environ[_variable] = str(_HOME / _variable.lower())

from piratefinder.models import (  # noqa: E402
    DiskKind,
    Platform,
    SearchFilters,
    SessionSummary,
    WriteOutcome,
    WriteStatus,
)
from piratefinder.settings import Settings  # noqa: E402
from piratefinder.ui import formatting as fmt  # noqa: E402

try:
    from piratefinder.library.userdb import UserDatabase
    from piratefinder.ui.real_backend import RealBackend
except ImportError as error:  # the backend modules are not all there yet
    MISSING = str(error)
else:
    MISSING = ""


@unittest.skipIf(MISSING, f"backend modules missing: {MISSING}")
class RealBackendWithoutCatalogueTests(unittest.TestCase):
    def setUp(self) -> None:
        self.folder = Path(tempfile.mkdtemp(dir=_HOME))
        self.settings = Settings(path=self.folder / "settings.json")
        self.settings.download_folder = str(self.folder / "downloads")
        self.userdb = UserDatabase.open(self.folder / "user.sqlite")
        self.backend = RealBackend(self.settings, self.userdb, None, "No catalogue here.")
        self.backend.queue.clear()

    def tearDown(self) -> None:
        self.backend.close()

    def test_catalogue_is_reported_missing_with_the_reason(self) -> None:
        info = self.backend.catalogue_info()
        self.assertFalse(info.available)
        self.assertEqual(info.error, "No catalogue here.")
        self.assertEqual(self.backend.search("anything", SearchFilters()), [])
        with self.assertRaises(LookupError):
            self.backend.detail(1)

    def test_queue_calls_reach_the_write_queue(self) -> None:
        first = fmt.new_queue_item("Automation 250", Platform.ATARI_ST, disk_id=1)
        second = fmt.new_queue_item("Automation 251", Platform.ATARI_ST, disk_id=2)
        self.backend.queue_add([first, second, fmt.new_queue_item("again", None, disk_id=1)])
        self.assertEqual([item.id for item in self.backend.queue_items()], [first.id, second.id])
        self.backend.queue_move(second.id, -1)
        self.backend.queue_set_copies(first.id, 3)
        self.assertEqual([item.id for item in self.backend.queue_items()], [second.id, first.id])
        self.assertEqual(self.backend.queue.get(first.id).copies, 3)
        self.backend.queue_remove([second.id])
        self.assertEqual(len(self.backend.queue_items()), 1)
        self.backend.queue_clear()
        self.assertEqual(self.backend.queue_items(), [])

    def test_scanning_an_empty_folder_and_the_library_counts(self) -> None:
        images = self.folder / "images"
        images.mkdir()
        self.settings.library_folders = [str(images)]
        seen = []
        summary = self.backend.scan_library(seen.append, None)
        self.assertEqual(summary.images_found, 0)
        self.assertFalse(summary.cancelled)
        stats = self.backend.library_stats()
        self.assertEqual((stats.images, stats.matched, stats.unmatched), (0, 0, 0))
        self.assertEqual(self.backend.unmatched_files(), [])
        self.assertTrue(all(isinstance(step.done, int) for step in seen))

    def test_history_is_read_back_and_reported(self) -> None:
        summary = SessionSummary(
            "2026-09-12T14:03:00",
            "2026-09-12T14:10:00",
            "A",
            (("Automation 250", WriteOutcome(WriteStatus.VERIFIED, "ok"), "file.st"),),
        )
        self.backend.history_store.record(summary)
        (read,) = self.backend.history()
        self.assertEqual(read.items[0][0], "Automation 250")
        self.assertIn("Automation 250", self.backend.report_text(read))

    def test_a_session_can_be_created_for_queued_items(self) -> None:
        item = fmt.new_queue_item("Automation 250", Platform.ATARI_ST, disk_id=1)
        session = self.backend.create_session([item], events=None)
        self.assertTrue(callable(session.run) and callable(session.cancel))

    def test_series_kinds_are_known_to_the_filters(self) -> None:
        kinds = {kind for kind, _label, _tooltip in fmt.KIND_FILTERS}
        self.assertEqual(kinds, set(DiskKind))


def build_catalogue(path: Path) -> None:
    """A small catalogue made by the real catalogue builder."""
    from catalogue_builder.merge import SourceBatch, merge_records, write_catalogue
    from catalogue_builder.records import (
        ContentRecord,
        DiskRecord,
        ImageRecordIn,
        LocationRecord,
        SourceInfo,
    )
    from catalogue_builder.series import GroupRegistry, SeriesDef, SeriesRegistry

    registry = SeriesRegistry(
        {"automation": SeriesDef("automation", "Automation", "atari-st", "menu", aliases=["auto"])}
    )

    def menu(number: int, *titles: str, online: bool = False) -> DiskRecord:
        name = f"Automation {number}.st"
        locations = []
        if online:
            locations = [
                LocationRecord("atari-legend", f"https://example.invalid/{number}.zip", "zip")
            ]
        return DiskRecord(
            "tosec",
            "atari-st",
            "menu",
            "automation",
            number,
            contents=[ContentRecord(title) for title in titles],
            images=[ImageRecordIn(name, "st", md5=f"{number:032x}")],
            locations=locations,
        )

    batches = [
        SourceBatch(
            SourceInfo("tosec", "TOSEC", "https://tosec.example"),
            [menu(250, "Necron", "Boulderdash CK"), menu(251, "Rick Dangerous", online=True)],
        ),
        SourceBatch(SourceInfo("atari-legend", "Atari Legend", "https://example.invalid"), []),
    ]
    result = merge_records(batches, registry)
    connection = sqlite3.connect(path)
    write_catalogue(
        connection, result, meta={"built_at": "2026-09-12"}, groups=GroupRegistry({}, {})
    )
    connection.commit()
    connection.close()


def catalogue_modules_missing() -> str:
    try:
        import catalogue_builder.merge  # noqa: F401
        from piratefinder.catalogue.store import Catalogue  # noqa: F401
    except ImportError as error:
        return str(error)
    return MISSING


@unittest.skipIf(catalogue_modules_missing(), "catalogue modules missing")
class RealBackendWithCatalogueTests(unittest.TestCase):
    def setUp(self) -> None:
        from piratefinder.catalogue.store import Catalogue

        self.folder = Path(tempfile.mkdtemp(dir=_HOME))
        path = self.folder / "catalogue.sqlite"
        build_catalogue(path)
        self.settings = Settings(path=self.folder / "settings.json")
        self.userdb = UserDatabase.open(self.folder / "user.sqlite")
        self.backend = RealBackend(self.settings, self.userdb, Catalogue.open(path))

    def tearDown(self) -> None:
        self.backend.close()

    def test_catalogue_information(self) -> None:
        info = self.backend.catalogue_info()
        self.assertTrue(info.available)
        self.assertEqual(info.built_at, "2026-09-12")
        self.assertEqual(info.stats["disks"], 2)
        self.assertEqual([series.name for series in info.series], ["Automation"])
        self.assertEqual(info.providers, (("atari-legend", "Atari Legend"),))
        self.assertIn("TOSEC", {source["name"] for source in info.sources})

    def test_search_detail_and_availability(self) -> None:
        results = self.backend.search("rick", SearchFilters())
        self.assertEqual([result.disk.label for result in results], ["Automation 251"])
        self.assertEqual(results[0].matched, ("Rick Dangerous",))
        detail = self.backend.detail(results[0].disk.id)
        self.assertEqual(detail.availability.value, "online")
        self.settings.providers = {"atari-legend": False}
        (again,) = self.backend.search("rick", SearchFilters())
        self.assertEqual(again.availability.value, "missing")


def tearDownModule() -> None:
    shutil.rmtree(_HOME, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()

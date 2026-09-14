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
from unittest import mock

_HOME = Path(tempfile.mkdtemp(prefix="piratefinder-ui-real-"))
for _variable in ("XDG_CONFIG_HOME", "XDG_DATA_HOME"):
    os.environ[_variable] = str(_HOME / _variable.lower())

from piratefinder.jobs.queue import new_item  # noqa: E402
from piratefinder.models import (  # noqa: E402
    DiskKind,
    MediaItem,
    Platform,
    Query,
    ResultMode,
    SessionSummary,
    VirusStatus,
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
        page = self.backend.search_page(Query(text="anything"))
        self.assertEqual((page.rows, page.total), ((), 0))
        self.assertEqual(self.backend.facets().crews, ())
        self.assertEqual(self.backend.clean_alternates(1), [])
        self.assertEqual(self.backend.summaries(1), [])
        with self.assertRaises(LookupError):
            self.backend.detail(1)

    def test_media_is_not_fetched_when_switched_off(self) -> None:
        from piratefinder.ui.backend import set_fetch_media

        set_fetch_media(self.settings, False)
        item = MediaItem("menu", "https://example.invalid/a.png", "synthetic")
        self.assertIsNone(self.backend.media_file(item))
        self.assertEqual(self.backend.summaries(1, 2), [])

    def test_boot_blocks_are_checked_once_for_each_set_of_virus_data(self) -> None:
        from piratefinder.models import BootRecheck

        steps = []
        first = self.backend.recheck_boot_blocks(steps.append, None)
        self.assertEqual(first, BootRecheck(), "a new database has nothing to check yet")
        self.assertIsNone(self.backend.recheck_boot_blocks(steps.append, None))

    def test_brainfile_status_is_always_answered(self) -> None:
        status = self.backend.brainfile_status()
        self.assertIsInstance(status.installed, bool)
        self.assertEqual(self.backend.infected_files(), [])

    def test_infected_files_are_read_with_one_query(self) -> None:
        from piratefinder.library.userdb import LibraryEntry

        for number in range(3):
            path = f"/nas/{number}.adf"
            found = LibraryEntry(
                path, display_name=f"Disk {number}", boot_status="virus", boot_name="SCA"
            )
            self.userdb.store_file(path, 1, 1.0, [found])
        whole_index = AssertionError("the whole library index was read")
        with mock.patch.object(self.userdb, "entries", side_effect=whole_index):
            infected = self.backend.infected_files(2)
        self.assertEqual(
            [(local.path, local.virus) for local in infected],
            [("/nas/0.adf", "SCA"), ("/nas/1.adf", "SCA")],
        )

    def test_virus_reports_come_from_the_finder_without_a_catalogue(self) -> None:
        from piratefinder.library.userdb import LibraryEntry

        path = str(self.folder / "gone.adf")  # scanned with a virus, then deleted
        entry = LibraryEntry(path, display_name="Gone", boot_status="virus", boot_name="SCA")
        self.userdb.store_file(path, 1, 1.0, [entry])
        (local,) = self.backend.infected_files()
        report = self.backend.virus_report(local)
        self.assertEqual(
            (report.status, report.name, report.removable), (VirusStatus.VIRUS, "SCA", False)
        )
        self.assertIn("cannot be read now", report.explanation)

    def test_queue_calls_reach_the_write_queue(self) -> None:
        first = new_item("Automation 250", Platform.ATARI_ST, disk_id=1)
        second = new_item("Automation 251", Platform.ATARI_ST, disk_id=2)
        self.backend.queue_add([first, second, new_item("again", None, disk_id=1)])
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
        item = new_item("Automation 250", Platform.ATARI_ST, disk_id=1)
        session = self.backend.create_session([item], events=None)
        self.assertTrue(callable(session.run) and callable(session.cancel))

    def test_the_device_setting_reaches_the_check_and_gw_info(self) -> None:
        from piratefinder.greaseweazle import client
        from piratefinder.models import DeviceStatus

        self.settings.device = "/dev/ttyACM3"
        with mock.patch.object(client, "device_present", return_value=True) as present:
            self.assertTrue(self.backend.device_present())
        present.assert_called_once_with("/dev/ttyACM3")
        answer = DeviceStatus(True, "Greaseweazle V4 connected on /dev/ttyACM3.")
        with mock.patch.object(client, "probe", return_value=answer) as probe:
            self.assertIs(self.backend.probe(), answer)
        self.assertEqual(probe.call_args.kwargs["device"], "/dev/ttyACM3")
        self.assertTrue(probe.call_args.kwargs["online"])
        # With online use off, gw info's firmware lookup must not reach the network.
        self.settings.online_enabled = False
        with mock.patch.object(client, "probe", return_value=answer) as probe:
            self.backend.probe()
        self.assertFalse(probe.call_args.kwargs["online"])

    def test_series_kinds_are_known_to_the_filters(self) -> None:
        kinds = {kind for kind, _label, _tooltip in fmt.KIND_FILTERS}
        self.assertEqual(kinds, set(DiskKind))


class BrainfileStatusTests(unittest.TestCase):
    def test_whatever_the_virus_module_returns_is_understood(self) -> None:
        from piratefinder.ui.real_backend import as_brainfile_status

        self.assertFalse(as_brainfile_status(None).installed)
        status = as_brainfile_status({"version": "2.1", "entries": 12, "path": "/x"})
        self.assertEqual((status.installed, status.version, status.entries), (True, "2.1", 12))

        class Status:
            installed = False
            version = ""
            count = 0
            path = ""

        self.assertFalse(as_brainfile_status(Status()).installed)


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
        page = self.backend.search_page(Query(text="rick", mode=ResultMode.DISCS))
        self.assertEqual([row.disk.label for row in page.rows], ["Automation 251"])
        self.assertEqual(page.total, 1)
        self.assertEqual(page.rows[0].matched, ("Rick Dangerous",))
        titles = self.backend.search_page(Query(text="rick"))
        self.assertEqual([row.title for row in titles.rows], ["Rick Dangerous"])
        detail = self.backend.detail(page.rows[0].disk.id)
        self.assertEqual(detail.availability.value, "online")
        self.assertIsNone(detail.virus)
        facets = self.backend.facets()
        self.assertIn("Automation", dict(facets.crews))
        self.settings.providers = {"atari-legend": False}
        (again,) = self.backend.search_page(Query(text="rick", mode=ResultMode.DISCS)).rows
        self.assertEqual(again.availability.value, "missing")

    def test_an_unmatched_file_is_linked_to_a_disc_and_unlinked(self) -> None:
        from tests.test_library_helpers import make_st_image

        files = self.folder / "files"
        files.mkdir()
        (files / "a251.st").write_bytes(make_st_image("downloaded by hand"))
        self.settings.library_folders = [str(files)]
        self.settings.download_folder = str(self.folder / "downloads")
        self.backend.scan_library(lambda _progress: None, None)
        [local] = self.backend.unmatched_files()
        (disc,) = self.backend.search_page(Query(text="rick", mode=ResultMode.DISCS)).rows
        linked = self.backend.link_file(local, disc.disk.id)
        self.assertEqual(linked.disk_id, disc.disk.id)
        detail = self.backend.detail(disc.disk.id)
        self.assertEqual(detail.availability.value, "local")
        self.assertEqual([f.path for f in detail.local_files], [local.path])
        self.assertEqual(self.backend.unmatched_files(), [])
        self.assertIsNone(self.backend.unlink_file(linked).disk_id)
        self.assertEqual(self.backend.detail(disc.disk.id).availability.value, "online")

    def test_edit_details_are_kept_shown_and_reverted(self) -> None:
        from piratefinder.library.corrections import CorrectionError

        (row,) = self.backend.search_page(Query(text="rick")).rows
        disk_id, content_id = row.disk.id, row.content_id
        with self.assertRaises(CorrectionError):
            self.backend.save_details(disk_id, {"date": "June 1990"})
        self.backend.save_details(
            disk_id, {"crew": "Someone Else", "date": "1990-06"}, {content_id: "Rick Dangerous 1"}
        )
        detail = self.backend.detail(disk_id)
        self.assertEqual(
            (detail.disk.crew, detail.disk.year, detail.disk.month), ("Someone Else", 1990, 6)
        )
        self.assertEqual(detail.edited, ("crew", "date"))
        (shown,) = self.backend.search_page(Query(text="rick")).rows
        self.assertEqual((shown.title, shown.disk.crew), ("Rick Dangerous 1", "Someone Else"))
        # The search matches, filters and counts by the corrected values.
        (found,) = self.backend.search_page(Query(text="someone")).rows
        self.assertEqual(found.title, "Rick Dangerous 1")
        self.assertEqual(self.backend.search_page(Query(crew="Someone Else")).total, 1)
        self.assertEqual(self.backend.search_page(Query(year=1990)).total, 1)
        self.assertEqual(dict(self.backend.facets().crews).get("Someone Else"), 1)
        self.backend.revert_details(disk_id)
        self.assertEqual(self.backend.detail(disk_id).edited, ())
        self.assertEqual(self.backend.search_page(Query(text="someone")).total, 0)
        self.assertNotIn("Someone Else", dict(self.backend.facets().crews))


def tearDownModule() -> None:
    shutil.rmtree(_HOME, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()

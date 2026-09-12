"""Read-only catalogue access and catalogue lookup."""

from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from catalogue_builder.merge import SourceBatch, merge_records, write_catalogue
from catalogue_builder.records import (
    ContentRecord,
    DiskRecord,
    ImageRecordIn,
    LocationRecord,
    SourceInfo,
)
from catalogue_builder.series import GroupRegistry, SeriesDef, SeriesRegistry
from piratefinder.catalogue import store
from piratefinder.catalogue.store import Catalogue, CatalogueError, locate_catalogue
from piratefinder.models import ContentKind, DiskKind, Platform

TOSEC = SourceInfo("tosec", "TOSEC", "https://tosec.example", "free")


def write_sample(path: Path, built_at: str = "2026-09-12T10:00:00+00:00") -> None:
    registry = SeriesRegistry(
        {
            "automation": SeriesDef(
                "automation", "Automation", "atari-st", "menu", aliases=["auto", "a"]
            ),
            "d-bug": SeriesDef("d-bug", "D-Bug", "atari-st", "menu", aliases=["dbug", "a"]),
        }
    )
    records = [
        DiskRecord(
            "tosec",
            "atari-st",
            "menu",
            "automation",
            250,
            title="Automation Menu Disk 250 (1990)(Automation)",
            menu_text="Hi to all our contacts",
            contents=[
                ContentRecord("Xenon 2 - Megablast", publisher="Image Works"),
                ContentRecord("Tetris", kind="game"),
            ],
            images=[
                ImageRecordIn("a250.st", "st", md5="aa", sha1="a1", crc32="0000cafe", size=100),
                ImageRecordIn(
                    "a250[b].st", "st", "[b]", md5="bb", crc32="0000beef", size=200, bad=True
                ),
            ],
            locations=[
                LocationRecord(
                    "internet-archive",
                    "https://ia/a250.zip",
                    "zip",
                    "a250.st",
                    image_name="a250.st",
                )
            ],
            links=[("TOSEC", "https://tosec.example/a250")],
        ),
        DiskRecord(
            "tosec",
            "atari-st",
            "menu",
            "d-bug",
            100,
            "A",
            images=[ImageRecordIn("db100a.st", "st", md5="d1")],
        ),
        DiskRecord(
            "tosec",
            "atari-st",
            "menu",
            "d-bug",
            100,
            "B",
            images=[
                ImageRecordIn("db100b-first.st", "st", md5="d3"),
                ImageRecordIn(
                    "db100b.st", "st", md5="d2", crc32="0000beef", size=200, sha512="5" * 128
                ),
            ],
        ),
        DiskRecord(
            "tosec",
            "amiga",
            "single",
            title="Xenon (1988)(Melbourne House)[cr QTX]",
            cracker="Quartex",
            contents=[ContentRecord("Xenon", publisher="Melbourne House", cracker="Quartex")],
            images=[ImageRecordIn("x.adf", "adf", md5="cc")],
        ),
    ]
    result = merge_records([SourceBatch(TOSEC, records)], registry)
    connection = sqlite3.connect(path)
    write_catalogue(connection, result, meta={"built_at": built_at}, groups=GroupRegistry({}, {}))
    connection.commit()
    connection.close()


class StoreTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.folder = tempfile.TemporaryDirectory()
        cls.path = Path(cls.folder.name) / "catalogue.sqlite"
        write_sample(cls.path)
        cls.catalogue = Catalogue.open(cls.path)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.catalogue.close()
        cls.folder.cleanup()

    def automation(self):
        return next(d for d in self.catalogue.disks(range(1, 10)).values() if d.number == 250)

    def test_opens_read_only(self) -> None:
        with self.assertRaises(sqlite3.OperationalError):
            self.catalogue.query("INSERT INTO meta(key, value) VALUES ('x', 'y')")
        self.assertEqual(self.catalogue.built_at, "2026-09-12T10:00:00+00:00")
        self.assertEqual(self.catalogue.path, self.path)

    def test_refuses_missing_foreign_and_newer_files(self) -> None:
        with self.assertRaises(CatalogueError):
            Catalogue.open(self.path.with_name("absent.sqlite"))
        with tempfile.TemporaryDirectory() as folder:
            other = Path(folder) / "other.sqlite"
            other.write_bytes(b"not a database at all" * 100)
            with self.assertRaises(CatalogueError):
                Catalogue.open(other)
            newer = Path(folder) / "newer.sqlite"
            write_sample(newer)
            with sqlite3.connect(newer) as connection:
                connection.execute("UPDATE meta SET value = '99' WHERE key = 'schema_version'")
            with self.assertRaises(CatalogueError):
                Catalogue.open(newer)

    def test_disks_and_their_details(self) -> None:
        disk = self.automation()
        self.assertEqual(disk.label, "Automation 250")
        self.assertEqual((disk.platform, disk.kind), (Platform.ATARI_ST, DiskKind.MENU))
        self.assertEqual(disk.series_name, "Automation")
        self.assertEqual(disk.menu_text, "Hi to all our contacts")
        self.assertEqual(self.catalogue.disk(disk.id), disk)
        self.assertIsNone(self.catalogue.disk(999))
        contents = self.catalogue.contents(disk.id)
        self.assertEqual([c.title for c in contents], ["Xenon 2 - Megablast", "Tetris"])
        self.assertEqual(contents[0].kind, ContentKind.GAME)
        self.assertEqual(contents[0].position, 1)
        images = self.catalogue.images(disk.id)
        self.assertEqual(
            [(i.name, i.rank, i.bad) for i in images],
            [("a250.st", 0, False), ("a250[b].st", 1, True)],
        )
        self.assertEqual(self.catalogue.image(images[1].id), images[1])
        (location,) = self.catalogue.locations(disk.id)
        self.assertEqual((location.provider, location.image_id), ("internet-archive", images[0].id))
        self.assertEqual(
            [link.url for link in self.catalogue.links(disk.id)], ["https://tosec.example/a250"]
        )

    def test_series_and_aliases(self) -> None:
        series = {s.id: s for s in self.catalogue.series()}
        self.assertEqual(series["d-bug"].disk_count, 2)
        self.assertEqual(series["automation"].aliases, ("a", "auto", "automation"))
        aliases = self.catalogue.aliases()
        self.assertEqual(aliases["auto"], ("automation",))
        self.assertEqual(set(aliases["a"]), {"automation", "d-bug"})
        self.assertEqual(aliases["d bug"], ("d-bug",))

    def test_match_image_tries_md5_sha1_sha512_then_crc_and_prefers_good_dumps(self) -> None:
        match = self.catalogue.match_image
        self.assertEqual(match(md5="AA").name, "a250.st")
        self.assertEqual(match(sha1="a1").name, "a250.st")
        self.assertEqual(match(sha512="5" * 128).name, "db100b.st")
        self.assertEqual(match(md5="cc", crc32="0000cafe", size=100).name, "x.adf")
        self.assertEqual(match(md5="zz", crc32="0000cafe", size=100).name, "a250.st")
        self.assertIsNone(match(crc32="0000cafe"))  # a CRC without a size is not enough
        # Both dumps have rank 1; the good one wins over the bad one.
        self.assertEqual(match(crc32="0000BEEF", size=200).name, "db100b.st")
        self.assertIsNone(match())

    def test_disks_with_locations_stats_and_sources(self) -> None:
        disk = self.automation()
        self.assertEqual(self.catalogue.disks_with_locations(["internet-archive"]), {disk.id})
        self.assertEqual(self.catalogue.disks_with_locations(["atari-legend"]), set())
        self.assertEqual(self.catalogue.disks_with_locations([]), set())
        self.assertEqual(self.catalogue.providers(), ["internet-archive"])
        stats = self.catalogue.stats()
        self.assertEqual(stats["disks"], 4)
        self.assertEqual(stats["platform:atari-st"], 3)
        self.assertEqual(stats["kind:single"], 1)
        self.assertEqual(stats["amiga:single"], 1)
        self.assertEqual(stats["images"], 6)
        self.assertEqual(stats["disks with locations"], 1)
        (source,) = self.catalogue.sources()
        self.assertEqual(source["id"], "tosec")
        self.assertEqual(source["records"], "4")


class LocateTest(unittest.TestCase):
    def test_the_environment_variable_wins(self) -> None:
        with mock.patch.dict(os.environ, {"PIRATEFINDER_CATALOGUE": "/nowhere/cat.sqlite"}):
            self.assertEqual(locate_catalogue(), Path("/nowhere/cat.sqlite"))

    def test_the_newest_usable_candidate_is_chosen(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder)
            older, newer, broken = base / "older.sqlite", base / "newer.sqlite", base / "bad"
            write_sample(older, "2026-01-01T00:00:00+00:00")
            write_sample(newer, "2026-06-01T00:00:00+00:00")
            broken.write_text("junk")
            candidates = [base / "missing.sqlite", older, broken, newer]
            environment = {k: v for k, v in os.environ.items() if k != "PIRATEFINDER_CATALOGUE"}
            with (
                mock.patch.dict(os.environ, environment, clear=True),
                mock.patch.object(store.paths, "catalogue_candidates", return_value=candidates),
            ):
                self.assertEqual(locate_catalogue(), newer)
                candidates[:] = [base / "missing.sqlite", broken]
                self.assertIsNone(locate_catalogue())


if __name__ == "__main__":
    unittest.main()

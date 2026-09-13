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
    CrewRecord,
    DiskRecord,
    ImageRecordIn,
    LocationRecord,
    MediaRecordIn,
    SourceInfo,
    TriviaRecordIn,
)
from catalogue_builder.series import GroupRegistry, SeriesDef, SeriesRegistry
from piratefinder.catalogue import schema, store
from piratefinder.catalogue.store import Catalogue, CatalogueError, locate_catalogue
from piratefinder.models import ContentKind, CrewInfo, DiskKind, MediaItem, Platform, TriviaItem

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
            release_date="1990-06-17",
            menu_text="Hi to all our contacts",
            contents=[
                ContentRecord(
                    "Xenon 2 - Megablast",
                    publisher="Image Works",
                    links=[("wikipedia", "Xenon 2 Megablast")],
                ),
                ContentRecord("Tetris", kind="game"),
            ],
            images=[
                ImageRecordIn("a250.st", "st", md5="aa", sha1="a1", crc32="0000cafe", size=100),
                ImageRecordIn(
                    "a250[b].st",
                    "st",
                    "[b][v Ghost]",
                    md5="bb",
                    crc32="0000beef",
                    size=200,
                    bad=True,
                    virus="Ghost",
                ),
            ],
            media=[
                MediaRecordIn("snap", "https://m/tetris.png", content_title="Tetris", rank=5),
                MediaRecordIn("menu", "https://m/menu.png", credit="Atari Legend", rank=10),
            ],
            trivia=[
                TriviaRecordIn("fact", "Tetris came from Moscow.", content_title="Tetris"),
                TriviaRecordIn("note", "A second version exists.", licence="CC BY-NC-SA 4.0"),
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
            media=[
                MediaRecordIn("boxart", "https://m/xenon.png", content_title="Xenon"),
                MediaRecordIn("boxart", "https://m/xenon.png", width=320, height=200),
            ],
        ),
    ]
    crews = [
        CrewRecord(
            "Automation",
            "tosec",
            "A crew.",
            ["Rob C", "Sharaz Jek"],
            "1988",
            "https://c",
            "Auto",
            platforms={"atari-st": 800},
        ),
        # Namesakes on the other platform: neither is the crew of a disk here.
        CrewRecord("Automation", "tosec", "An Amiga demo group.", platforms={"amiga": 3}),
        CrewRecord("Quartex", "tosec", "An Atari ST crew.", platforms={"atari-st": 1}),
    ]
    result = merge_records([SourceBatch(TOSEC, records, crews=crews)], registry)
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

    def test_schema_2_disk_content_and_image_fields(self) -> None:
        disk = self.automation()
        self.assertEqual(
            (disk.category, disk.crew, disk.year, disk.month, disk.day),
            ("Games", "Automation", 1990, 6, 17),
        )
        self.assertNotIn("17", disk.date, "the day comes from the release date alone")
        contents = self.catalogue.contents(disk.id)
        self.assertTrue(all(content.id > 0 for content in contents))
        self.assertEqual(self.catalogue.content(contents[1].id), contents[1])
        self.assertIsNone(self.catalogue.content(9999))
        images = self.catalogue.images(disk.id)
        self.assertEqual([image.virus for image in images], ["", "Ghost"])
        xenon = next(
            d for d in self.catalogue.disks(range(1, 10)).values() if d.label.startswith("Xenon")
        )
        self.assertEqual(
            (xenon.crew, xenon.year, xenon.month, xenon.day), ("Quartex", None, None, None)
        )

    def test_media_trivia_and_crew(self) -> None:
        disk = self.automation()
        tetris = self.catalogue.contents(disk.id)[1]
        self.assertEqual(
            self.catalogue.media(disk.id),
            [
                MediaItem("snap", "https://m/tetris.png", "tosec", content_id=tetris.id),
                MediaItem("menu", "https://m/menu.png", "tosec", credit="Atari Legend"),
            ],
        )
        xenon = next(
            d for d in self.catalogue.disks(range(1, 10)).values() if d.label.startswith("Xenon")
        )
        # Filed for the disc and for its only title: shown once, as the disc's.
        self.assertEqual(
            self.catalogue.media(xenon.id),
            [MediaItem("boxart", "https://m/xenon.png", "tosec", width=320, height=200)],
        )
        megablast = self.catalogue.contents(disk.id)[0]
        self.assertEqual(
            self.catalogue.trivia(disk.id),
            [
                TriviaItem("note", "A second version exists.", "tosec", licence="CC BY-NC-SA 4.0"),
                TriviaItem(
                    "wikipedia",
                    "Xenon 2 Megablast",
                    "tosec",
                    url="https://en.wikipedia.org/wiki/Xenon_2_Megablast",
                    title="Xenon 2 Megablast",
                    content_id=megablast.id,
                ),
                TriviaItem("fact", "Tetris came from Moscow.", "tosec", content_id=tetris.id),
            ],
        )
        self.assertEqual(self.catalogue.trivia(999), [])
        self.assertEqual(
            self.catalogue.crew_for_disk(disk.id),
            CrewInfo(
                "Automation",
                "A crew.",
                ("Rob C", "Sharaz Jek"),
                "1988",
                "tosec",
                "https://c",
                "Auto",
            ),
        )
        self.assertEqual(xenon.crew, "Quartex")
        self.assertIsNone(self.catalogue.crew_for_disk(xenon.id), "the ST Quartex is not Xenon's")
        self.assertIsNone(self.catalogue.crew_for_disk(999))

    def test_disk_sets_are_temporary_tables_reused_for_the_same_ids(self) -> None:
        before = self.path.read_bytes()
        with self.catalogue.batch():
            name = self.catalogue.disk_set([3, 1, 2, 2])
            self.assertEqual(self.catalogue.disk_set({1, 2, 3}), name)
            self.assertEqual(self.catalogue.query(f"SELECT id FROM {name}"), [(1,), (2,), (3,)])
            names = [self.catalogue.disk_set([100 + n]) for n in range(store.DISK_SETS)]
            self.assertEqual(self.catalogue.query(f"SELECT id FROM {names[-1]}"), [(107,)])
            with self.assertRaises(sqlite3.OperationalError):  # the oldest set was dropped
                self.catalogue.query(f"SELECT id FROM {name}")
        self.assertEqual(self.path.read_bytes(), before)
        hosted = self.catalogue.disks_with_locations(["internet-archive"])
        hosted.add(999)  # the cached set is not handed out
        self.assertEqual(
            self.catalogue.disks_with_locations(["internet-archive"]), {self.automation().id}
        )

    def test_the_available_set_is_remembered_until_its_table_is_dropped(self) -> None:
        disk = self.automation()
        with self.catalogue.batch():
            name, size = self.catalogue.available_set([999], ["internet-archive"])
            self.assertEqual(size, 2)
            self.assertEqual(self.catalogue.query(f"SELECT id FROM {name}"), [(disk.id,), (999,)])
            self.assertEqual(self.catalogue.available_set({999}, ("internet-archive",)), (name, 2))
            for number in range(store.DISK_SETS):
                self.catalogue.disk_set([500 + number])
            again, _size = self.catalogue.available_set([999], ["internet-archive"])
            self.assertEqual(self.catalogue.query(f"SELECT id FROM {again}"), [(disk.id,), (999,)])
            self.assertEqual(self.catalogue.available_set([], []), ("", 0))

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
        self.assertEqual(
            (stats["entries"], stats["media"], stats["trivia"], stats["crews"]), (5, 4, 3, 1)
        )
        (source,) = self.catalogue.sources()
        self.assertEqual(source["id"], "tosec")
        self.assertEqual(source["records"], "4")


class LayoutTest(unittest.TestCase):
    """A catalogue of another layout is refused with a message that names both layouts."""

    def test_refuses_a_catalogue_from_an_older_or_newer_piratefinder(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            for version in (schema.SCHEMA_VERSION - 1, schema.SCHEMA_VERSION + 1):
                path = Path(folder) / f"layout{version}.sqlite"
                with sqlite3.connect(path) as connection:
                    schema.create(connection)
                    connection.execute(
                        "UPDATE meta SET value = ? WHERE key = 'schema_version'", (str(version),)
                    )
                connection.close()
                with self.subTest(version=version), self.assertRaises(CatalogueError) as caught:
                    Catalogue.open(path)
                self.assertEqual(
                    str(caught.exception),
                    f"{path} was made for a different PirateFinder version (layout {version}; "
                    f"this version reads layout {schema.SCHEMA_VERSION}).",
                )

    def test_a_database_without_a_layout_is_not_a_catalogue(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "other.sqlite"
            with sqlite3.connect(path) as connection:
                connection.execute("CREATE TABLE notes (text TEXT)")
            connection.close()
            with self.assertRaises(CatalogueError) as caught:
                Catalogue.open(path)
            self.assertEqual(str(caught.exception), f"{path} is not a PirateFinder catalogue.")


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


class CrewLookupTest(unittest.TestCase):
    def test_a_disk_gets_its_own_crew_not_a_namesake(self) -> None:
        # Awesome made Atari ST menus; another Awesome was an Amiga demo group.
        # Both have a crews row, so a lookup by name alone shows one crew's
        # history on the other's disks.
        registry = SeriesRegistry(
            {"awesome": SeriesDef("awesome", "Awesome", "atari-st", "menu", group="Awesome")}
        )
        records = [
            DiskRecord("tosec", "atari-st", "menu", "awesome", 1),
            DiskRecord("tosec", "amiga", "single", title="Awesome Demo", publisher="Awesome"),
        ]
        crews = [
            CrewRecord("Awesome", "tosec", "An ST menu crew.", platforms={"atari-st": 36}),
            CrewRecord("Awesome", "zoo", "An Amiga demo group.", platforms={"amiga": 40}),
        ]
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "catalogue.sqlite"
            connection = sqlite3.connect(path)
            write_catalogue(
                connection,
                merge_records([SourceBatch(TOSEC, records, crews=crews)], registry),
                groups=GroupRegistry({}, {}),
            )
            connection.commit()
            connection.close()
            with Catalogue.open(path) as catalogue:
                found = {
                    disk.label: catalogue.crew_for_disk(disk.id)
                    for disk in catalogue.disks([1, 2]).values()
                }
        self.assertEqual(
            {label: (crew.name, crew.notes) for label, crew in found.items()},
            {
                "Awesome 1": ("Awesome", "An ST menu crew."),
                "Awesome Demo": ("Awesome", "An Amiga demo group."),
            },
        )


class CompactMediaTest(unittest.TestCase):
    """Media rows are stored as shared prefixes and credit templates and read back whole."""

    PICTURES = 60  # more than SHARED_PREFIX, so the folders become prefixes

    @classmethod
    def setUpClass(cls) -> None:
        cls.folder = tempfile.TemporaryDirectory()
        path = Path(cls.folder.name) / "catalogue.sqlite"
        records = []
        for number in range(cls.PICTURES):
            page = f"https://zoo.example/productions/{number}/"
            records.append(
                DiskRecord(
                    "tosec",
                    "amiga",
                    "pack",
                    title=f"Pack {number}",
                    images=[ImageRecordIn(f"p{number}.adf", "adf", md5=f"{number:032x}")],
                    locations=[
                        LocationRecord(
                            "archive",
                            f"https://archive.example/download/packs/p{number}.zip",
                            container="zip",
                            page_url="https://archive.example/details/packs",
                        )
                    ],
                    media=[
                        MediaRecordIn(
                            "menu",
                            f"https://media.zoo.example/screens/s/{number % 3}/{number}.png",
                            thumb_url=f"https://media.zoo.example/screens/t/{number % 3}/{number}.png",
                            width=320,
                            height=256,
                            credit=f"Zoo contributors, zoo.example/productions/{number}/",
                            page_url=page,
                        )
                    ],
                )
            )
        records.append(
            DiskRecord(
                "tosec",
                "amiga",
                "pack",
                title="Odd Pack",
                images=[ImageRecordIn("odd.adf", "adf", md5="f" * 32)],
                media=[
                    MediaRecordIn(
                        "snap",
                        "https://elsewhere.example/one/odd.gif",
                        thumb_url="https://elsewhere.example/thumbs/odd.gif",
                        credit="Braces {kept} and {{doubled}}, and {page}",
                    )
                ],
            )
        )
        result = merge_records([SourceBatch(TOSEC, records)], SeriesRegistry({}))
        connection = sqlite3.connect(path)
        write_catalogue(
            connection, result, meta={"built_at": "2026-09-13"}, groups=GroupRegistry({}, {})
        )
        connection.commit()
        cls.prefixes = connection.execute("SELECT text FROM address_prefix ORDER BY id").fetchall()
        cls.credits = connection.execute("SELECT source, credit FROM media_credit").fetchall()
        cls.rests = connection.execute("SELECT url, thumb_url, page_url FROM media").fetchall()
        connection.close()
        cls.catalogue = Catalogue.open(path)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.catalogue.close()
        cls.folder.cleanup()

    def pictures(self, title: str) -> list:
        [(disk_id,)] = self.catalogue.query("SELECT id FROM disks WHERE title = ?", (title,))
        return self.catalogue.media(disk_id)

    def test_addresses_and_credits_come_back_whole(self) -> None:
        [item] = self.pictures("Pack 7")
        self.assertEqual(item.url, "https://media.zoo.example/screens/s/1/7.png")
        self.assertEqual(item.thumb_url, "https://media.zoo.example/screens/t/1/7.png")
        self.assertEqual(item.page_url, "https://zoo.example/productions/7/")
        self.assertEqual(item.credit, "Zoo contributors, zoo.example/productions/7/")
        self.assertEqual(
            (item.source, item.kind, item.width, item.height), ("tosec", "menu", 320, 256)
        )

    def test_location_addresses_come_back_whole(self) -> None:
        [(disk_id,)] = self.catalogue.query("SELECT id FROM disks WHERE title = 'Pack 7'")
        [location] = self.catalogue.locations(disk_id)
        self.assertEqual(location.url, "https://archive.example/download/packs/p7.zip")
        self.assertEqual(location.page_url, "https://archive.example/details/packs")
        self.assertIn(("https://archive.example/download/packs/",), self.prefixes)

    def test_a_credit_with_braces_and_no_page_is_kept_as_written(self) -> None:
        [item] = self.pictures("Odd Pack")
        self.assertEqual(item.credit, "Braces {kept} and {{doubled}}, and {page}")
        self.assertEqual(item.url, "https://elsewhere.example/one/odd.gif")
        self.assertEqual(item.thumb_url, "https://elsewhere.example/thumbs/odd.gif")
        self.assertEqual(item.page_url, "")

    def test_shared_beginnings_and_credits_are_stored_once(self) -> None:
        self.assertEqual(
            self.prefixes,
            [
                ("",),
                # Every address shares its scheme; the odd one keeps only that.
                ("https://",),
                ("https://archive.example/download/packs/",),
                # One page for every pack: a beginning counts distinct addresses.
                ("https://archive.example/",),
                ("https://media.zoo.example/screens/s/",),
                ("https://media.zoo.example/screens/t/",),
                ("https://zoo.example/productions/",),
            ],
        )
        self.assertEqual(
            sorted(self.credits),
            [
                ("tosec", "Braces {{kept}} and {{{{doubled}}}}, and {{page}}"),
                ("tosec", "Zoo contributors, {page}"),
            ],
        )
        # A thumbnail with the same rest as its picture stores no rest.
        self.assertIn(("1/7.png", None, "7/"), self.rests)
        self.assertIn(
            (
                "elsewhere.example/one/odd.gif",
                "elsewhere.example/thumbs/odd.gif",
                "",
            ),
            self.rests,
        )


if __name__ == "__main__":
    unittest.main()

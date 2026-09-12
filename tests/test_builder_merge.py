"""Joining records from several sources and writing the catalogue."""

from __future__ import annotations

import json
import sqlite3
import unittest

from catalogue_builder.merge import SourceBatch, merge_records, search_text, write_catalogue
from catalogue_builder.records import (
    ContentRecord,
    DiskRecord,
    ImageRecordIn,
    LocationRecord,
    SourceInfo,
)
from catalogue_builder.series import GroupRegistry, SeriesDef, SeriesRegistry

TOSEC = SourceInfo("tosec", "TOSEC", "https://tosec.example")
LEGEND = SourceInfo("atari-legend", "Atari Legend", "https://al.example", "CC BY-NC-SA 4.0")
ARCHIVE = SourceInfo("internet-archive", "Internet Archive", "https://ia.example")


def registry() -> SeriesRegistry:
    return SeriesRegistry(
        {
            "automation": SeriesDef(
                "automation",
                "Automation",
                "atari-st",
                "menu",
                group="Automation",
                label="Automation {number}{part_suffix}{version_suffix}",
                aliases=["auto", "a"],
            ),
            "d-bug": SeriesDef("d-bug", "D-Bug", "atari-st", "menu", aliases=["dbug"]),
        }
    )


def image(name: str, md5: str, **fields: object) -> ImageRecordIn:
    return ImageRecordIn(name=name, format=name.rsplit(".", 1)[-1], md5=md5, **fields)


def build(*batches: SourceBatch, series: SeriesRegistry | None = None):
    connection = sqlite3.connect(":memory:")
    logged: list[str] = []
    result = merge_records(list(batches), series or registry(), logged.append)
    stats = write_catalogue(
        connection,
        result,
        meta={"built_at": "2026-09-12T10:00:00+00:00"},
        groups=GroupRegistry({"QTX": "Quartex"}, {}),
    )
    return connection, stats, logged


def automation_250(source: str, **fields: object) -> DiskRecord:
    return DiskRecord(
        source=source,
        platform="atari-st",
        kind="menu",
        series_key="automation",
        number=250,
        **fields,
    )


class KeyedMergeTest(unittest.TestCase):
    def setUp(self) -> None:
        tosec_record = automation_250(
            "tosec",
            title="Automation Menu Disk 250 (1990)(Automation)",
            date="1990",
            contents=[ContentRecord("Tosec Only Title")],
            images=[
                image(
                    "Automation Menu Disk 250 (1990)(Automation)[b].st", "bb", bad=True, flags="[b]"
                ),
                image("Automation Menu Disk 250 (1990)(Automation)[a].st", "a1", flags="[a]"),
                image(
                    "Automation Menu Disk 250 (1990)(Automation).st",
                    "c1",
                    crc32="1234abcd",
                    size=737280,
                ),
            ],
        )
        legend_record = automation_250(
            "atari-legend",
            title="Automation 250",
            credits="Menu by The Lost Boys",
            contents=[
                ContentRecord("Xenon 2 - Megablast", publisher="Image Works", cracker="QTX"),
                ContentRecord("Rick Dangerous"),
            ],
            images=[ImageRecordIn(name="a250.msa", format="msa", sha512="f" * 128, md5="c1")],
            links=[("Atari Legend", "https://al.example/menu/250")],
        )
        self.connection, self.stats, self.logged = build(
            SourceBatch(TOSEC, [tosec_record], priority=90),
            SourceBatch(LEGEND, [legend_record], priority=10),
        )

    def test_records_with_the_same_key_are_one_disk(self) -> None:
        rows = self.connection.execute("SELECT id, label, series_id, number FROM disks").fetchall()
        self.assertEqual(rows, [(1, "Automation 250", "automation", 250)])

    def test_the_best_source_gives_the_contents_and_every_source_is_searchable(self) -> None:
        titles = self.connection.execute(
            "SELECT title, source FROM contents ORDER BY position"
        ).fetchall()
        self.assertEqual(
            titles, [("Xenon 2 - Megablast", "atari-legend"), ("Rick Dangerous", "atari-legend")]
        )
        for word in ("tosec", "megablast", "rick", "quartex", "qtx", "lost", "works"):
            with self.subTest(word=word):
                found = self.connection.execute(
                    "SELECT rowid FROM disk_fts WHERE disk_fts MATCH ?", (f'"{word}"',)
                ).fetchall()
                self.assertEqual(found, [(1,)])

    def test_single_values_come_from_the_first_source_by_priority(self) -> None:
        title, date, credits = self.connection.execute(
            "SELECT title, date, credits FROM disks"
        ).fetchone()
        self.assertEqual(
            (title, date, credits), ("Automation 250", "1990", "Menu by The Lost Boys")
        )

    def test_images_with_the_same_hash_are_one_image_ranked_bad_last(self) -> None:
        rows = self.connection.execute(
            "SELECT name, md5, sha512, crc32, size, bad, rank FROM images ORDER BY rank"
        ).fetchall()
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[0][0], "a250.msa")  # best source first among clean dumps
        self.assertEqual(
            (rows[0][1], rows[0][2], rows[0][3], rows[0][4]), ("c1", "f" * 128, "1234abcd", 737280)
        )
        self.assertTrue(rows[1][0].endswith("[a].st"))
        self.assertEqual((rows[2][5], rows[2][6]), (1, 2))

    def test_sources_meta_and_links(self) -> None:
        sources = self.connection.execute(
            "SELECT id, licence, retrieved, records FROM sources ORDER BY id"
        ).fetchall()
        self.assertEqual(
            sources,
            [("atari-legend", "CC BY-NC-SA 4.0", "2026-09-12", 1), ("tosec", "", "2026-09-12", 1)],
        )
        meta = dict(self.connection.execute("SELECT key, value FROM meta"))
        self.assertEqual(meta["built_at"], "2026-09-12T10:00:00+00:00")
        self.assertEqual(meta["licence"], "CC BY-NC-SA 4.0")
        self.assertEqual(meta["schema_version"], "1")
        self.assertEqual(json.loads(meta["statistics"])["disks"], 1)
        self.assertEqual(
            self.connection.execute("SELECT label, url FROM links").fetchall(),
            [("Atari Legend", "https://al.example/menu/250")],
        )

    def test_series_and_aliases_are_written(self) -> None:
        aliases = self.connection.execute(
            "SELECT alias FROM series_alias WHERE series_id = 'automation' ORDER BY alias"
        ).fetchall()
        self.assertEqual([row[0] for row in aliases], ["a", "auto", "automation"])
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM series").fetchone()[0], 1)


class UnkeyedMergeTest(unittest.TestCase):
    def test_a_shared_hash_joins_an_unnumbered_record_to_a_disk(self) -> None:
        keyed = automation_250("tosec", images=[image("a.st", "abc")])
        single = DiskRecord(
            source="atari-legend",
            platform="atari-st",
            kind="single",
            title="Something",
            contents=[ContentRecord("Found By Hash")],
            images=[image("x.msa", "ABC")],
        )
        connection, stats, _ = build(
            SourceBatch(TOSEC, [keyed]), SourceBatch(LEGEND, [single], priority=10)
        )
        self.assertEqual(connection.execute("SELECT COUNT(*) FROM disks").fetchone()[0], 1)
        self.assertEqual(stats["hash merges"], 1)
        self.assertEqual(
            connection.execute("SELECT title FROM contents").fetchone()[0], "Found By Hash"
        )

    def test_without_a_shared_hash_a_record_is_its_own_disk(self) -> None:
        single = DiskRecord(
            source="tosec",
            platform="atari-st",
            kind="single",
            title="Chaos Engine, The (1993)(Renegade)(Disk 1 of 2)[cr Cynix]",
            part="1 of 2",
            images=[image("c.st", "c0")],
        )
        connection, _, _ = build(SourceBatch(TOSEC, [single]))
        label, part, series_id = connection.execute(
            "SELECT label, part, series_id FROM disks"
        ).fetchone()
        self.assertEqual(label, "The Chaos Engine (Disk 1 of 2) [cr Cynix]")
        self.assertEqual((part, series_id), ("1 of 2", None))

    def test_versions_v2_and_v2_0_are_the_same_disk(self) -> None:
        first = automation_250("tosec", version="v2.0", images=[image("a.st", "1")])
        second = automation_250("atari-legend", version="Version 2", images=[image("b.st", "2")])
        connection, _, _ = build(SourceBatch(TOSEC, [first]), SourceBatch(LEGEND, [second]))
        self.assertEqual(
            connection.execute("SELECT label, version FROM disks").fetchall(),
            [("Automation 250 v2", "v2")],
        )

    def test_parts_from_different_spellings_merge(self) -> None:
        first = DiskRecord("tosec", "atari-st", "menu", "d-bug", 100, "Disk 2 of 5")
        second = DiskRecord("d-bug", "atari-st", "menu", "d-bug", 100, "b")
        connection, _, _ = build(SourceBatch(TOSEC, [first, second]))
        self.assertEqual(
            connection.execute("SELECT label, part FROM disks").fetchall(), [("D-Bug 100 B", "B")]
        )

    def test_an_undeclared_series_is_logged_and_still_written(self) -> None:
        record = DiskRecord("tosec", "amiga", "pack", "made-up-pack", 3)
        connection, _, logged = build(SourceBatch(TOSEC, [record]))
        self.assertEqual(
            connection.execute("SELECT label FROM disks").fetchone()[0], "Made Up Pack 3"
        )
        self.assertTrue(any("made-up-pack" in line for line in logged))


class ContentsTest(unittest.TestCase):
    def test_entries_with_one_title_but_another_kind_or_note_are_all_kept(self) -> None:
        record = automation_250(
            "atari-legend",
            contents=[
                ContentRecord("Xenon", "game"),
                ContentRecord("Xenon", "doc", extra="[doc]"),
                ContentRecord("Xenon", "cheat", extra="[cheat code]"),
                ContentRecord("XENON", "game"),
            ],
        )
        connection, _, _ = build(SourceBatch(LEGEND, [record], priority=10))
        rows = connection.execute("SELECT title, kind, extra FROM contents ORDER BY position")
        self.assertEqual(
            rows.fetchall(),
            [("Xenon", "game", ""), ("Xenon", "doc", "[doc]"), ("Xenon", "cheat", "[cheat code]")],
        )
        found = connection.execute(
            "SELECT rowid FROM disk_fts WHERE disk_fts MATCH 'contents : cheat'"
        ).fetchall()
        self.assertEqual(found, [(1,)])


class LocationTest(unittest.TestCase):
    def build_with(self, *locations: LocationRecord):
        disk = automation_250(
            "tosec",
            images=[image("Automation Menu Disk 250 (1990)(Automation).st", "dead", sha1="beef")],
        )
        only = DiskRecord("internet-archive", "atari-st", "menu", locations=list(locations))
        return build(SourceBatch(TOSEC, [disk]), SourceBatch(ARCHIVE, [only]))

    def test_locations_attach_by_name_or_hash(self) -> None:
        connection, stats, _ = self.build_with(
            LocationRecord(
                "internet-archive",
                "https://ia/1.zip",
                container="zip",
                image_name="AUTOMATION MENU DISK 250 (1990)(AUTOMATION).ST",
            ),
            LocationRecord(
                "internet-archive",
                "https://ia/2.zip",
                image_name="Automation Menu Disk 250 (1990)(Automation).msa",
            ),
            LocationRecord("internet-archive", "https://ia/3", hash_kind="sha1", hash_value="BEEF"),
        )
        rows = connection.execute(
            "SELECT disk_id, image_id, url FROM locations ORDER BY url"
        ).fetchall()
        self.assertEqual(
            rows, [(1, 1, "https://ia/1.zip"), (1, 1, "https://ia/2.zip"), (1, 1, "https://ia/3")]
        )
        self.assertEqual(stats["with locations"], 1)
        self.assertEqual(stats["unmatched locations"], 0)

    def test_unmatched_locations_are_counted_and_never_make_a_disk(self) -> None:
        connection, stats, logged = self.build_with(
            LocationRecord("internet-archive", "https://ia/x", image_name="Nothing.st")
        )
        self.assertEqual(connection.execute("SELECT COUNT(*) FROM disks").fetchone()[0], 1)
        self.assertEqual(connection.execute("SELECT COUNT(*) FROM locations").fetchone()[0], 0)
        self.assertEqual(stats["unmatched locations"], 1)
        self.assertTrue(any("1 locations match no catalogue image" in line for line in logged))


class TitledLocationTest(unittest.TestCase):
    def test_a_titled_archive_that_names_no_image_is_its_own_disk(self) -> None:
        titled = DiskRecord(
            "amigascne",
            "amiga",
            "pack",
            title="Scene Pack 7",
            locations=[LocationRecord("amigascne", "https://scene/sp7.dms", container="dms")],
        )
        connection, stats, _ = build(SourceBatch(ARCHIVE, [titled]))
        self.assertEqual(
            connection.execute("SELECT label, kind, platform FROM disks").fetchall(),
            [("Scene Pack 7", "pack", "amiga")],
        )
        self.assertEqual(connection.execute("SELECT COUNT(*) FROM locations").fetchone()[0], 1)
        self.assertEqual(stats["unmatched locations"], 0)

    def test_a_titled_pointer_to_an_unknown_image_is_dropped(self) -> None:
        titled = DiskRecord(
            "internet-archive",
            "amiga",
            "single",
            title="5th Gear [cr VF]",
            locations=[
                LocationRecord("internet-archive", "https://ia/x.zip", image_name="5th Gear.adf")
            ],
        )
        connection, stats, _ = build(SourceBatch(ARCHIVE, [titled]))
        self.assertEqual(connection.execute("SELECT COUNT(*) FROM disks").fetchone()[0], 0)
        self.assertEqual(stats["unmatched locations"], 1)


class SearchTextTest(unittest.TestCase):
    def test_joined_words_are_indexed_both_ways(self) -> None:
        self.assertEqual(search_text("R-Type & S.T.U.N."), "r type and s t u n rtype stun")


if __name__ == "__main__":
    unittest.main()

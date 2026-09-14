"""Joining records from several sources and writing the catalogue."""

from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from catalogue_builder.merge import (
    SourceBatch,
    distinct_words,
    merge_records,
    parse_date,
    write_catalogue,
)
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
from catalogue_builder.series import CrewChoice, GroupRegistry, SeriesDef, SeriesRegistry
from piratefinder.catalogue import schema
from piratefinder.catalogue.naming import search_text

TOSEC = SourceInfo("tosec", "TOSEC", "https://tosec.example")
LEGEND = SourceInfo("atari-legend", "Atari Legend", "https://al.example", "CC BY-NC-SA 4.0")
ARCHIVE = SourceInfo("internet-archive", "Internet Archive", "https://ia.example")
DBUG = SourceInfo("d-bug", "D-Bug", "https://dbug.example")
ZOO = SourceInfo("demozoo", "Demozoo", "https://zoo.example", "CC BY-NC-SA 4.0")
PICTURES = SourceInfo("pictures", "Pictures", "https://pictures.example", "CC BY 4.0")


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


def build(
    *batches: SourceBatch,
    series: SeriesRegistry | None = None,
    crew_choice: CrewChoice | None = None,
):
    connection = sqlite3.connect(":memory:")
    logged: list[str] = []
    result = merge_records(list(batches), series or registry(), logged.append)
    stats = write_catalogue(
        connection,
        result,
        meta={"built_at": "2026-09-12T10:00:00+00:00"},
        groups=GroupRegistry({"QTX": "Quartex"}, {}),
        log=logged.append,
        crew_choice=crew_choice or CrewChoice(0.9, 10),
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
        self.assertEqual(meta["schema_version"], str(schema.SCHEMA_VERSION))
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
            "SELECT l.disk_id, l.image_id, p.text || l.url FROM locations l "
            "JOIN address_prefix p ON p.id = l.url_prefix ORDER BY 3"
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
        self.assertTrue(any("1 locations match no catalogue disc" in line for line in logged))

    def test_a_location_with_a_hash_goes_only_to_the_image_with_that_hash(self) -> None:
        # An old TOSEC name that now belongs to another dump: the name matches
        # the catalogue image, but the hash (the dump the zip holds) does not,
        # so the location must not be attached to that image by its name.
        connection, stats, _ = self.build_with(
            LocationRecord(
                "internet-archive",
                "https://ia/old.zip",
                image_name="Automation Menu Disk 250 (1990)(Automation).st",
                hash_kind="sha1",
                hash_value="0ld",
            )
        )
        self.assertEqual(connection.execute("SELECT COUNT(*) FROM locations").fetchone()[0], 0)
        self.assertEqual(stats["unmatched locations"], 1)

    def test_a_renamed_image_is_found_by_the_hash_an_old_name_carries(self) -> None:
        connection, stats, _ = self.build_with(
            LocationRecord(
                "internet-archive",
                "https://ia/renamed.zip",
                image_name="Automation Compact Disk 250 (19xx)(Automation).st",
                hash_kind="sha1",
                hash_value="BEEF",
            )
        )
        # The image carries the hash, so the location does not repeat it.
        self.assertEqual(
            connection.execute("SELECT disk_id, image_id, hash_value FROM locations").fetchall(),
            [(1, 1, "")],
        )
        self.assertEqual(stats["unmatched locations"], 0)

    def test_a_location_keeps_a_hash_its_image_lacks(self) -> None:
        disk = automation_250(
            "tosec",
            images=[image("Automation Menu Disk 250 (1990)(Automation).st", "dead")],
            locations=[
                LocationRecord(
                    "amigascne",
                    "https://scene/a250.st",
                    image_name="Automation Menu Disk 250 (1990)(Automation).st",
                    hash_kind="crc32",
                    hash_value="0BADF00D",
                ),
                LocationRecord(
                    "amigascne",
                    "https://scene/a250b.st",
                    image_name="Automation Menu Disk 250 (1990)(Automation).st",
                    hash_kind="md5",
                    hash_value="DEAD",
                ),
            ],
        )
        connection, _stats, _ = build(SourceBatch(TOSEC, [disk]))
        self.assertEqual(
            connection.execute(
                "SELECT image_id, hash_kind, hash_value FROM locations ORDER BY id"
            ).fetchall(),
            [(1, "crc32", "0badf00d"), (1, "", "")],
        )


SCENE = SourceInfo("amigascne", "amigascne", "https://scene.example")
GLENZ = "https://scene.example/Packdisks/MadElks/MadElks-2FuckTheGlenz10.dms"


def scene_pack(title: str, url: str, **fields: object) -> DiskRecord:
    """An amigascne pack: a title from the file name and the file as its location."""
    return DiskRecord(
        "amigascne",
        "amiga",
        "pack",
        title=title,
        locations=[LocationRecord("amigascne", url, priority=40)],
        **fields,
    )


def zoo_pack(title: str, url: str, **fields: object) -> DiskRecord:
    """A Demozoo pack: its members, and the file Demozoo names as its download."""
    return DiskRecord(
        "demozoo",
        "amiga",
        "pack",
        title=title,
        contents=[ContentRecord("Glenz Intro", "intro"), ContentRecord("Vector Balls", "demo")],
        locations=[LocationRecord("amigascne", url, priority=60)],
        **fields,
    )


class SharedDownloadTest(unittest.TestCase):
    """Records that name the same download are one disk, unless their numbers disagree."""

    def test_a_pack_and_the_file_it_names_are_one_disk(self) -> None:
        connection, stats, _ = build(
            SourceBatch(SCENE, [scene_pack("2 Fuck The Glenz 10 (Mad Elks)", GLENZ)], 90),
            SourceBatch(ZOO, [zoo_pack("2 Fuck da Glenz 10 (Mad Elks)", GLENZ.lower())], 35),
        )
        self.assertEqual(row(connection, "SELECT count(*) FROM disks"), (1,))
        titles = rows(connection, "SELECT title FROM contents ORDER BY position")
        self.assertEqual(titles, [("Glenz Intro",), ("Vector Balls",)])
        # One download, spelt as the archive's own index spells it.
        self.assertEqual(
            rows(
                connection,
                "SELECT coalesce(p.text, '') || l.url FROM locations l "
                "LEFT JOIN address_prefix p ON p.id = l.url_prefix",
            ),
            [(GLENZ,)],
        )
        self.assertEqual(stats["download merges"], 1)

    def test_a_download_of_another_issue_is_not_joined(self) -> None:
        url = "https://scene.example/Packdisks/BadTaste/BadTaste2.dms"
        connection, stats, _ = build(
            SourceBatch(SCENE, [scene_pack("Bad Taste 2", url)], 90),
            SourceBatch(ZOO, [zoo_pack("Bad Taste 4", url)], 35),
        )
        self.assertEqual(row(connection, "SELECT count(*) FROM disks"), (2,))
        self.assertEqual((stats["download merges"], stats["download merges refused"]), (0, 1))

    def test_a_file_with_a_dump_joins_the_keyed_disk_that_names_it(self) -> None:
        url = "https://scene.example/Packdisks/Automation/Auto250.adf"
        keyed = DiskRecord(
            "demozoo",
            "atari-st",
            "menu",
            "automation",
            250,
            contents=[ContentRecord("Necron")],
            locations=[LocationRecord("amigascne", url)],
        )
        dumped = DiskRecord(
            "amigascne",
            "atari-st",
            "menu",
            title="Auto 250",
            images=[image("Auto250.st", "ab12")],
            locations=[LocationRecord("amigascne", url)],
        )
        connection, _, _ = build(SourceBatch(ZOO, [keyed], 35), SourceBatch(SCENE, [dumped], 90))
        self.assertEqual(
            row(connection, "SELECT count(*), max(label) FROM disks"), (1, "Automation 250")
        )
        self.assertEqual(row(connection, "SELECT count(*) FROM images"), (1,))


class AttachOnlyTest(unittest.TestCase):
    """Keyed records that may join a disc but never make one (short menu zip names)."""

    def zip_of(self, number: int, part: str = "") -> DiskRecord:
        return DiskRecord(
            "internet-archive",
            "atari-st",
            "menu",
            series_key="automation",
            number=number,
            part=part,
            attach_only=True,
            locations=[LocationRecord("internet-archive", f"https://ia/A{number}{part}.zip")],
        )

    def test_an_attach_only_record_joins_the_disc_with_its_key(self) -> None:
        disk = automation_250("tosec", images=[image("Automation 250.st", "dead")])
        connection, stats, logged = build(
            SourceBatch(TOSEC, [disk]),
            SourceBatch(ARCHIVE, [self.zip_of(250), self.zip_of(251), self.zip_of(250, "B")]),
        )
        self.assertEqual(
            connection.execute("SELECT id, label FROM disks").fetchall(), [(1, "Automation 250")]
        )
        self.assertEqual(
            connection.execute(
                "SELECT l.disk_id, l.image_id, p.text || l.url FROM locations l "
                "JOIN address_prefix p ON p.id = l.url_prefix"
            ).fetchall(),
            [(1, None, "https://ia/A250.zip")],
        )
        self.assertEqual(stats["unmatched locations"], 2)
        self.assertIn("merge: 2 attach-only records name a disc no other source has", logged)

    def test_attach_only_records_alone_make_no_disc(self) -> None:
        connection, stats, _ = build(SourceBatch(ARCHIVE, [self.zip_of(250), self.zip_of(250)]))
        self.assertEqual(connection.execute("SELECT COUNT(*) FROM disks").fetchone()[0], 0)
        self.assertEqual(stats["unmatched locations"], 2)


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

    def test_notes_and_scroll_texts_are_indexed_a_word_once(self) -> None:
        self.assertEqual(distinct_words("hi hi skid row hi row greetings"), "hi skid row greetings")
        scroll = "SKID ROW PRESENTS COMPACT 31 ... SKID ROW SKID ROW ... greetings to FAIRLIGHT"
        disk = automation_250("tosec", images=[image("a.st", "dead")], menu_text=scroll)
        connection, _stats, _ = build(SourceBatch(TOSEC, [disk]))
        for word in ("skid", "fairlight", "greetings"):
            self.assertEqual(matches(connection, "entry_fts", "notes", word), ["Automation 250"])
        # The scroll text itself is kept as written for the details pane.
        self.assertEqual(connection.execute("SELECT menu_text FROM disks").fetchone()[0], scroll)


def single(source: str, title: str, platform: str = "atari-st", **fields: object) -> DiskRecord:
    return DiskRecord(source=source, platform=platform, kind="single", title=title, **fields)


def row(connection: sqlite3.Connection, sql: str, *parameters: object) -> tuple:
    return connection.execute(sql, parameters).fetchone()


def rows(connection: sqlite3.Connection, sql: str, *parameters: object) -> list[tuple]:
    return connection.execute(sql, parameters).fetchall()


class DiskColumnsTest(unittest.TestCase):
    def test_category_and_crew_come_from_the_archive_layout(self) -> None:
        records = [
            automation_250("tosec", contents=[ContentRecord("Xenon", "game")]),
            single("tosec", "Xenon [cr QTX]", cracker="Quartex", publisher="Melbourne House"),
            single("tosec", "Tetris", publisher="Mirrorsoft", contents=[ContentRecord("Tetris")]),
            single("tosec", "Nobody Knows"),
            DiskRecord("tosec", "amiga", "pack", title="Some Pack"),
            single("tosec", "Tune", contents=[ContentRecord("Tune", "music")]),
        ]
        connection, _, _ = build(SourceBatch(TOSEC, records))
        found = {
            label: (category, crew)
            for label, category, crew in rows(connection, "SELECT label, category, crew FROM disks")
        }
        self.assertEqual(
            found,
            {
                "Automation 250": ("Games", "Automation"),
                "Xenon [cr QTX]": ("Games", "Quartex"),
                "Tetris": ("Games", "Mirrorsoft"),
                "Nobody Knows": ("Games", "Unknown crew"),
                "Some Pack": ("Demos", "Unknown crew"),
                "Tune": ("Music", "Unknown crew"),
            },
        )

    def test_one_spelling_of_a_crew_on_each_platform(self) -> None:
        records = [
            single("tosec", "A [cr FLD]", "amiga", cracker="Flashlight Design"),
            single("tosec", "B [cr FLD]", "amiga", cracker="Flashlight Design"),
            single("tosec", "C", "amiga", cracker="Flash Light Design"),
            single("tosec", "D", "amiga", cracker="the flash-light design"),
            single("tosec", "E", "atari-st", cracker="Flash Light Design"),
            # A compilation's publisher is a tag like a crack's.
            DiskRecord("tosec", "atari-st", "pack", title="Some Pack", publisher="QTX"),
        ]
        connection, stats, _ = build(SourceBatch(TOSEC, records))
        crews = dict(rows(connection, "SELECT label, crew FROM disks"))
        self.assertEqual(
            crews,
            {
                "A [cr FLD]": "Flashlight Design",
                "B [cr FLD]": "Flashlight Design",
                "C": "Flashlight Design",
                "D": "Flashlight Design",
                "E": "Flash Light Design",  # another platform keeps its own
                "Some Pack": "Quartex",
            },
        )
        self.assertEqual(stats["crew spellings joined"], 2)
        # The written spelling is searchable on every disk it was joined for.
        found = rows(
            connection,
            "SELECT d.label FROM disk_fts f JOIN disks d ON d.id = f.rowid "
            "WHERE disk_fts MATCH 'crew : flashlight' ORDER BY d.label",
        )
        self.assertEqual([label for (label,) in found], ["A [cr FLD]", "B [cr FLD]", "C", "D"])

    def test_the_most_precise_release_date_wins(self) -> None:
        batches = [
            SourceBatch(TOSEC, [automation_250("tosec", date="1990")], priority=90),
            SourceBatch(LEGEND, [automation_250("atari-legend", release_date="1990-06")], 10),
            SourceBatch(DBUG, [automation_250("d-bug", release_date="1990-06-17")], priority=15),
        ]
        connection, stats, _ = build(*batches)
        self.assertEqual(
            row(connection, "SELECT year, month, day, date FROM disks"), (1990, 6, 17, "1990")
        )
        self.assertEqual((stats["with year"], stats["with month"], stats["with day"]), (1, 1, 1))

    def test_equal_precision_goes_to_the_source_first_by_priority(self) -> None:
        batches = [
            SourceBatch(DBUG, [automation_250("d-bug", release_date="1990-07")], priority=15),
            SourceBatch(LEGEND, [automation_250("atari-legend", release_date="1990-05")], 10),
        ]
        connection, _, _ = build(*batches)
        self.assertEqual(row(connection, "SELECT year, month, day FROM disks"), (1990, 5, None))

    def test_without_a_release_date_the_date_text_is_read(self) -> None:
        records = [
            single("tosec", "A", date="1989-06"),
            single("tosec", "B", date="19xx"),
            single("tosec", "C", date="1991-xx-xx"),
        ]
        connection, _, _ = build(SourceBatch(TOSEC, records))
        found = {r[0]: r[1:] for r in rows(connection, "SELECT label, year, month, day FROM disks")}
        self.assertEqual(
            found, {"A": (1989, 6, None), "B": (None, None, None), "C": (1991, None, None)}
        )

    def test_parse_date(self) -> None:
        cases = {
            "1989": (1989, None, None),
            "1989-06": (1989, 6, None),
            "1989-06-17": (1989, 6, 17),
            "1989-06-xx": (1989, 6, None),
            "1989-13": (1989, None, None),
            "19xx": (None, None, None),
            "198x": (None, None, None),
            "": (None, None, None),
            "0000": (None, None, None),
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(parse_date(text), expected)

    def test_numbered_disks_come_first_in_id_order_with_sort_titles(self) -> None:
        records = [
            single("tosec", "Chaos Engine, The (1993)(Renegade)"),
            automation_250("tosec"),
            DiskRecord("tosec", "atari-st", "menu", "automation", 9),
            DiskRecord("tosec", "atari-st", "menu", "automation", 10),
            single("tosec", "Alpha", platform="amiga"),
        ]
        connection, _, _ = build(SourceBatch(TOSEC, records))
        self.assertEqual(
            rows(connection, "SELECT id, label, sort_title FROM disks ORDER BY id"),
            [
                (1, "Automation 9", "automation 00000009"),
                (2, "Automation 10", "automation 00000010"),
                (3, "Automation 250", "automation 00000250"),
                (4, "Alpha", "alpha"),
                (5, "The Chaos Engine", "chaos engine"),
            ],
        )


class ImageVirusTest(unittest.TestCase):
    def test_virus_columns_are_written_and_completed_across_sources(self) -> None:
        first = automation_250(
            "tosec",
            images=[
                image("a.st", "aa", virus="Ghost", flags="[v Ghost]"),
                image("b.st", "bb", virus_damage=True, bad=True, flags="[b virus damage]"),
                image("c.st", "cc", antivirus="Protector IV", flags="[m Protector IV]"),
            ],
        )
        second = automation_250("atari-legend", images=[image("a.msa", "AA", sha512="1" * 128)])
        # Atari Legend comes first by priority; TOSEC completes its image with the flag.
        connection, stats, _ = build(
            SourceBatch(TOSEC, [first]), SourceBatch(LEGEND, [second], priority=10)
        )
        self.assertEqual(
            rows(
                connection,
                "SELECT md5, virus, virus_damage, antivirus, bad FROM images ORDER BY md5",
            ),
            [("aa", "Ghost", 0, "", 0), ("bb", "", 1, "", 1), ("cc", "", 0, "Protector IV", 0)],
        )
        self.assertEqual((stats["images with virus"], stats["images with virus damage"]), (1, 1))
        self.assertEqual(stats["images with antivirus"], 1)


def matches(connection: sqlite3.Connection, table: str, column: str, word: str) -> list:
    """Entry titles (or disk ids) whose ``column`` holds ``word``."""
    if table == "disk_fts":
        sql = "SELECT rowid FROM disk_fts WHERE disk_fts MATCH ? ORDER BY rowid"
    else:
        sql = (
            "SELECT e.title FROM entry_fts JOIN entries e ON e.id = entry_fts.rowid "
            "WHERE entry_fts MATCH ? ORDER BY e.id"
        )
    return [r[0] for r in rows(connection, sql, f'{column} : "{word}"')]


class EntriesTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        legend = automation_250(
            "atari-legend",
            release_date="1990-06",
            credits="Menu by The Lost Boys",
            menu_text="Greetings to the Replicants",
            contents=[
                ContentRecord(
                    "Xenon 2 - Megablast",
                    publisher="Image Works",
                    cracker="QTX",
                    extra="[doc] aka Megablaster",
                ),
                ContentRecord("Chaos Engine, The", "game"),
            ],
            images=[image("a250.msa", "aa")],
            locations=[
                LocationRecord("atari-legend", "https://al/1.zip", member="Menus/Auto 250.msa")
            ],
        )
        dbug = automation_250(
            "d-bug",
            contents=[
                ContentRecord("Xenon II", cracker="Vapour"),
                ContentRecord("Bonus Game"),
                ContentRecord("Chaos Strikes Back"),  # one shared word is not enough
            ],
        )
        tosec = automation_250("tosec", title="Automation Menu Disk 250 (1990)(Automation)")
        empty = single("tosec", "Lonely Crack [cr QTX]", platform="amiga", cracker="QTX")
        cls.connection, cls.stats, _ = build(
            SourceBatch(LEGEND, [legend], priority=10),
            SourceBatch(DBUG, [dbug], priority=15),
            SourceBatch(TOSEC, [tosec, empty], priority=90),
        )

    def test_one_entry_per_shown_title_and_one_for_a_disk_without_titles(self) -> None:
        self.assertEqual(
            rows(
                self.connection,
                "SELECT e.id, d.label, e.content_id IS NULL, e.title, e.sort_title, e.kind "
                "FROM entries e JOIN disks d ON d.id = e.disk_id ORDER BY e.id",
            ),
            [
                (1, "Automation 250", 0, "Xenon 2 - Megablast", "xenon 00000002 megablast", "game"),
                (2, "Automation 250", 0, "The Chaos Engine", "chaos engine", "game"),
                (3, "Lonely Crack [cr QTX]", 1, "Lonely Crack [cr QTX]", "lonely crack cr qtx", ""),
            ],
        )
        content_ids = rows(
            self.connection, "SELECT content_id FROM entries WHERE content_id IS NOT NULL"
        )
        self.assertEqual(
            content_ids, rows(self.connection, "SELECT id FROM contents ORDER BY position")
        )
        self.assertEqual(self.stats["entries"], 3)

    def test_titles_other_sources_give_join_the_title_they_stand_for(self) -> None:
        megablast = ["Xenon 2 - Megablast"]
        self.assertEqual(matches(self.connection, "entry_fts", "title", "megablaster"), megablast)
        self.assertEqual(matches(self.connection, "entry_fts", "title", "ii"), megablast)
        self.assertEqual(matches(self.connection, "entry_fts", "crew", "vapour"), megablast)
        self.assertEqual(matches(self.connection, "entry_fts", "title", "bonus"), [])
        self.assertEqual(matches(self.connection, "entry_fts", "title", "strikes"), [])
        self.assertEqual(
            matches(self.connection, "entry_fts", "notes", "strikes"),
            ["Xenon 2 - Megablast", "The Chaos Engine"],
        )
        self.assertEqual(
            matches(self.connection, "entry_fts", "notes", "bonus"),
            ["Xenon 2 - Megablast", "The Chaos Engine"],
        )

    def test_every_column_of_the_title_index(self) -> None:
        both = ["Xenon 2 - Megablast", "The Chaos Engine"]
        lonely = ["Lonely Crack [cr QTX]"]
        cases = [
            ("disk", "auto", both),
            ("disk", "menu", both),  # the TOSEC name
            ("crew", "automation", both),
            ("crew", "quartex", ["Xenon 2 - Megablast", *lonely]),
            ("crew", "qtx", ["Xenon 2 - Megablast", *lonely]),
            ("crew", "works", ["Xenon 2 - Megablast"]),
            ("people", "lost", both),
            ("facets", "atari", both),
            ("facets", "compact", both),
            ("facets", "games", [*both, *lonely]),
            ("facets", "1990", both),
            ("facets", "commodore", lonely),
            ("facets", "single", lonely),
            ("files", "a250", both),
            ("files", "msa", both),
            ("files", "auto", both),
            ("notes", "replicants", both),
            ("notes", "doc", ["Xenon 2 - Megablast"]),
        ]
        for column, word, expected in cases:
            with self.subTest(column=column, word=word):
                self.assertEqual(matches(self.connection, "entry_fts", column, word), expected)
        phrase = rows(
            self.connection,
            "SELECT COUNT(*) FROM entry_fts WHERE entry_fts MATCH 'facets : \"1990 06\"'",
        )
        self.assertEqual(phrase, [(2,)])

    def test_the_disk_index_has_the_crew_facets_and_files(self) -> None:
        for column, word in (("crew", "vapour"), ("facets", "compact"), ("files", "a250")):
            with self.subTest(column=column):
                self.assertEqual(matches(self.connection, "disk_fts", column, word), [1])
        self.assertEqual(matches(self.connection, "disk_fts", "contents", "bonus"), [1])


def awesome_registry() -> SeriesRegistry:
    """An Atari ST menu series of the crew Awesome, as Atari Legend has it."""
    return SeriesRegistry(
        {
            "awesome": SeriesDef(
                "awesome",
                "Awesome",
                "atari-st",
                "menu",
                group="Awesome",
                label="Awesome {number}",
            )
        }
    )


def awesome_menu(source: str, number: int, **fields: object) -> DiskRecord:
    return DiskRecord(source, "atari-st", "menu", "awesome", number, **fields)


def crew_of(connection: sqlite3.Connection, label: str) -> tuple | None:
    """The crews row (name, platform, notes, source) a disk points at, or None."""
    return row(
        connection,
        "SELECT c.name, c.platform, c.notes, c.source FROM disks d "
        "LEFT JOIN crews c ON c.id = d.crew_id WHERE d.label = ?",
        label,
    )


class CrewsTest(unittest.TestCase):
    def test_crew_records_join_the_crews_disks_carry(self) -> None:
        legend = SourceBatch(
            LEGEND,
            [automation_250("atari-legend")],
            priority=10,
            crews=[
                CrewRecord(
                    "Automation",
                    "atari-legend",
                    "History A.",
                    ["Rob", "Sharaz"],
                    url="u1",
                    id="13",
                    platforms={"atari-st": 300},
                ),
                CrewRecord("Nobody", "atari-legend", "Not on any disk.", platforms={"atari-st": 1}),
            ],
        )
        zoo = SourceBatch(
            ZOO,
            [single("demozoo", "Xenon [cr QTX]", cracker="Quartex")],
            priority=35,
            crews=[
                CrewRecord(
                    "AUTOMATION",
                    "demozoo",
                    "History B.",
                    ["Sharaz", "Jek"],
                    founded="1988",
                    wikipedia="Automation (group)",
                    id="60",
                    platforms={"atari-st": 12},
                ),
                CrewRecord("QTX", "demozoo", "ST crackers.", id="7", platforms={"atari-st": 2}),
                # Released nowhere Demozoo knows of, so it describes no disk.
                CrewRecord("The Automation", "demozoo", "Nowhere.", id="8"),
            ],
        )
        connection, stats, logged = build(zoo, legend)
        self.assertEqual(
            rows(connection, "SELECT * FROM crews ORDER BY name"),
            [
                (
                    1,
                    "Automation",
                    "atari-st",
                    "History A.\n\nHistory B.",
                    "Rob, Sharaz, Jek",
                    "1988",
                    "atari-legend, demozoo",
                    "u1",
                    "Automation (group)",
                ),
                (2, "Quartex", "atari-st", "ST crackers.", "", "", "demozoo", "", ""),
            ],
        )
        self.assertEqual(
            rows(connection, "SELECT label, crew, crew_id FROM disks ORDER BY id"),
            [("Automation 250", "Automation", 1), ("Xenon [cr QTX]", "Quartex", 2)],
        )
        self.assertEqual(
            (stats["crews"], stats["crews unmatched"], stats["with crew history"]), (2, 2, 2)
        )
        self.assertIn("merge: 2 crew records describe the crew of no disk", logged)

    def test_crew_names_agree_without_the_article_ampersand_or_apostrophe(self) -> None:
        tosec = SourceBatch(
            TOSEC,
            [
                single(
                    "tosec", "Game [cr TRSI]", platform="amiga", cracker="Tristar & Red Sector Inc"
                ),
                single("tosec", "Other [cr Droogs]", platform="amiga", cracker="Droogs"),
                single("tosec", "Third [cr Rebels]", platform="amiga", cracker="Rebels"),
            ],
        )
        zoo = SourceBatch(
            ZOO,
            [],
            priority=35,
            crews=[
                CrewRecord(name, "demozoo", f"About {name}", id=str(number), platforms={"amiga": 1})
                for number, name in enumerate(
                    ("Tristar and Red Sector Inc.", "The Droog's", "The Rebels")
                )
            ],
        )
        connection, _stats, _logged = build(tosec, zoo)
        self.assertEqual(
            rows(
                connection,
                "SELECT d.crew, c.notes FROM disks d JOIN crews c ON c.id = d.crew_id ORDER BY d.crew",
            ),
            [
                ("Droogs", "About The Droog's"),
                ("Rebels", "About The Rebels"),
                ("Tristar & Red Sector Inc", "About Tristar and Red Sector Inc."),
            ],
        )

    def test_a_crew_of_the_same_name_on_another_platform_is_not_the_disks_crew(self) -> None:
        # Awesome made Atari ST menus; Demozoo's Awesome is an unrelated Amiga
        # demo group. A join by name alone gives the ST menus its history.
        tosec = SourceBatch(
            TOSEC,
            [
                awesome_menu("tosec", 1),
                single("tosec", "Awesome Demo", platform="amiga", publisher="Awesome"),
            ],
        )
        zoo = SourceBatch(
            ZOO,
            [],
            priority=35,
            crews=[
                CrewRecord(
                    "Awesome", "demozoo", "An Amiga demo group.", id="1454", platforms={"amiga": 40}
                )
            ],
        )
        connection, stats, _logged = build(tosec, zoo, series=awesome_registry())
        self.assertEqual(crew_of(connection, "Awesome 1"), (None, None, None, None))
        self.assertEqual(
            crew_of(connection, "Awesome Demo"),
            ("Awesome", "amiga", "An Amiga demo group.", "demozoo"),
        )
        legend = SourceBatch(
            LEGEND,
            [awesome_menu("atari-legend", 1, crew_ids=["40"])],
            priority=10,
            crews=[
                CrewRecord(
                    "Awesome",
                    "atari-legend",
                    "An ST menu crew.",
                    id="40",
                    platforms={"atari-st": 36},
                )
            ],
        )
        connection, stats, _logged = build(tosec, zoo, legend, series=awesome_registry())
        self.assertEqual(
            crew_of(connection, "Awesome 1"),
            ("Awesome", "atari-st", "An ST menu crew.", "atari-legend"),
        )
        self.assertEqual(
            crew_of(connection, "Awesome Demo"),
            ("Awesome", "amiga", "An Amiga demo group.", "demozoo"),
        )
        self.assertEqual(stats["crews"], 2)

    def test_the_credited_crew_wins_over_a_namesake_on_the_same_platform(self) -> None:
        # Two Demozoo groups called Awesome released on the ST. A disk takes
        # the one its Demozoo record credits; without a credit it takes
        # neither, but still the one Atari Legend crew of that name.
        tosec = SourceBatch(TOSEC, [awesome_menu("tosec", number) for number in (1, 2, 3)])
        zoo = SourceBatch(
            ZOO,
            [
                awesome_menu("demozoo", 1, crew_ids=["2"]),
                # A credit for a crew of another name tells nothing about Awesome.
                awesome_menu("demozoo", 3, crew_ids=["9"]),
            ],
            priority=35,
            crews=[
                CrewRecord("Awesome", "demozoo", "Group one.", id="1", platforms={"atari-st": 3}),
                CrewRecord("Awesome", "demozoo", "Group two.", id="2", platforms={"atari-st": 8}),
                CrewRecord("Elite", "demozoo", "Another crew.", id="9", platforms={"atari-st": 5}),
            ],
        )
        legend = SourceBatch(
            LEGEND,
            [],
            priority=10,
            crews=[
                CrewRecord(
                    "Awesome", "atari-legend", "The ST crew.", id="40", platforms={"atari-st": 3}
                )
            ],
        )
        connection, stats, logged = build(tosec, zoo, legend, series=awesome_registry())
        self.assertEqual(
            crew_of(connection, "Awesome 1"),
            ("Awesome", "atari-st", "The ST crew.\n\nGroup two.", "atari-legend, demozoo"),
        )
        for label in ("Awesome 2", "Awesome 3"):
            self.assertEqual(
                crew_of(connection, label),
                ("Awesome", "atari-st", "The ST crew.", "atari-legend"),
            )
        self.assertEqual((stats["crews"], stats["crews ambiguous"]), (2, 2))
        self.assertTrue(any("Awesome (atari-st, demozoo, 2)" in line for line in logged))


def paradox_disks(*crews: CrewRecord):
    """Three Amiga cracks by Paradox, and Demozoo's crews of that name."""
    tosec = SourceBatch(
        TOSEC,
        [
            single("tosec", f"Game {n} (1990)(Soft)[cr PDX]", platform="amiga", cracker="Paradox")
            for n in (1, 2, 3)
        ],
    )
    return tosec, SourceBatch(ZOO, [], priority=35, crews=list(crews))


def paradox(crew_id: str, releases: int) -> CrewRecord:
    return CrewRecord(
        "Paradox", "demozoo", f"Group {crew_id}.", id=crew_id, platforms={"amiga": releases}
    )


class SameNameCrewsTest(unittest.TestCase):
    """Choosing one of a source's several crews of a name when no record credits one."""

    def notes(self, connection: sqlite3.Connection) -> set:
        return {
            value
            for (value,) in rows(
                connection,
                "SELECT COALESCE(c.notes, '') FROM disks d LEFT JOIN crews c ON c.id = d.crew_id",
            )
        }

    def test_a_crew_with_most_of_the_releases_wins(self) -> None:
        # 265 of 266 releases: the well-known Paradox, not the one-release namesake.
        connection, stats, _ = build(*paradox_disks(paradox("1853", 265), paradox("66103", 1)))
        self.assertEqual(self.notes(connection), {"Group 1853."})
        self.assertEqual((stats["crews dominant"], stats["crews ambiguous"]), (3, 0))

    def test_the_share_and_the_count_must_both_be_enough(self) -> None:
        for crews in (
            (paradox("1", 85), paradox("2", 15)),  # 85 percent is not enough
            (paradox("1", 9), paradox("2", 0)),  # all of 9 releases is not enough
        ):
            connection, stats, _ = build(*paradox_disks(*crews))
            self.assertEqual(self.notes(connection), {""}, crews)
            self.assertEqual((stats["crews dominant"], stats["crews ambiguous"]), (0, 3))
        # 10 of 12 releases is not enough; exactly 90 percent (18 of 20) is.
        connection, stats, _ = build(*paradox_disks(paradox("1", 10), paradox("2", 2)))
        self.assertEqual(self.notes(connection), {""})
        connection, stats, _ = build(*paradox_disks(paradox("1", 18), paradox("2", 2)))
        self.assertEqual(self.notes(connection), {"Group 1."})

    def test_a_pin_names_the_crew_and_wins_over_the_dominant_one(self) -> None:
        choice = CrewChoice(0.9, 10, {("paradox", "amiga", "demozoo"): "2"})
        connection, stats, logged = build(
            *paradox_disks(paradox("1", 50), paradox("2", 5)), crew_choice=choice
        )
        self.assertEqual(self.notes(connection), {"Group 2."})
        self.assertEqual((stats["crews pinned"], stats["crews dominant"]), (3, 0))
        self.assertFalse(any("crew pins that chose no crew" in line for line in logged))

    def test_a_pin_that_chooses_no_crew_is_logged(self) -> None:
        choice = CrewChoice(0.9, 10, {("paradox", "amiga", "demozoo"): "404"})
        connection, stats, logged = build(
            *paradox_disks(paradox("1", 5), paradox("2", 5)), crew_choice=choice
        )
        self.assertEqual(self.notes(connection), {""})
        self.assertIn("merge: crew pins that chose no crew: paradox (amiga, demozoo 404)", logged)

    def test_a_crew_pinned_twice_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "pins.toml"
            pin = '[[pin]]\nname = "{}"\nplatform = "amiga"\nsource = "demozoo"\nid = "{}"\n'
            path.write_text(
                "dominant_share = 0.9\ndominant_releases = 10\n"
                + pin.format("Paradox", "1")
                + pin.format("The Paradox", "2")
            )
            with self.assertRaisesRegex(ValueError, "pinned twice"):
                CrewChoice.load(path)

    def test_the_pins_file(self) -> None:
        choice = CrewChoice.load()
        self.assertEqual((choice.dominant_share, choice.dominant_releases), (0.9, 10))
        self.assertEqual(choice.pinned({"supreme"}, "amiga", "demozoo"), "44901")
        self.assertIsNone(choice.pinned({"supreme"}, "atari-st", "demozoo"))


class MediaTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        legend = automation_250(
            "atari-legend",
            contents=[ContentRecord("Rick Dangerous"), ContentRecord("Xenon")],
            media=[
                MediaRecordIn("menu", "https://al/menu.png", rank=10),
                MediaRecordIn("snap", "https://al/rick.png", content_title="rick dangerous"),
                MediaRecordIn("snap", "https://al/none.png", content_title="Not Listed"),
            ],
            images=[image("Automation 250.st", "aa")],
        )
        rick_st = single(
            "tosec", "Rick Dangerous [cr X]", contents=[ContentRecord("Rick Dangerous")]
        )
        rick_amiga = single(
            "tosec", "Rick Dangerous", platform="amiga", contents=[ContentRecord("Rick Dangerous")]
        )
        pictures = [
            DiskRecord(
                "pictures",
                "atari-st",
                "single",
                media=[
                    MediaRecordIn("boxart", "https://p/rick.png", title_key="Rick Dangerous"),
                    MediaRecordIn(
                        "title",
                        "https://p/rick-title.png",
                        source="other",
                        title_key="rick dangerous",
                    ),
                    MediaRecordIn("boxart", "https://p/rick.png", title_key="Rick Dangerous"),
                    MediaRecordIn("menu", "https://p/a250.png", image_name="automation 250.ST"),
                    MediaRecordIn("snap", "https://p/lost.png", title_key="Nothing Like It"),
                    MediaRecordIn("snap", "https://p/lost2.png", image_name="nothing.st"),
                ],
            )
        ]
        cls.connection, cls.stats, cls.logged = build(
            SourceBatch(LEGEND, [legend], priority=10),
            SourceBatch(TOSEC, [rick_st, rick_amiga]),
            SourceBatch(PICTURES, pictures),
        )

    def media(self) -> list[tuple]:
        return rows(
            self.connection,
            "SELECT d.label, c.title, m.kind, u.text || m.url, mc.source, m.rank FROM media m "
            "JOIN address_prefix u ON u.id = m.url_prefix "
            "JOIN media_credit mc ON mc.id = m.credit_id "
            "JOIN disks d ON d.id = m.disk_id LEFT JOIN contents c ON c.id = m.content_id "
            "ORDER BY m.id",
        )

    def test_media_only_records_never_make_a_disk(self) -> None:
        self.assertEqual(self.stats["disks"], 3)

    def test_pictures_attach_to_discs_and_titles(self) -> None:
        self.assertEqual(
            self.media(),
            [
                ("Automation 250", None, "menu", "https://al/menu.png", "atari-legend", 10),
                (
                    "Automation 250",
                    "Rick Dangerous",
                    "snap",
                    "https://al/rick.png",
                    "atari-legend",
                    100,
                ),
                ("Automation 250", None, "menu", "https://p/a250.png", "pictures", 100),
                (
                    "Automation 250",
                    "Rick Dangerous",
                    "boxart",
                    "https://p/rick.png",
                    "pictures",
                    100,
                ),
                (
                    "Rick Dangerous [cr X]",
                    "Rick Dangerous",
                    "boxart",
                    "https://p/rick.png",
                    "pictures",
                    100,
                ),
                ("Rick Dangerous [cr X]", None, "boxart", "https://p/rick.png", "pictures", 100),
                (
                    "Automation 250",
                    "Rick Dangerous",
                    "title",
                    "https://p/rick-title.png",
                    "other",
                    100,
                ),
                (
                    "Rick Dangerous [cr X]",
                    "Rick Dangerous",
                    "title",
                    "https://p/rick-title.png",
                    "other",
                    100,
                ),
                ("Rick Dangerous [cr X]", None, "title", "https://p/rick-title.png", "other", 100),
            ],
        )

    def test_title_keys_leave_out_brackets_and_versions(self) -> None:
        preview = single(
            "tosec", "Paperboy (Preview)", contents=[ContentRecord("Paperboy (Preview) v1.2")]
        )
        picture = DiskRecord(
            "pictures",
            "atari-st",
            "single",
            media=[MediaRecordIn("title", "https://p/paperboy.png", title_key="paperboy")],
        )
        connection, _, _ = build(SourceBatch(TOSEC, [preview]), SourceBatch(PICTURES, [picture]))
        self.assertEqual(
            rows(
                connection,
                "SELECT m.content_id IS NULL, u.text || m.url FROM media m "
                "JOIN address_prefix u ON u.id = m.url_prefix ORDER BY m.id",
            ),
            [(0, "https://p/paperboy.png"), (1, "https://p/paperboy.png")],
        )

    def test_unplaced_pictures_are_counted_and_logged(self) -> None:
        self.assertEqual(self.stats["media unmatched"], 3)
        self.assertEqual(self.stats["media"], 9)
        self.assertTrue(
            any("3 media records match no disk or title" in line for line in self.logged)
        )
        self.assertTrue(
            any("'other', which is not in the sources table" in line for line in self.logged)
        )


class TriviaTest(unittest.TestCase):
    def test_facts_notes_and_wikipedia_articles(self) -> None:
        legend = automation_250(
            "atari-legend",
            contents=[ContentRecord("Rick Dangerous"), ContentRecord("Xenon")],
            trivia=[
                TriviaRecordIn("note", "Two versions exist.", licence="CC BY-NC-SA 4.0"),
                TriviaRecordIn("fact", "Rick was drawn by hand.", content_title="Rick Dangerous"),
                TriviaRecordIn("fact", "Lost fact.", content_title="Nobody"),
                TriviaRecordIn("note", "Two versions exist."),
            ],
        )
        dbug = automation_250(
            "d-bug",
            contents=[ContentRecord("Xenon", links=[("wikipedia", "Xenon (video game)")])],
        )
        notes = DiskRecord(
            "pictures",
            "atari-st",
            "single",
            trivia=[TriviaRecordIn("fact", "Sold well.", content_title="xenon")],
        )
        connection, stats, _ = build(
            SourceBatch(LEGEND, [legend], priority=10),
            SourceBatch(DBUG, [dbug], priority=15),
            SourceBatch(PICTURES, [notes]),
        )
        self.assertEqual(
            rows(
                connection,
                "SELECT c.title, t.kind, t.text, t.source, t.url, t.licence FROM trivia t "
                "LEFT JOIN contents c ON c.id = t.content_id ORDER BY t.id",
            ),
            [
                (None, "note", "Two versions exist.", "atari-legend", "", "CC BY-NC-SA 4.0"),
                ("Rick Dangerous", "fact", "Rick was drawn by hand.", "atari-legend", "", ""),
                (
                    "Xenon",
                    "wikipedia",
                    "Xenon (video game)",
                    "d-bug",
                    "https://en.wikipedia.org/wiki/Xenon_%28video_game%29",
                    "",
                ),
                ("Xenon", "fact", "Sold well.", "pictures", "", ""),
            ],
        )
        self.assertEqual((stats["trivia"], stats["trivia unmatched"]), (4, 1))

    def test_an_article_is_kept_once_per_disk_on_the_game(self) -> None:
        legend = automation_250(
            "atari-legend",
            notes="Menu by Vapour.",
            contents=[
                ContentRecord("Rick Dangerous", "doc", extra="[doc]"),
                ContentRecord("Rick Dangerous", "game"),
            ],
            trivia=[
                TriviaRecordIn("note", "Menu by Vapour."),  # already the disk's notes
                TriviaRecordIn("note", "Menu by Vapour, second version."),
            ],
        )
        other = single("tosec", "Rick Dangerous [cr X]", contents=[ContentRecord("Rick Dangerous")])
        article = DiskRecord(
            "wikidata",
            "atari-st",
            "single",
            trivia=[
                TriviaRecordIn(
                    "wikipedia",
                    "Rick Dangerous",
                    source="wikipedia",
                    licence="CC BY-SA 4.0",
                    content_title="rick dangerous",
                )
            ],
        )
        wikidata = SourceInfo("wikidata", "Wikidata", "https://wikidata.example", "CC0 1.0")
        connection, stats, logged = build(
            SourceBatch(LEGEND, [legend], priority=10),
            SourceBatch(TOSEC, [other]),
            SourceBatch(wikidata, [article], priority=95),
        )
        self.assertEqual(
            rows(
                connection,
                "SELECT d.label, c.kind, t.kind, t.text FROM trivia t "
                "JOIN disks d ON d.id = t.disk_id LEFT JOIN contents c ON c.id = t.content_id "
                "ORDER BY t.id",
            ),
            [
                ("Automation 250", None, "note", "Menu by Vapour, second version."),
                ("Automation 250", "game", "wikipedia", "Rick Dangerous"),
                ("Rick Dangerous [cr X]", "game", "wikipedia", "Rick Dangerous"),
            ],
        )
        self.assertEqual((stats["wikipedia repeats"], stats["trivia repeating disk notes"]), (1, 1))
        self.assertIn(
            ("wikipedia", "Wikipedia", "https://en.wikipedia.org/", "CC BY-SA 4.0", 2),
            rows(connection, "SELECT id, name, url, licence, records FROM sources"),
        )
        self.assertFalse(any("not in the sources table" in line for line in logged))


if __name__ == "__main__":
    unittest.main()

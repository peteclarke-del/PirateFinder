"""Tests for the Atari Legend importer: the dump reader and the disk records."""

from __future__ import annotations

import gzip
import io
import shutil
import tempfile
import unittest
from pathlib import Path

from catalogue_builder.context import BuildContext
from catalogue_builder.series import SeriesRegistry
from catalogue_builder.sources import atarilegend as al

FIXTURES = Path(__file__).parent / "fixtures" / "atarilegend"
DUMP = FIXTURES / "dump.sql"


def rows(text: str, tables: set[str]) -> list[tuple[str, dict]]:
    return list(al.iter_rows(io.StringIO(text), tables))


class DumpReaderTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tables = al.load_tables(DUMP)

    def test_strings_keep_escapes_quotes_and_line_breaks(self) -> None:
        disk = next(row for row in self.tables["menu_disks"] if row["id"] == 3219)
        self.assertEqual(
            disk["scrolltext"],
            "IT'S \"GREAT\"\nIT'S A BACKSLASH \\ AND\nA REAL LINE BREAK; (NOT THE END)",
        )
        self.assertEqual(disk["menu_disk_dump_id"], 1746)
        self.assertIsNone(disk["donated_by_individual_id"])

    def test_numbers_null_and_negative_values(self) -> None:
        dumps = {row["id"]: row for row in self.tables["menu_disk_dumps"]}
        self.assertEqual(dumps[1745]["user_id"], -1)
        self.assertEqual(dumps[1746]["size"], 799870)
        self.assertIsNone(dumps[1756]["user_id"])

    def test_every_row_of_multi_row_and_column_list_inserts(self) -> None:
        menus = {row["id"]: row for row in self.tables["menus"]}
        self.assertEqual(sorted(menus), [208, 1000, 1001, 3041, 3042, 3043, 3056, 5000])
        self.assertEqual(menus[208]["version"], "bis")
        self.assertEqual(menus[1000]["date"], "1991-01-26")

    def test_skipped_table_text_never_reads_as_rows(self) -> None:
        # The news table holds a line that looks like an INSERT into menus.
        self.assertNotIn(99, [row["id"] for row in self.tables["menus"]])

    def test_escaped_quote_in_a_set_name(self) -> None:
        names = [row["name"] for row in self.tables["menu_sets"]]
        self.assertIn("Bob's Menus", names)

    def test_gzip_dump_is_read_whatever_its_name(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            packed = Path(folder) / "dump.sql"
            packed.write_bytes(gzip.compress(DUMP.read_bytes()))
            self.assertEqual(al.load_tables(packed), self.tables)

    def test_value_count_mismatch_is_an_error(self) -> None:
        text = "CREATE TABLE `t` (\n  `a` int,\n  `b` int\n) ENGINE=InnoDB;\n"
        text += "INSERT INTO `t` VALUES (1,2),(3);\n"
        with self.assertRaises(al.DumpError):
            rows(text, {"t"})

    def test_hex_and_charset_introducers(self) -> None:
        text = "CREATE TABLE `t` (\n  `a` blob,\n  `b` text\n) ENGINE=InnoDB;\n"
        text += "INSERT INTO `t` VALUES (0x4869,_binary 'x');\n"
        self.assertEqual(rows(text, {"t"}), [("t", {"a": "Hi", "b": "x"})])


class ListingTest(unittest.TestCase):
    def test_newest_full_dump_is_chosen(self) -> None:
        listing = (FIXTURES / "database-dumps.html").read_text()
        self.assertEqual(al.latest_dump_name(listing), "2026-09-06.sql.gz")

    def test_no_dump_listed(self) -> None:
        self.assertIsNone(al.latest_dump_name("<pre>2026-09-06-images.zip</pre>"))


class MappingTest(unittest.TestCase):
    def test_versions_follow_tosec(self) -> None:
        self.assertEqual(al.map_version("1"), "")
        self.assertEqual(al.map_version("1.0"), "")
        self.assertEqual(al.map_version(None), "")
        self.assertEqual(al.map_version("2"), "2")
        self.assertEqual(al.map_version("bis"), "2")
        self.assertEqual(al.map_version("ter"), "3")
        self.assertEqual(al.map_version("fix"), "fix")

    def test_parts_become_positions_unless_letters(self) -> None:
        self.assertEqual(al.map_parts(["_1", "_2", "_3", "_4A", "_4B"]), list("ABCDE"))
        self.assertEqual(al.map_parts(["A", "b"]), ["A", "B"])
        self.assertEqual(al.map_parts([None]), [""])
        self.assertEqual(al.map_parts(["B"]), ["B"])
        self.assertEqual(al.map_parts(["phreaking"]), ["PHREAKING"])
        self.assertEqual(al.map_parts(["demo", "game"]), ["DEMO", "GAME"])
        self.assertEqual(al.map_parts([None, "phreaking", "A", "B"]), ["", "PHREAKING", "A", "B"])
        self.assertEqual(al.map_parts(["part I", "part II A"]), ["A", "B"])
        self.assertEqual(al.map_parts([None, ".5"]), ["", "B"])
        self.assertEqual(al.map_parts(["I v1", "I v2"]), ["A", "B"])
        self.assertEqual(al.map_parts(["_2"]), [""])
        self.assertEqual(al.map_parts(["A", "a"]), ["A", "B"])  # never the same part twice

    def test_conditions(self) -> None:
        self.assertEqual(al.map_condition("Missing"), "missing")
        self.assertEqual(al.map_condition("Intro only or partially damaged"), "intro only")
        self.assertEqual(al.map_condition("Slightly damaged"), "damaged")
        self.assertEqual(al.map_condition("Intact"), "intact")
        self.assertEqual(al.map_condition(None), "")

    def test_site_order_and_page_links(self) -> None:
        menus = {index: {"number": index, "issue": None, "version": None} for index in range(1, 25)}
        menus[0] = {"number": None, "issue": None, "version": None}
        disks = [{"id": 100 + index, "menu_id": index, "part": None} for index in range(25)]
        ascending = al.site_order(list(reversed(disks)), menus, descending=False)
        self.assertEqual([disk["id"] for disk in ascending[:3]], [100, 101, 102])
        self.assertEqual(al.disk_url(7, 101, 0), f"{al.SITE}/menusets/7#menudisk-101")
        self.assertEqual(al.disk_url(7, 120, 20), f"{al.SITE}/menusets/7?page=2#menudisk-120")
        descending = al.site_order(disks, menus, descending=True)
        self.assertEqual(descending[0]["id"], 124)
        self.assertEqual(descending[-1]["id"], 100)  # NULL sorts last when descending

    def test_parts_sort_within_a_menu(self) -> None:
        menus = {1: {"number": 1, "issue": None, "version": None}}
        disks = [
            {"id": 3, "menu_id": 1, "part": "b"},
            {"id": 1, "menu_id": 1, "part": "A"},
            {"id": 2, "menu_id": 1, "part": None},
        ]
        self.assertEqual([d["id"] for d in al.site_order(disks, menus, False)], [2, 1, 3])


class RecordsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.messages: list[str] = []
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.registry = SeriesRegistry.load()
        self.ctx = BuildContext(
            cache_dir=Path(self.folder.name),
            series=self.registry,
            offline=True,
            inputs={al.INFO.id: DUMP},
            log=self.messages.append,
        )
        self.records = list(al.collect(self.ctx))
        self.by_disk = {
            record.links[0][1].rsplit("-", 1)[-1]: record for record in self.records if record.links
        }

    def disk(self, disk_id: int):
        return self.by_disk[str(disk_id)]

    def test_one_record_per_disk(self) -> None:
        self.assertEqual(len(self.records), 12)
        self.assertTrue(any("12 disks" in message for message in self.messages))

    def test_pompey_pirates_1_first_version(self) -> None:
        record = self.disk(3219)
        self.assertEqual(record.key, ("pompey-pirates", 1, "", ""))
        self.assertEqual(record.source, "atari-legend")
        self.assertEqual(record.platform, "atari-st")
        self.assertEqual(record.title, "")  # numbered disks take their title from TOSEC
        self.assertEqual(record.condition, "intact")
        self.assertEqual(record.publisher, "Pompey Pirates")
        self.assertIn("A REAL LINE BREAK", record.menu_text)
        [image] = record.images
        self.assertEqual((image.name, image.format, image.size), ("1746.msa", "msa", 799870))
        self.assertEqual(image.sha512, "a0aa0e26c2839776")
        [location] = record.locations
        self.assertEqual(location.url, f"{al.SITE}/storage/zips/menus/1746.zip")
        self.assertEqual(
            (location.provider, location.container, location.member), (al.INFO.id, "zip", "")
        )
        self.assertEqual((location.hash_kind, location.hash_value), ("sha512", "a0aa0e26c2839776"))
        self.assertEqual(location.page_url, f"{al.SITE}/menusets/155#menudisk-3219")
        self.assertEqual(location.image_name, "1746.msa")
        self.assertEqual(location.priority, 30)
        self.assertIn(("Atari Legend", location.page_url), record.links)
        self.assertIn(
            ("Demozoo: Synth Dream", "https://demozoo.org/productions/69534/"), record.links
        )

    def test_contents_in_menu_order_with_kinds_and_credits(self) -> None:
        contents = self.disk(3219).contents
        self.assertEqual(
            [(item.title, item.kind) for item in contents],
            [
                ("New Zealand Story, The", "game"),
                ("Rick Dangerous", "game"),
                ("Stack Up", "doc"),
                ("Ripper", "utility"),
                ("New Zealand Story, The", "cheat"),
                ("Synth Dream", "demo"),
            ],
        )
        story, rick, stack, _ripper, cheat, demo = contents
        self.assertEqual(story.publisher, "Ocean")  # from the game's commercial release
        self.assertEqual(rick.cracker, "Pompey Pirates")
        self.assertEqual(
            rick.extra,
            "(1 MB) trainer: Infinite Lives aka Rick Dangerous 1; Rick Dangereux; Rick D",
        )
        self.assertEqual(stack.extra, "[doc]")
        self.assertEqual(cheat.extra, "[cheat code]")
        self.assertEqual(demo.version, "2.1")

    def test_other_titles_are_searchable_notes(self) -> None:
        self.assertEqual(
            self.disk(3219).notes,
            "Rick Dangerous is also known as Rick Dangerous 1, Rick Dangereux, Rick D.",
        )

    def test_second_version_missing_disk(self) -> None:
        record = self.disk(3220)
        self.assertEqual(record.key, ("pompey-pirates", 1, "", "v2"))
        self.assertEqual(record.condition, "missing")
        self.assertEqual(record.notes, "Needs\nfixing")
        self.assertEqual((record.images, record.locations), ([], []))

    def test_multi_disk_menu_parts_follow_tosec_disk_numbers(self) -> None:
        parts = [self.disk(disk_id).key for disk_id in range(3233, 3238)]
        self.assertEqual(parts, [("pompey-pirates", 13, part, "") for part in "ABCDE"])
        [image] = self.disk(3233).images
        self.assertEqual((image.name, image.format), ("1756.st", "st"))

    def test_shared_set_is_split_by_number(self) -> None:
        self.assertEqual(self.disk(1744).key, ("flame-of-finland", 54, "", ""))
        self.assertEqual(self.disk(1745).key, ("superior", 55, "", ""))
        self.assertEqual(self.disk(1744).date, "1991-01-26")
        self.assertEqual(self.disk(1744).condition, "damaged")
        self.assertEqual(self.disk(1744).notes, "Donated to Atari Legend by Marcer.")
        self.assertEqual(self.disk(1745).publisher, "Flame of Finland / Superior")

    def test_fake_automation_menus_stay_out_of_automation(self) -> None:
        record = self.disk(223)
        self.assertNotEqual(record.series_key, "automation")
        self.assertEqual(record.key[1:], (22, "", "v2"))
        self.assertEqual(record.condition, "intro only")

    def test_unnumbered_menus_have_no_key(self) -> None:
        self.assertIsNone(self.disk(3218).key)
        self.assertEqual(self.disk(3218).series_key, "pompey-pirates")
        record = self.disk(6000)
        self.assertIsNone(record.key)
        self.assertEqual(record.title, "Bob's Menus B")

    def test_unknown_set_is_registered_as_a_series(self) -> None:
        series = self.registry.get(self.disk(6000).series_key)
        self.assertIsNotNone(series)
        self.assertEqual(
            (series.name, series.platform, series.kind), ("Bob's Menus", "atari-st", "menu")
        )


class RuleNumberTest(unittest.TestCase):
    def test_a_number_captured_by_a_rule_replaces_the_menu_number(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            (Path(folder) / "series.toml").write_text(
                '[[series]]\nid = "later"\nname = "Later"\nplatform = "atari-st"\nkind = "menu"\n'
                '[[match]]\nseries = "later"\nsource = "atari-legend"\n'
                "patterns = ['^Old Set #1(?P<number>[0-9]{2})$']\n"
            )
            registry = SeriesRegistry.load(Path(folder))
        tables = {
            "menu_sets": [{"id": 1, "name": "Old Set", "menus_sort": "asc"}],
            "menus": [
                {"id": 1, "number": 105, "issue": None, "version": None, "menu_set_id": 1},
                {"id": 2, "number": 7, "issue": None, "version": None, "menu_set_id": 1},
            ],
            "menu_disks": [{"id": 10, "menu_id": 1}, {"id": 11, "menu_id": 2}],
        }
        keys = sorted(record.key for record in al.build_records(tables, registry))
        self.assertEqual(keys, [("later", 5, "", ""), ("old-set", 7, "", "")])


class CollectTest(unittest.TestCase):
    def test_newest_dump_is_taken_from_the_listing(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            ctx = BuildContext(
                cache_dir=Path(folder),
                series=SeriesRegistry.load(),
                offline=True,
                log=lambda m: None,
            )
            listing = ctx.cache_path(al.DUMPS_URL, "database-dumps.html")
            listing.parent.mkdir(parents=True)
            shutil.copyfile(FIXTURES / "database-dumps.html", listing)
            dump = ctx.cache_path(al.DUMPS_URL + "2026-09-06.sql.gz")
            dump.write_bytes(gzip.compress(DUMP.read_bytes()))
            self.assertEqual(len(list(al.collect(ctx))), 12)


if __name__ == "__main__":
    unittest.main()

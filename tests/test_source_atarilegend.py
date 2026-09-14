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


class DetailsTest(unittest.TestCase):
    """Pictures, facts, notes, dates and reference ids for the details pane."""

    def setUp(self) -> None:
        self.messages: list[str] = []
        self.registry = SeriesRegistry.load()
        tables = al.load_tables(DUMP)
        records = al.build_records(
            tables, self.registry, log=self.messages.append, articles={"2": "Rick Dangerous"}
        )
        self.by_disk = {record.links[0][1].rsplit("-", 1)[-1]: record for record in records}

    def test_menu_screenshots_are_disc_pictures(self) -> None:
        record = self.by_disk["3219"]
        menus = [item for item in record.media if item.kind == "menu"]
        self.assertEqual(
            [(item.url, item.rank) for item in menus],
            [
                (f"{al.SITE}/storage/images/menu_screenshots/1886.png", 10),
                (f"{al.SITE}/storage/images/menu_screenshots/1887.bmp", 11),
            ],
        )
        first = menus[0]
        self.assertEqual((first.source, first.content_title), ("atari-legend", ""))
        self.assertEqual(first.page_url, f"{al.SITE}/menusets/155#menudisk-3219")
        self.assertIn("Atari Legend", first.credit)
        self.assertIn("CC BY-NC-SA 4.0", first.credit)

    def test_a_file_that_is_not_a_picture_is_left_out(self) -> None:
        self.assertEqual(self.by_disk["3220"].media, [])  # its only screenshot is a zip

    def test_game_screenshots_are_title_pictures_three_at_most(self) -> None:
        record = self.by_disk["3219"]
        rick = [item for item in record.media if item.content_title == "Rick Dangerous"]
        self.assertEqual(
            [item.url.rsplit("/", 1)[-1] for item in rick], ["319.png", "320.png", "321.jpg"]
        )
        self.assertEqual({(item.kind, item.rank) for item in rick}, {("snap", 30)})
        self.assertEqual(rick[0].page_url, f"{al.SITE}/games/rick-dangerous")

    def test_a_game_listed_twice_on_a_disk_gets_its_pictures_once(self) -> None:
        # The New Zealand Story is on disk 3219 as a game and as a cheat.
        record = self.by_disk["3219"]
        story = [m for m in record.media if m.content_title == "New Zealand Story, The"]
        self.assertEqual(len(story), 1)

    def test_facts_are_plain_text_title_trivia(self) -> None:
        facts = [note for note in self.by_disk["3219"].trivia if note.kind == "fact"]
        self.assertEqual(
            [note.text for note in facts],
            [
                "A re-release of Westphaser.",
                "See the story (https://example.org/rick) and https://example.org/.\n\n"
                "Cheat: type POOKIE",
            ],
        )
        self.assertEqual({note.content_title for note in facts}, {"Rick Dangerous"})
        self.assertEqual({note.licence for note in facts}, {"CC BY-NC-SA 4.0"})
        self.assertEqual(facts[0].url, f"{al.SITE}/games/rick-dangerous")

    def test_disk_notes_are_disc_trivia(self) -> None:
        [note] = self.by_disk["3220"].trivia
        self.assertEqual((note.kind, note.text, note.content_title), ("note", "Needs\nfixing", ""))
        self.assertEqual(note.source, "atari-legend")

    def test_wikipedia_article_by_game_id(self) -> None:
        record = self.by_disk["3219"]
        [article] = [note for note in record.trivia if note.kind == "wikipedia"]
        self.assertEqual(
            (article.text, article.content_title), ("Rick Dangerous", "Rick Dangerous")
        )
        self.assertEqual(article.url, "https://en.wikipedia.org/wiki/Rick_Dangerous")
        self.assertEqual(article.licence, "CC BY-SA 4.0")
        rick = next(c for c in record.contents if c.title == "Rick Dangerous")
        self.assertEqual(rick.links, [("atari-legend-game", "2"), ("wikipedia", "Rick Dangerous")])

    def test_reference_ids_of_titles(self) -> None:
        links = {c.title: c.links for c in self.by_disk["3219"].contents}
        self.assertEqual(links["Stack Up"], [("atari-legend-game", "3")])
        self.assertEqual(links["Synth Dream"], [("demozoo", "69534")])
        self.assertEqual(links["Ripper"], [])

    def test_release_date_from_the_menu_date(self) -> None:
        self.assertEqual(self.by_disk["1744"].release_date, "1991-01-26")
        self.assertEqual(self.by_disk["1745"].release_date, "")
        self.assertEqual(al.release_date("1990-00-00"), "1990")
        self.assertEqual(al.release_date("1990-07-00"), "1990-07")
        self.assertEqual(al.release_date("0000-00-00"), "")
        self.assertEqual(al.release_date(None), "")

    def test_counts_are_logged(self) -> None:
        self.assertTrue(any("2 menu screenshots, 4 game screenshots" in m for m in self.messages))

    def test_without_the_wikidata_answer_the_dump_still_reads(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            messages: list[str] = []
            ctx = BuildContext(
                cache_dir=Path(folder),
                series=SeriesRegistry.load(),
                offline=True,
                inputs={al.INFO.id: DUMP},
                log=messages.append,
            )
            records = list(al.collect(ctx))
        self.assertEqual(len(records), 12)
        self.assertFalse(any(n.kind == "wikipedia" for r in records for n in r.trivia))
        self.assertTrue(any("no Wikipedia articles" in message for message in messages))


class PlainTextTest(unittest.TestCase):
    def test_bbcode(self) -> None:
        self.assertEqual(al.plain_text("[url=http://x.org]x.org[/url]"), "x.org (http://x.org)")
        self.assertEqual(al.plain_text("[url]http://x.org[/url]"), "http://x.org")
        self.assertEqual(al.plain_text("[code]A\r\n  B[/code]"), "A\n  B")
        self.assertEqual(al.plain_text("[u][b]Hi[/b][/u] [list][*]one[/list]"), "Hi one")
        self.assertEqual(al.plain_text(None), "")


class CrewTest(unittest.TestCase):
    def test_crews_from_the_dump(self) -> None:
        messages: list[str] = []
        crews = list(
            al.crew_records(al.load_tables(DUMP), SeriesRegistry.load(), log=messages.append)
        )
        by_name = {crew.name: crew for crew in crews}
        self.assertEqual(sorted(by_name), ["Pompey Pirates", "The Lonely Crew"])
        pompey = by_name["Pompey Pirates"]
        self.assertEqual(pompey.notes, "Founded in Portsmouth.\nIt's true.")
        self.assertEqual(pompey.members, ["Marcer", "Alien (Big Al)"])  # once each
        self.assertEqual(pompey.url, f"{al.SITE}/menusets/155")
        self.assertEqual(pompey.source, "atari-legend")
        # The site's crew id, and eight menu disks and one game release on the ST.
        self.assertEqual((pompey.id, pompey.platforms), ("7", {"atari-st": 9}))
        self.assertEqual(by_name["The Lonely Crew"].url, "")
        # The site covers the ST only, so a crew it credits with nothing is
        # still an ST crew.
        self.assertEqual(by_name["The Lonely Crew"].platforms, {"atari-st": 0})
        self.assertTrue(any("2 crews, 2 with a history" in message for message in messages))

    def test_a_crew_is_named_as_its_menus_series_group(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            (Path(folder) / "series.toml").write_text(
                '[[series]]\nid = "medway-boys"\nname = "Medway Boys"\nplatform = "atari-st"\n'
                'kind = "menu"\ngroup = "Medway Boys"\naliases = ["the medway boys"]\n'
                '[[series]]\nid = "other"\nname = "Other"\nplatform = "atari-st"\n'
                'kind = "menu"\ngroup = "Wild Copiers"\n'
            )
            registry = SeriesRegistry.load(Path(folder))
        tables = {
            "menu_sets": [{"id": 1, "name": "The Medway Boys", "menus_sort": "asc"}],
            "menus": [{"id": 1, "number": 1, "issue": None, "version": None, "menu_set_id": 1}],
            "menu_disks": [{"id": 10, "menu_id": 1}],
            "crews": [
                {"id": 5, "name": "The Medway Boys", "history": "From Kent."},
                {"id": 6, "name": "The Wild Copiers", "history": "No menus here."},
                {"id": 7, "name": "Nobody", "history": None},
                {"id": 8, "name": "Medway Boys", "history": "Another crew of the name."},
            ],
            "crew_menu_set": [{"crew_id": 5, "menu_set_id": 1}],
        }
        crews = [(crew.name, crew.id, crew.notes) for crew in al.crew_records(tables, registry)]
        # Two crews of one name stay two records: the merge tells them apart
        # by the crew ids the menu disks list.
        self.assertEqual(
            crews,
            [
                ("Medway Boys", "5", "From Kent."),
                ("Wild Copiers", "6", "No menus here."),
                ("Medway Boys", "8", "Another crew of the name."),
            ],
        )
        [medway] = [crew for crew in al.crew_records(tables, registry) if crew.id == "5"]
        self.assertEqual(medway.url, f"{al.SITE}/menusets/1")
        [record] = al.build_records(tables, registry)
        self.assertEqual(record.crew_ids, ["5"])

    def test_collect_crews_reads_the_dump(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            ctx = BuildContext(
                cache_dir=Path(folder),
                series=SeriesRegistry.load(),
                offline=True,
                inputs={al.INFO.id: DUMP},
                log=lambda m: None,
            )
            self.assertEqual(len(list(al.collect_crews(ctx))), 2)

    def test_menu_disks_list_the_crews_of_their_set(self) -> None:
        records = list(al.build_records(al.load_tables(DUMP), SeriesRegistry.load()))
        by_set = {record.links[0][1].split("#")[0]: record.crew_ids for record in records}
        self.assertEqual(by_set[al.set_url(155)], ["7"])
        self.assertEqual(by_set[al.set_url(89)], ["8", "9"])
        self.assertEqual(by_set[al.set_url(13)], [])


def to_sql(tables: dict[str, list[dict]]) -> str:
    """A MariaDB-style dump of ``tables``, as Atari Legend's export writes them."""

    def value(item: object) -> str:
        if item is None:
            return "NULL"
        if isinstance(item, (int, float)):
            return repr(item)
        text = str(item).replace("\\", "\\\\").replace("'", "\\'")
        return "'" + text.replace("\n", "\\n").replace("\r", "\\r") + "'"

    lines = []
    for table, rows in tables.items():
        columns = list(dict.fromkeys(column for row in rows for column in row))
        if not columns:
            continue
        lines.append(f"CREATE TABLE `{table}` (")
        lines += [f"  `{column}` text," for column in columns[:-1]]
        lines += [f"  `{columns[-1]}` text", ") ENGINE=InnoDB;"]
        values = ",".join(
            "(" + ",".join(value(row.get(column)) for column in columns) + ")" for row in rows
        )
        lines.append(f"INSERT INTO `{table}` VALUES {values};")
    return "\n".join(lines) + "\n"


def export_of_2026_09_13(tables: dict[str, list[dict]]) -> dict[str, list[dict]]:
    """The fixture's tables as the export of 2026-09-13 and later names them."""
    changed = {
        al.RENAMED_TABLES.get(new_name, new_name): [dict(row) for row in rows]
        for new_name, rows in tables.items()
    }
    changed = {
        {old: new for new, old in al.RENAMED_TABLES.items()}.get(name, name): rows
        for name, rows in changed.items()
    }
    for table, renames in al.RENAMED_COLUMNS.items():
        for row in changed.get(table, ()):
            for new_name, old_name in renames.items():
                row[new_name] = row.pop(old_name)
    dump_of_disk = {}
    for disk in changed["menu_disks"]:
        dump_id = disk.pop("menu_disk_dump_id")
        if dump_id is not None:
            dump_of_disk[dump_id] = disk["id"]
    for dump in changed["menu_disk_dumps"]:
        dump["menu_disk_id"] = dump_of_disk.get(dump["id"])
        dump["format"] = str(dump["format"]).lower()
    return changed


class ExportOf20260913Test(unittest.TestCase):
    """Atari Legend renamed tables and columns, and moved the dump link, on 2026-09-13."""

    def setUp(self) -> None:
        self.registry = SeriesRegistry.load()
        self.old = al.load_tables(DUMP)
        folder = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, folder)
        self.path = folder / "2026-09-13.sql"
        new_tables = export_of_2026_09_13(al.load_tables(DUMP))
        self.assertIn("companies", new_tables)
        self.assertNotIn("menu_disk_dump_id", new_tables["menu_disks"][0])
        self.path.write_text(to_sql(new_tables), encoding="utf-8")

    def test_the_newer_export_gives_the_same_disks(self) -> None:
        new = al.load_tables(self.path)
        before = list(al.build_records(self.old, self.registry))
        after = list(al.build_records(new, self.registry))
        self.assertEqual(after, before)
        self.assertTrue(any(record.locations for record in after))
        self.assertTrue(any(record.publisher for record in after))

    def test_the_newer_export_gives_the_same_crews(self) -> None:
        new = al.load_tables(self.path)
        self.assertEqual(
            list(al.crew_records(new, self.registry)),
            list(al.crew_records(self.old, self.registry)),
        )


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

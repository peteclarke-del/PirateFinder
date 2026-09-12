"""Query parsing and ranked catalogue search."""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from catalogue_builder.merge import SourceBatch, merge_records, write_catalogue
from catalogue_builder.records import ContentRecord, DiskRecord, ImageRecordIn, SourceInfo
from catalogue_builder.series import GroupRegistry, SeriesDef, SeriesRegistry
from piratefinder.catalogue.naming import normalise
from piratefinder.catalogue.search import parse_query, search_catalogue, tokens
from piratefinder.catalogue.store import Catalogue
from piratefinder.models import DiskKind, Platform, SearchFilters


def declared_aliases() -> dict[str, tuple[str, ...]]:
    """The aliases the real catalogue carries for the declared series."""
    aliases: dict[str, list[str]] = {}
    for series in SeriesRegistry.load().all():
        for alias in [series.name, *series.aliases]:
            aliases.setdefault(normalise(alias), []).append(series.id)
    return {alias: tuple(ids) for alias, ids in aliases.items()}


class ParseQueryTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.aliases = declared_aliases()

    def parse(self, text: str) -> tuple:
        parsed = parse_query(text, self.aliases)
        return parsed.series_ids, parsed.number, parsed.part, parsed.version, parsed.terms

    def test_disk_references(self) -> None:
        automation = (("automation",), 250, "", "", ())
        for text in (
            "automation 250",
            "Automation #250",
            "auto250",
            "a250",
            "a 250",
            "automation menu disk 250",
            "AUTOMATION 250",
        ):
            with self.subTest(text=text):
                self.assertEqual(self.parse(text), automation)
        for text in ("pp51", "PP 51", "pompey pirates 51", "Pompey Pirates Menu Disk 51"):
            with self.subTest(text=text):
                self.assertEqual(self.parse(text), (("pompey-pirates",), 51, "", "", ()))
        for text in ("d-bug 100b", "dbug 100 b", "D-Bug 100 part B", "db 100 disk 2"):
            with self.subTest(text=text):
                self.assertEqual(self.parse(text), (("d-bug",), 100, "B", "", ()))
        self.assertEqual(
            self.parse("skid row compact 128"), (("skid-row-compact",), 128, "", "", ())
        )
        self.assertEqual(self.parse("automation 100 v2"), (("automation",), 100, "", "v2", ()))
        self.assertEqual(self.parse("automation 100 v2.0"), (("automation",), 100, "", "v2", ()))
        # The first edition is stored unversioned.
        self.assertEqual(self.parse("automation 100 v1.0"), (("automation",), 100, "", "", ()))
        self.assertEqual(self.parse("medway 20"), (("medway-boys",), 20, "", "", ()))
        self.assertEqual(
            self.parse("fof 12 xenon"), (("flame-of-finland",), 12, "", "", ("xenon",))
        )

    def test_the_longest_alias_wins(self) -> None:
        aliases = {"skid row": ("skid-row-cracks",), "skid row compact": ("skid-row-compact",)}
        parsed = parse_query("skid row compact 128", aliases)
        self.assertEqual((parsed.series_ids, parsed.number), (("skid-row-compact",), 128))
        self.assertEqual(parse_query("skid row 12", aliases).series_ids, ("skid-row-cracks",))

    def test_free_text(self) -> None:
        self.assertEqual(self.parse("250"), ((), None, "", "", ("250",)))
        self.assertEqual(self.parse("Rick Dangerous"), ((), None, "", "", ("rick", "dangerous")))
        self.assertEqual(self.parse("Paperboy 2")[4], ("paperboy", "2"))
        # A series name without a number is still searched as words.
        self.assertEqual(self.parse("Elite"), (("elite",), None, "", "", ("elite",)))
        self.assertEqual(self.parse("V8 music")[3:], ("", ("v8", "music")))
        self.assertEqual(self.parse("xenon2")[4], ("xenon2",))

    def test_tokens(self) -> None:
        self.assertEqual(tokens("auto250"), ["auto", "250"])
        self.assertEqual(tokens("100b"), ["100", "b"])
        self.assertEqual(tokens("17bit intros"), ["17bit", "intros"])


def write_sample(path: Path) -> None:
    registry = SeriesRegistry(
        {
            "automation": SeriesDef(
                "automation", "Automation", "atari-st", "menu", aliases=["auto", "a"]
            ),
            "d-bug": SeriesDef("d-bug", "D-Bug", "atari-st", "menu", aliases=["dbug"]),
            "skid-row-compact": SeriesDef(
                "skid-row-compact",
                "Skid Row Compact",
                "amiga",
                "menu",
                aliases=["sr compact", "skid row"],
            ),
        }
    )
    source = SourceInfo("tosec", "TOSEC", "https://tosec.example")

    def menu(series: str, number: int, part: str = "", *titles: str) -> DiskRecord:
        platform = "amiga" if series == "skid-row-compact" else "atari-st"
        return DiskRecord(
            "tosec",
            platform,
            "menu",
            series,
            number,
            part,
            contents=[ContentRecord(title) for title in titles],
            images=[
                ImageRecordIn(f"{series}{number}{part}.st", "st", md5=f"{series}{number}{part}")
            ],
        )

    def single(title: str, platform: str = "atari-st") -> DiskRecord:
        return DiskRecord(
            "tosec",
            platform,
            "single",
            title=title,
            contents=[ContentRecord(title.split(" (")[0])],
            images=[ImageRecordIn(f"{title}.st", "st", md5=title)],
        )

    records = [
        menu("automation", 250, "", "Xenon 2 - Megablast", "Tetris"),
        menu("automation", 251, "", "Rick Dangerous"),
        menu("d-bug", 100, "A", "Chaos Engine, The"),
        menu("d-bug", 100, "B", "Chaos Engine, The"),
        menu("skid-row-compact", 128, "", "Paperboy 2", "Xenon"),
        single("Xenon (1988)(Melbourne House)"),
        single("Xenon 2 - Megablast (1989)(Image Works)", "amiga"),
        single("Rick Dangerous (1989)(Firebird)"),
        single("Paperboy 2 (1992)(Mindscape)", "amiga"),
        single("Chaos Engine 2, The (1996)(Renegade)(Disk 1 of 3)[cr LFC]", "amiga"),
        single("Chaos Engine, The (1993)(Renegade)(Disk 1 of 2)[cr Cynix]"),
    ]
    result = merge_records([SourceBatch(source, records)], registry)
    connection = sqlite3.connect(path)
    write_catalogue(
        connection,
        result,
        meta={"built_at": "2026-09-12T00:00:00+00:00"},
        groups=GroupRegistry({}, {}),
    )
    connection.commit()
    connection.close()


class SearchTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.folder = tempfile.TemporaryDirectory()
        path = Path(cls.folder.name) / "catalogue.sqlite"
        write_sample(path)
        cls.catalogue = Catalogue.open(path)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.catalogue.close()
        cls.folder.cleanup()

    def labels(self, text: str, filters: SearchFilters | None = None, limit: int = 500):
        hits = search_catalogue(self.catalogue, text, filters or SearchFilters(), limit)
        disks = self.catalogue.disks(hit.disk_id for hit in hits)
        return [disks[hit.disk_id].label for hit in hits]

    def test_a_disk_reference_comes_first(self) -> None:
        self.assertEqual(self.labels("Automation 250")[0], "Automation 250")
        self.assertEqual(self.labels("a251")[0], "Automation 251")
        self.assertEqual(self.labels("skid row compact 128")[0], "Skid Row Compact 128")

    def test_every_part_is_returned_unless_one_is_asked_for(self) -> None:
        self.assertEqual(self.labels("dbug 100")[:2], ["D-Bug 100 A", "D-Bug 100 B"])
        self.assertEqual(self.labels("dbug 100b")[0], "D-Bug 100 B")
        self.assertNotIn("D-Bug 100 A", self.labels("dbug 100b"))

    def test_words_after_a_disk_reference_search_the_text_too(self) -> None:
        labels = self.labels("automation 250 rick")
        self.assertEqual(labels[0], "Automation 250")
        self.assertIn("Rick Dangerous", labels)

    def test_full_text_ranks_the_title_first_and_reports_matched_contents(self) -> None:
        hits = search_catalogue(self.catalogue, "xenon", SearchFilters())
        disks = self.catalogue.disks(hit.disk_id for hit in hits)
        labels = [disks[hit.disk_id].label for hit in hits]
        self.assertEqual(labels[0], "Xenon")
        self.assertEqual(
            set(labels), {"Xenon", "Xenon 2 - Megablast", "Automation 250", "Skid Row Compact 128"}
        )
        matched = {disks[hit.disk_id].label: hit.matched for hit in hits}
        self.assertEqual(matched["Automation 250"], ("Xenon 2 - Megablast",))
        self.assertEqual(matched["Skid Row Compact 128"], ("Xenon",))

    def test_an_exact_title_beats_a_longer_one(self) -> None:
        self.assertEqual(
            self.labels("chaos engine")[0], "The Chaos Engine (Disk 1 of 2) [cr Cynix]"
        )
        self.assertEqual(
            self.labels("the chaos engine")[0], "The Chaos Engine (Disk 1 of 2) [cr Cynix]"
        )

    def test_prefixes_and_substrings(self) -> None:
        self.assertIn("Automation 250", self.labels("megab"))
        self.assertEqual(self.labels("paperboy2")[0], "Paperboy 2")
        self.assertIn("Automation 250", self.labels("egablas"))  # inside a word: trigram
        self.assertEqual(self.labels("nothing like this"), [])

    def test_filters_apply_to_every_kind_of_hit(self) -> None:
        amiga = SearchFilters(platform=Platform.AMIGA)
        self.assertEqual(
            set(self.labels("xenon", amiga)), {"Xenon 2 - Megablast", "Skid Row Compact 128"}
        )
        self.assertEqual(self.labels("automation 250", amiga), [])
        singles = SearchFilters(kinds=frozenset({DiskKind.SINGLE}))
        self.assertEqual(set(self.labels("xenon", singles)), {"Xenon", "Xenon 2 - Megablast"})
        self.assertEqual(len(self.labels("xenon", limit=2)), 2)


if __name__ == "__main__":
    unittest.main()

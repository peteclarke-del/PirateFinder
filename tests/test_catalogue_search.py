"""Query parsing and ranked catalogue search."""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from dataclasses import replace
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
from piratefinder.catalogue import search
from piratefinder.catalogue.naming import normalise
from piratefinder.catalogue.search import (
    Overrides,
    facets,
    parse_query,
    search_page,
    term_matches,
    tokens,
)
from piratefinder.catalogue.store import Catalogue
from piratefinder.models import (
    ContentKind,
    DiskKind,
    Platform,
    Query,
    ResultMode,
    SortOrder,
)


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


class DiskNameSearchTest(unittest.TestCase):
    """Discs found by ``search_page`` among TOSEC-style names with parts and sequels."""

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

    def page(self, text: str, **fields) -> search.CataloguePage:
        fields.setdefault("page_size", 500)
        query = Query(text=text, mode=ResultMode.DISCS, **fields)
        return search_page(self.catalogue, query)

    def labels(self, text: str, **fields) -> list[str]:
        return [self.catalogue.disk(row.disk_id).label for row in self.page(text, **fields).rows]

    def test_a_disk_reference_finds_its_disc(self) -> None:
        self.assertEqual(self.labels("Automation 250"), ["Automation 250"])
        self.assertEqual(self.labels("a251"), ["Automation 251"])
        self.assertEqual(self.labels("skid row compact 128"), ["Skid Row Compact 128"])

    def test_every_part_is_returned_unless_one_is_asked_for(self) -> None:
        self.assertEqual(self.labels("dbug 100"), ["D-Bug 100 A", "D-Bug 100 B"])
        self.assertEqual(self.labels("dbug 100b"), ["D-Bug 100 B"])

    def test_the_title_itself_ranks_first_and_matched_titles_are_reported(self) -> None:
        rows = self.page("xenon").rows
        labels = [self.catalogue.disk(row.disk_id).label for row in rows]
        self.assertEqual(
            labels, ["Xenon", "Skid Row Compact 128", "Xenon 2 - Megablast", "Automation 250"]
        )
        matched = dict(zip(labels, (row.matched for row in rows), strict=True))
        self.assertEqual(matched["Automation 250"], ("Xenon 2 - Megablast",))
        self.assertEqual(matched["Skid Row Compact 128"], ("Xenon",))

    def test_xenon_2_finds_the_sequel_first(self) -> None:
        for text in ("xenon 2", "xenon2"):
            with self.subTest(text=text):
                labels = self.labels(text)
                self.assertEqual(labels[:2], ["Xenon 2 - Megablast", "Automation 250"])
                self.assertNotIn("Xenon", labels)

    def test_an_exact_title_beats_a_longer_one(self) -> None:
        for text in ("chaos engine", "the chaos engine"):
            with self.subTest(text=text):
                labels = self.labels(text)
                self.assertEqual(labels[0], "The Chaos Engine (Disk 1 of 2) [cr Cynix]")
                self.assertEqual(labels[-1], "The Chaos Engine 2 (Disk 1 of 3) [cr LFC]")

    def test_prefixes_and_substrings(self) -> None:
        self.assertIn("Automation 250", self.labels("megab"))
        self.assertEqual(self.labels("paperboy2")[0], "Paperboy 2")
        self.assertIn("Automation 250", self.labels("egablas"))  # inside a word: substring index
        self.assertEqual(self.labels("nothing like this"), [])

    def test_filters_apply_to_every_kind_of_hit(self) -> None:
        self.assertEqual(
            set(self.labels("xenon", platform=Platform.AMIGA)),
            {"Xenon 2 - Megablast", "Skid Row Compact 128"},
        )
        self.assertEqual(self.labels("automation 250", platform=Platform.AMIGA), [])
        singles = frozenset({DiskKind.SINGLE})
        self.assertEqual(set(self.labels("xenon", kinds=singles)), {"Xenon", "Xenon 2 - Megablast"})
        page = self.page("xenon", page_size=2)
        self.assertEqual((len(page.rows), page.total), (2, 4))


def write_find_sample(path: Path) -> None:
    """A small catalogue for the Find screen: menus, a compact, singles and a pack."""
    registry = SeriesRegistry(
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
            "skid-row-compact": SeriesDef(
                "skid-row-compact",
                "Skid Row Compact",
                "amiga",
                "menu",
                group="Skid Row",
                aliases=["sr compact"],
            ),
            "lemmings-menu": SeriesDef(
                "lemmings-menu",
                "Lemmings",
                "atari-st",
                "menu",
                group="Lemmings Crew",
                aliases=["lemmings"],
            ),
        }
    )

    def menu(series: str, number: int, date: str, *titles: str, **fields) -> DiskRecord:
        platform = "amiga" if series == "skid-row-compact" else "atari-st"
        version = fields.pop("version", "")
        return DiskRecord(
            "tosec",
            platform,
            "menu",
            series,
            number,
            version=version,
            date=date,
            contents=[ContentRecord(title) for title in titles],
            images=[
                ImageRecordIn(
                    f"{series}{number}{version}.st", "st", md5=f"{series}{number}{version}"
                )
            ],
            **fields,
        )

    def single(title: str, platform: str, date: str, **fields) -> DiskRecord:
        name = title.split(" [")[0]
        return DiskRecord(
            "tosec",
            platform,
            "single",
            title=title,
            date=date,
            contents=[ContentRecord(name, publisher=fields.get("publisher", ""))],
            images=[ImageRecordIn(f"{title}.st", "st", md5=title)],
            **fields,
        )

    records = [
        menu("automation", 9, "1989", "Xenon", "Tetris", menu_text="See you all on disk 10 soon"),
        menu("automation", 10, "1990-06", "Rick Dangerous", "Chaos Engine, The"),
        menu(
            "automation",
            250,
            "1990",
            "Necron",
            "Xenon 2 - Megablast",
            locations=[
                LocationRecord(
                    "internet-archive", "https://ia/a250.zip", image_name="automation250.st"
                )
            ],
        ),
        menu("automation", 250, "1990", "Necron", version="v2"),
        menu("skid-row-compact", 128, "1991", "Paperboy 2", "Xenon"),
        menu("lemmings-menu", 2, "1992", "Civilization"),
        single("Lemmings 2", "amiga", "1993", publisher="Psygnosis"),
        single(
            "Rick Dangerous [cr Elite]", "atari-st", "1989", cracker="Elite", publisher="Firebird"
        ),
        single("Rick Dangerous 2", "amiga", "1990", publisher="Firebird"),
        single("Speedball", "amiga", "1989-06", publisher="Image Works"),
        single("The Chaos Engine", "atari-st", "1993", publisher="Renegade"),
        DiskRecord("tosec", "amiga", "pack", title="Some Demo Pack"),
    ]
    result = merge_records(
        [SourceBatch(SourceInfo("tosec", "TOSEC", "https://t"), records)], registry
    )
    connection = sqlite3.connect(path)
    write_catalogue(
        connection,
        result,
        meta={"built_at": "2026-09-12T00:00:00+00:00"},
        groups=GroupRegistry({}, {}),
    )
    connection.commit()
    connection.close()


class SearchPageTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.folder = tempfile.TemporaryDirectory()
        path = Path(cls.folder.name) / "catalogue.sqlite"
        write_find_sample(path)
        cls.catalogue = Catalogue.open(path)
        cls.ids = {disk.label: disk.id for disk in cls.catalogue.disks(range(1, 50)).values()}

    @classmethod
    def tearDownClass(cls) -> None:
        cls.catalogue.close()
        cls.folder.cleanup()

    def page(self, **fields) -> search.CataloguePage:
        local = fields.pop("local_disks", ())
        providers = fields.pop("providers", ())
        return search_page(self.catalogue, Query(**fields), local_disks=local, providers=providers)

    def label(self, disk_id: int) -> str:
        return self.catalogue.disk(disk_id).label

    def titles(self, **fields) -> list[tuple[str, str]]:
        """(disc label, title) of every row, over all pages."""
        return [
            (self.label(row.disk_id), row.title) for row in self.page(page_size=500, **fields).rows
        ]

    def discs(self, **fields) -> list[str]:
        return [
            self.label(row.disk_id)
            for row in self.page(mode=ResultMode.DISCS, page_size=500, **fields).rows
        ]

    def test_a_disc_number_is_not_widened_by_scroll_text(self) -> None:
        # Automation 9's scroll text mentions "disk 10"; only names may add rows.
        self.assertEqual(
            {label for label, _title in self.titles(text="automation 10")}, {"Automation 10"}
        )
        self.assertEqual(self.discs(text="automation 10"), ["Automation 10"])

    def test_titles_by_relevance_put_the_exact_title_first(self) -> None:
        page = self.page(text="rick dangerous")
        self.assertEqual(
            [(self.label(row.disk_id), row.title) for row in page.rows],
            [
                ("Rick Dangerous [cr Elite]", "Rick Dangerous"),
                ("Automation 10", "Rick Dangerous"),
                ("Rick Dangerous 2", "Rick Dangerous 2"),
            ],
        )
        self.assertEqual(page.total, 3)
        first = page.rows[0]
        self.assertEqual(
            (first.content_kind, first.matched), (ContentKind.GAME, ("Rick Dangerous",))
        )
        self.assertIsNotNone(first.content_id)
        self.assertGreater(first.score, 0)
        self.assertEqual(self.titles(text="the chaos engine")[0][1], "The Chaos Engine")
        # A disc named as the query comes first, then discs holding that title.
        self.assertEqual(self.discs(text="chaos engine"), ["The Chaos Engine", "Automation 10"])
        self.assertEqual(self.discs(text="xenon")[:2], ["Automation 9", "Skid Row Compact 128"])

    def test_every_word_must_match_some_field_of_the_row(self) -> None:
        self.assertEqual(
            self.titles(text="necron automation"),
            [("Automation 250", "Necron"), ("Automation 250 v2", "Necron")],
        )
        self.assertEqual(self.titles(text="xenon amiga"), [("Skid Row Compact 128", "Xenon")])
        self.assertEqual(self.titles(text="1989 amiga speedball"), [("Speedball", "Speedball")])
        self.assertEqual(
            self.titles(text="firebird 1989"), [("Rick Dangerous [cr Elite]", "Rick Dangerous")]
        )
        self.assertEqual(self.titles(text="1990 06 chaos"), [("Automation 10", "The Chaos Engine")])
        self.assertEqual(
            self.titles(text="skid row", sort=SortOrder.DISC),
            [("Skid Row Compact 128", "Paperboy 2"), ("Skid Row Compact 128", "Xenon")],
        )
        self.assertEqual(self.titles(text="necron elite"), [])
        # An article among other words is left out: no Xenon row has "the" in it.
        self.assertEqual(self.titles(text="the xenon"), self.titles(text="xenon"))
        self.assertEqual(len(self.titles(text="the xenon")), 3)

    def test_matched_holds_only_titles_a_word_is_in(self) -> None:
        rows = self.page(text="necron automation", page_size=5).rows
        self.assertEqual([row.matched for row in rows], [("Necron",), ("Necron",)])
        rows = self.page(text="automation 9", mode=ResultMode.TITLES).rows
        self.assertEqual({row.matched for row in rows}, {()})
        discs = self.page(text="xenon", mode=ResultMode.DISCS).rows
        matched = {self.label(row.disk_id): row.matched for row in discs}
        self.assertEqual(matched["Automation 250"], ("Xenon 2 - Megablast",))
        self.assertEqual({row.title for row in discs}, {""})
        self.assertEqual({row.content_id for row in discs}, {None})

    def test_a_disk_reference_becomes_a_filter(self) -> None:
        self.assertEqual(
            self.titles(text="a250"),
            [
                ("Automation 250", "Necron"),
                ("Automation 250", "Xenon 2 - Megablast"),
                ("Automation 250 v2", "Necron"),
            ],
        )
        self.assertEqual(self.discs(text="a250"), ["Automation 250", "Automation 250 v2"])
        self.assertEqual(self.discs(text="auto 250 v2"), ["Automation 250 v2"])
        self.assertEqual(
            self.titles(text="a250 xenon"), [("Automation 250", "Xenon 2 - Megablast")]
        )
        self.assertEqual(self.discs(text="a250", platform=Platform.AMIGA), [])

    def test_a_reference_by_a_long_alias_also_finds_its_words(self) -> None:
        rows = self.page(text="lemmings 2").rows
        found = [(self.label(row.disk_id), row.title) for row in rows]
        self.assertEqual(found, [("Lemmings 2", "Civilization"), ("Lemmings 2", "Lemmings 2")])
        self.assertGreaterEqual(rows[0].score, search.SERIES_SCORE)
        self.assertLess(rows[1].score, search.SERIES_SCORE)

    def test_filters(self) -> None:
        self.assertEqual(
            self.discs(platform=Platform.AMIGA, text="xenon"), ["Skid Row Compact 128"]
        )
        self.assertEqual(self.titles(category="Demos"), [("Some Demo Pack", "Some Demo Pack")])
        self.assertEqual(
            self.discs(kinds=frozenset({DiskKind.SINGLE}), platform=Platform.ATARI_ST),
            ["The Chaos Engine", "Rick Dangerous [cr Elite]"],
        )
        self.assertEqual(
            self.discs(crew="Automation"),
            ["Automation 9", "Automation 10", "Automation 250", "Automation 250 v2"],
        )
        self.assertEqual(self.discs(crew="Firebird"), ["Rick Dangerous 2"])
        self.assertEqual(
            self.discs(year=1989), ["Automation 9", "Rick Dangerous [cr Elite]", "Speedball"]
        )
        self.assertEqual(
            self.discs(year=1989, platform=Platform.AMIGA, text="speed"), ["Speedball"]
        )

    def test_available_only_keeps_local_and_hosted_discs(self) -> None:
        local = [self.ids["Automation 9"]]
        self.assertEqual(
            self.discs(available_only=True, local_disks=local, providers=["internet-archive"]),
            ["Automation 9", "Automation 250"],
        )
        self.assertEqual(self.discs(available_only=True, local_disks=local), ["Automation 9"])
        self.assertEqual(self.discs(available_only=True, providers=["atari-legend"]), [])
        page = self.page(available_only=True)
        self.assertEqual((page.rows, page.total), ([], 0))

    def test_a_large_available_set_gives_the_same_rows_in_every_order(self) -> None:
        local = [self.ids["Automation 9"], self.ids["Speedball"], self.ids["Some Demo Pack"]]
        for mode in ResultMode:
            for sort in SortOrder:
                for text in ("", "xenon"):
                    fields = {"mode": mode, "sort": sort, "text": text, "available_only": True}
                    with self.subTest(mode=mode, sort=sort, text=text):
                        small = self.page(
                            local_disks=local, providers=["internet-archive"], **fields
                        )
                        with mock.patch.object(search, "LARGE_DISK_SET", 0):
                            large = self.page(
                                local_disks=local, providers=["internet-archive"], **fields
                            )
                        self.assertEqual(large, small)
                        self.assertEqual(len(large.rows), large.total)
        self.assertEqual(
            self.discs(available_only=True, local_disks=local, sort=SortOrder.TITLE),
            ["Automation 9", "Some Demo Pack", "Speedball"],
        )

    def test_sorting(self) -> None:
        def titles(sort: SortOrder) -> list[str]:
            return [title for _label, title in self.titles(sort=sort)]

        self.assertEqual(
            titles(SortOrder.TITLE),
            [
                "The Chaos Engine",  # articles do not count
                "The Chaos Engine",
                "Civilization",
                "Lemmings 2",
                "Necron",
                "Necron",
                "Paperboy 2",
                "Rick Dangerous",
                "Rick Dangerous",
                "Rick Dangerous 2",
                "Some Demo Pack",
                "Speedball",
                "Tetris",
                "Xenon",
                "Xenon",
                "Xenon 2 - Megablast",
            ],
        )
        self.assertEqual(
            self.discs(sort=SortOrder.TITLE_DESC)[:4],
            ["Speedball", "Some Demo Pack", "Skid Row Compact 128", "Rick Dangerous [cr Elite]"],
        )
        self.assertEqual(
            self.discs(sort=SortOrder.YEAR),
            [
                "Automation 9",  # 1989, then by title
                "Rick Dangerous [cr Elite]",
                "Speedball",
                "Automation 10",
                "Automation 250",
                "Automation 250 v2",
                "Rick Dangerous 2",
                "Skid Row Compact 128",
                "Lemmings 2",
                "The Chaos Engine",
                "Lemmings 2",
                "Some Demo Pack",  # undated last
            ],
        )
        self.assertEqual(
            self.discs(sort=SortOrder.YEAR_DESC)[:2], ["The Chaos Engine", "Lemmings 2"]
        )
        self.assertEqual(self.discs(sort=SortOrder.YEAR_DESC)[-1], "Some Demo Pack")
        self.assertEqual(
            self.discs(sort=SortOrder.DISC),
            [
                "Automation 9",  # numbered discs by series, number and version
                "Automation 10",
                "Automation 250",
                "Automation 250 v2",
                "Lemmings 2",
                "Skid Row Compact 128",
                "The Chaos Engine",  # then the rest by title
                "Lemmings 2",
                "Rick Dangerous 2",
                "Rick Dangerous [cr Elite]",
                "Some Demo Pack",
                "Speedball",
            ],
        )
        self.assertEqual(
            self.discs(sort=SortOrder.CREW),
            [
                "Automation 9",
                "Automation 10",
                "Automation 250",
                "Automation 250 v2",
                "Rick Dangerous [cr Elite]",
                "Rick Dangerous 2",
                "Speedball",
                "Lemmings 2",
                "Lemmings 2",
                "The Chaos Engine",
                "Skid Row Compact 128",
                "Some Demo Pack",
            ],
        )
        self.assertEqual(
            titles(SortOrder.PLATFORM)[:7],
            [
                "Lemmings 2",
                "Paperboy 2",
                "Rick Dangerous 2",
                "Some Demo Pack",
                "Speedball",
                "Xenon",
                "The Chaos Engine",
            ],
        )
        # Relevance without text is disc order.
        self.assertEqual(self.discs(), self.discs(sort=SortOrder.DISC))
        self.assertEqual(self.titles(), self.titles(sort=SortOrder.DISC))

    def test_every_order_ends_in_a_unique_id(self) -> None:
        for mode in ResultMode:
            plan = search._plan(self.catalogue, Query(text="xenon", mode=mode), (), (), None)
            key = "e.id" if mode == ResultMode.TITLES else "d.id"
            for sort in SortOrder:
                for rank in ("NULL", plan.rank):
                    with self.subTest(mode=mode, sort=sort, rank=rank):
                        order = search._order(sort, plan, rank, search._keys(None, plan, sort))
                        self.assertRegex(order, rf"(^|, ){key}( DESC)?$")

    def test_counts_and_pages_never_repeat_or_skip(self) -> None:
        for mode in ResultMode:
            for sort in SortOrder:
                for text in ("", "xenon"):
                    with self.subTest(mode=mode, sort=sort, text=text):
                        everything = self.page(mode=mode, sort=sort, text=text, page_size=500)
                        keys = [(row.disk_id, row.content_id) for row in everything.rows]
                        self.assertEqual(everything.total, len(keys))
                        paged = []
                        for number in range(everything.total // 2 + 2):
                            page = self.page(
                                mode=mode, sort=sort, text=text, page=number, page_size=2
                            )
                            self.assertEqual(page.total, everything.total)
                            paged += [(row.disk_id, row.content_id) for row in page.rows]
                        self.assertEqual(paged, keys)
                        self.assertEqual(len(set(keys)), len(keys))

    def test_a_word_the_index_does_not_know_is_looked_for_inside_titles(self) -> None:
        self.assertEqual(self.titles(text="egablas"), [("Automation 250", "Xenon 2 - Megablast")])
        self.assertEqual(self.discs(text="egablas"), ["Automation 250"])
        rows = self.page(text="egablas").rows
        self.assertEqual(rows[0].matched, ("Xenon 2 - Megablast",))
        self.assertEqual(
            self.titles(text="utomatio 250 necron"),
            [("Automation 250", "Necron"), ("Automation 250 v2", "Necron")],
        )

    def test_a_very_common_word_is_ranked_without_bm25(self) -> None:
        with mock.patch.object(search, "RANKED_LIMIT", 2):
            rows = self.page(text="xenon").rows
        self.assertEqual(
            [(self.label(row.disk_id), row.title) for row in rows],
            [
                ("Automation 9", "Xenon"),
                ("Skid Row Compact 128", "Xenon"),
                ("Automation 250", "Xenon 2 - Megablast"),
            ],
        )
        self.assertEqual({row.score for row in rows}, {0.0})
        self.assertGreater(self.page(text="xenon").rows[0].score, 0)

    def test_facets_count_discs_and_are_cached(self) -> None:
        found = facets(self.catalogue)
        self.assertEqual(
            found.crews,
            (
                ("Automation", 4),
                ("Elite", 1),
                ("Firebird", 1),
                ("Image Works", 1),
                ("Lemmings Crew", 1),
                ("Psygnosis", 1),
                ("Renegade", 1),
                ("Skid Row", 1),
                ("Unknown crew", 1),
            ),
        )
        self.assertEqual(found.years, ((1989, 3), (1990, 4), (1991, 1), (1992, 1), (1993, 2)))
        self.assertEqual(found.categories, (("Demos", 1), ("Games", 11)))
        self.assertIs(facets(self.catalogue), found)


class WordRuleTest(unittest.TestCase):
    def test_a_word_matches_as_the_index_matches_it(self) -> None:
        words = {"xenon", "2", "megablast", "250"}
        self.assertTrue(term_matches("xen", words))
        self.assertTrue(term_matches("xenon2", words), "split into xenon and 2")
        self.assertTrue(term_matches("2", words))
        self.assertFalse(term_matches("25", words), "a number matches whole")
        self.assertFalse(term_matches("x", {"xenon"}), "one character matches whole")
        self.assertFalse(term_matches("enon", words))


class SearchOverridesTest(unittest.TestCase):
    """The user's corrections as the search sees them (``Overrides``)."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.folder = tempfile.TemporaryDirectory()
        path = Path(cls.folder.name) / "catalogue.sqlite"
        write_find_sample(path)
        cls.catalogue = Catalogue.open(path)
        cls.ids = {disk.label: disk.id for disk in cls.catalogue.disks(range(1, 50)).values()}

    @classmethod
    def tearDownClass(cls) -> None:
        cls.catalogue.close()
        cls.folder.cleanup()

    def disk(self, name: str, **fields):
        """The disc labelled ``name`` with ``fields`` corrected, as the Finder hands it over."""
        return replace(self.catalogue.disk(self.ids[name]), **fields)

    def content(self, label: str, title: str) -> int:
        return next(c.id for c in self.catalogue.contents(self.ids[label]) if c.title == title)

    def overrides(self, disks=(), titles=()) -> Overrides:
        return Overrides(
            {disk.id: disk for disk in disks},
            {self.content(label, old): (self.ids[label], new) for label, old, new in titles},
        )

    def page(self, overrides: Overrides | None, **fields) -> search.CataloguePage:
        return search_page(self.catalogue, Query(page_size=500, **fields), overrides=overrides)

    def discs(self, overrides, **fields) -> list[str]:
        page = self.page(overrides, mode=ResultMode.DISCS, **fields)
        return [self.catalogue.disk(row.disk_id).label for row in page.rows]

    def titles(self, overrides, **fields) -> list[tuple[str, str]]:
        page = self.page(overrides, **fields)
        return [(self.catalogue.disk(row.disk_id).label, row.title) for row in page.rows]

    def test_filters_use_the_corrected_crew_and_year(self) -> None:
        overrides = self.overrides([self.disk("Speedball", crew="Zorglub Crew", year=1987)])
        self.assertEqual(self.discs(overrides, crew="Zorglub Crew"), ["Speedball"])
        self.assertEqual(self.titles(overrides, crew="Zorglub Crew"), [("Speedball", "Speedball")])
        self.assertEqual(self.discs(overrides, crew="Image Works"), [])
        self.assertEqual(self.discs(None, crew="Image Works"), ["Speedball"])
        self.assertEqual(self.discs(overrides, year=1987), ["Speedball"])
        self.assertNotIn("Speedball", self.discs(overrides, year=1989))
        self.assertEqual(self.page(overrides, crew="Zorglub Crew", year=1987).total, 1)
        # Other discs of the catalogue's crew still use the index as before.
        self.assertEqual(
            self.discs(overrides, crew="Automation"),
            ["Automation 9", "Automation 10", "Automation 250", "Automation 250 v2"],
        )

    def test_a_renamed_title_is_found_and_shown_by_its_new_name(self) -> None:
        overrides = self.overrides(titles=[("Automation 9", "Tetris", "Blockout Deluxe")])
        self.assertEqual(
            self.titles(overrides, text="blockout"), [("Automation 9", "Blockout Deluxe")]
        )
        (row,) = self.page(overrides, text="blockout deluxe").rows
        self.assertEqual(row.matched, ("Blockout Deluxe",))
        discs = self.page(overrides, text="blockout", mode=ResultMode.DISCS).rows
        self.assertEqual([row.matched for row in discs], [("Blockout Deluxe",)])
        self.assertEqual(self.titles(None, text="blockout"), [])

    def test_a_disc_without_titles_is_listed_by_its_corrected_label(self) -> None:
        overrides = self.overrides([self.disk("Some Demo Pack", label="Zork Pack")])
        self.assertEqual(self.titles(overrides, text="zork"), [("Some Demo Pack", "Zork Pack")])
        self.assertEqual(self.discs(overrides, text="zork pack"), ["Some Demo Pack"])
        self.assertEqual(self.discs(overrides, sort=SortOrder.TITLE_DESC)[0], "Some Demo Pack")

    def test_corrected_and_catalogue_words_together_find_a_row(self) -> None:
        overrides = self.overrides([self.disk("Automation 250", crew="Zorglub Crew")])
        # "necron" is in the catalogue text, "zorglub" only in the correction.
        self.assertEqual(
            self.titles(overrides, text="zorglub necron"), [("Automation 250", "Necron")]
        )
        self.assertEqual(self.discs(overrides, text="zorglub necron"), ["Automation 250"])
        self.assertEqual(self.titles(overrides, text="zorglub tetris"), [])
        # A disc reference with words: the words may be corrected text too.
        self.assertEqual(
            self.titles(overrides, text="a250 zorglub"),
            [("Automation 250", "Necron"), ("Automation 250", "Xenon 2 - Megablast")],
        )

    def test_text_a_correction_replaced_still_finds_the_row_but_is_not_shown_as_matched(
        self,
    ) -> None:
        overrides = self.overrides(titles=[("Automation 250", "Necron", "Cyborg Hunter")])
        rows = self.page(overrides, text="necron").rows
        found = {(self.catalogue.disk(row.disk_id).label, row.title): row.matched for row in rows}
        self.assertEqual(found[("Automation 250", "Cyborg Hunter")], ())
        self.assertEqual(found[("Automation 250 v2", "Necron")], ("Necron",))
        discs = self.page(overrides, text="necron", mode=ResultMode.DISCS).rows
        matched = {self.catalogue.disk(row.disk_id).label: row.matched for row in discs}
        self.assertEqual(matched["Automation 250"], ())

    def test_a_renamed_title_equal_to_the_query_ranks_first(self) -> None:
        overrides = self.overrides(titles=[("Skid Row Compact 128", "Paperboy 2", "Xenon")])
        self.assertEqual(
            self.titles(overrides, text="xenon")[:3],
            [
                ("Automation 9", "Xenon"),
                ("Skid Row Compact 128", "Xenon"),
                ("Skid Row Compact 128", "Xenon"),
            ],
        )

    def test_the_words_after_a_disc_reference_find_renamed_titles(self) -> None:
        overrides = self.overrides(
            titles=[("Skid Row Compact 128", "Paperboy 2", "Lemmings 2 Tribes")]
        )
        found = self.titles(overrides, text="lemmings 2")
        self.assertIn(("Skid Row Compact 128", "Lemmings 2 Tribes"), found)
        self.assertNotIn(
            ("Skid Row Compact 128", "Lemmings 2 Tribes"), self.titles(None, text="lemmings 2")
        )

    def test_sort_orders_use_the_corrected_values(self) -> None:
        overrides = self.overrides(
            [
                self.disk("Speedball", label="Aardvark", year=1995, crew="Aaa Crew"),
                self.disk("Some Demo Pack", year=1980),
            ],
            [("Automation 9", "Xenon", "Abacus")],
        )
        # A title row keeps its title; the disc's label names its disc row.
        self.assertEqual(
            self.titles(overrides, sort=SortOrder.TITLE)[0], ("Automation 9", "Abacus")
        )
        self.assertEqual(self.discs(overrides, sort=SortOrder.TITLE)[0], "Speedball")
        self.assertEqual(self.discs(overrides, sort=SortOrder.TITLE_DESC)[-1], "Speedball")
        years = self.discs(overrides, sort=SortOrder.YEAR)
        self.assertEqual((years[0], years[-1]), ("Some Demo Pack", "Speedball"))
        self.assertEqual(self.discs(overrides, sort=SortOrder.YEAR_DESC)[0], "Speedball")
        self.assertEqual(self.discs(overrides, sort=SortOrder.CREW)[0], "Speedball")
        self.assertEqual(self.discs(overrides, sort=SortOrder.PLATFORM)[0], "Speedball")

    def test_counts_and_pages_never_repeat_or_skip_with_corrections(self) -> None:
        overrides = self.overrides(
            [self.disk("Speedball", label="Aardvark", year=1995, crew="Aaa Crew")],
            [("Automation 9", "Xenon", "Abacus")],
        )
        for mode in ResultMode:
            for sort in SortOrder:
                for text in ("", "xenon", "aardvark"):
                    with self.subTest(mode=mode, sort=sort, text=text):
                        fields = {"mode": mode, "sort": sort, "text": text}
                        everything = self.page(overrides, **fields)
                        keys = [(row.disk_id, row.content_id) for row in everything.rows]
                        self.assertEqual(everything.total, len(keys))
                        paged = []
                        for number in range(everything.total // 2 + 1):
                            query = Query(page=number, page_size=2, **fields)
                            page = search_page(self.catalogue, query, overrides=overrides)
                            self.assertEqual(page.total, everything.total)
                            paged += [(row.disk_id, row.content_id) for row in page.rows]
                        self.assertEqual(paged, keys)

    def test_no_corrections_search_as_before(self) -> None:
        for mode in ResultMode:
            for sort in SortOrder:
                with self.subTest(mode=mode, sort=sort):
                    fields = {"mode": mode, "sort": sort, "text": "xenon"}
                    self.assertEqual(self.page(Overrides(), **fields), self.page(None, **fields))
        # Corrections that change nothing the search sees change no result either.
        notes_only = self.overrides([self.disk("Speedball", notes="Boxed copy.")])
        self.assertEqual(
            self.page(notes_only, sort=SortOrder.TITLE), self.page(None, sort=SortOrder.TITLE)
        )

    def test_facets_count_discs_under_their_corrected_crew_and_year(self) -> None:
        overrides = self.overrides([self.disk("Speedball", crew="Zorglub Crew", year=1987)])
        found = facets(self.catalogue, overrides)
        crews = dict(found.crews)
        self.assertEqual(crews["Zorglub Crew"], 1)
        self.assertNotIn("Image Works", crews)
        self.assertEqual([name for name, _count in found.crews][-1], "Zorglub Crew")
        years = dict(found.years)
        self.assertEqual((years[1987], years[1989]), (1, 2))
        self.assertEqual(facets(self.catalogue, None), facets(self.catalogue))


if __name__ == "__main__":
    unittest.main()

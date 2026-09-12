"""The D-Bug search engine importer, run offline on page excerpts."""

from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

from catalogue_builder.context import BuildContext, OfflineError
from catalogue_builder.series import DATA_DIR, SeriesRegistry
from catalogue_builder.sources import dbug

FIXTURES = Path(__file__).parent / "fixtures" / "dbug"

SERIES = """
[[series]]
id = "automation"
name = "Automation"
platform = "atari-st"
kind = "menu"

[[series]]
id = "d-bug"
name = "D-Bug"
platform = "atari-st"
kind = "menu"
"""


def registry(directory: Path) -> SeriesRegistry:
    """The real D-Bug match rules over a minimal series list."""
    (directory / "series.toml").write_text(SERIES)
    shutil.copy(DATA_DIR / "match-dbug.toml", directory)
    return SeriesRegistry.load(directory)


class DbugTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.temp)
        (self.temp / "series").mkdir()
        self.ctx = BuildContext(
            cache_dir=self.temp / "cache",
            series=registry(self.temp / "series"),
            offline=True,
            log=lambda message: None,
        )

    def cache(self, url: str, name: str, fixture: str) -> None:
        target = self.ctx.cache_path(url, name)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(FIXTURES / fixture, target)

    def cache_group(self, group: str, slug: str, prefix: str) -> None:
        self.cache(
            dbug.search_url(group, title="%", credits=False), f"{slug}.html", f"{prefix}-list.html"
        )
        self.cache(
            dbug.search_url(group, title="%"), f"{slug}-credits.html", f"{prefix}-credits.html"
        )

    def records(self) -> dict:
        return {record.key: record for record in dbug.collect(self.ctx)}

    def test_automation_contents_credits_and_versions(self) -> None:
        self.cache_group("Automation", "automation", "automation")
        self.cache(
            dbug.search_url("Automation", menu="206"), "automation-206.html", "automation-206.html"
        )
        self.cache(
            dbug.search_url("Automation", menu="250"), "automation-250.html", "automation-250.html"
        )
        self.cache_group("D-Bug", "d-bug", "dbug")
        records = self.records()

        first = records[("automation", 100, "", "")]
        self.assertEqual(
            [content.title for content in first.contents],
            ["Chicago 30's", "Puffy's Saga", "Savage", "CD List"],
        )
        self.assertEqual(first.contents[0].publisher, "Topo Soft")
        self.assertEqual(first.contents[0].cracker, "Ozzwald")
        self.assertEqual(first.contents[3].kind, "other")
        self.assertEqual(first.contents[3].publisher, "")  # "N/A" is dropped
        self.assertEqual(
            first.credits, "Code: Neil; Graphics: Pursy; Music: Obliterator by David Whittaker"
        )
        self.assertFalse(first.locations)  # D-Bug hosts no Automation images

        second = records[("automation", 100, "", "v2")]
        self.assertEqual(second.version, "v2")
        self.assertIn("S.H.I.T.", second.credits)

    def test_truncated_credits_page_falls_back_to_the_menu_page(self) -> None:
        # The site stops writing part way through Automation 206; that block must
        # not be trusted, so its credits come from the single menu page.
        self.cache_group("Automation", "automation", "automation")
        self.cache(
            dbug.search_url("Automation", menu="206"), "automation-206.html", "automation-206.html"
        )
        self.cache(
            dbug.search_url("Automation", menu="250"), "automation-250.html", "automation-250.html"
        )
        self.cache_group("D-Bug", "d-bug", "dbug")
        disk = self.records()[("automation", 206, "", "")]
        self.assertEqual(
            disk.credits, "Code: The Law; Graphics: Tucker; Music: Rock N Roll by Baz Leitch"
        )
        self.assertEqual(len(disk.contents), 4)
        self.assertEqual(disk.contents[1].title, "Kayden Garth")

    def test_a_missing_menu_page_is_requested(self) -> None:
        self.cache_group("Automation", "automation", "automation")
        self.cache(
            dbug.search_url("Automation", menu="206"), "automation-206.html", "automation-206.html"
        )
        with self.assertRaises(OfflineError) as raised:
            list(dbug.collect(self.ctx))
        self.assertIn("menu=250", str(raised.exception))

    def test_dbug_parts_and_msa_locations(self) -> None:
        self.cache_group("Automation", "automation", "empty")
        self.cache_group("D-Bug", "d-bug", "dbug")
        records = self.records()
        part_a = records[("d-bug", 100, "A", "")]
        part_b = records[("d-bug", 100, "B", "")]
        self.assertEqual(part_a.contents[0].title, "Streetfighter II")
        self.assertEqual(part_a.contents[0].extra, "[F30]")
        self.assertEqual(part_b.contents[0].title, "Streetfighter II data")
        self.assertEqual(
            part_a.credits,
            "Code: Cyrano Jones; Graphics: Pursy; Music: No Second Prize by Big Alec",
        )
        [location] = part_a.locations
        self.assertEqual(location.url, "https://d-bug.me/dbugmenu/dbug100a.msa")
        self.assertEqual(location.provider, "d-bug")
        self.assertEqual(location.priority, 35)
        self.assertEqual(location.hash_value, "")
        self.assertEqual(part_a.links[0][0], "D-Bug")

    def test_doc_entries_become_doc_contents(self) -> None:
        content = dbug._content(
            "<FONT COLOR='x'>Docs:- Simpsons,Utopia<a href=\"newsearch.php?comp=N/A\">"
            ' by N/A</a><a href="newsearch.php?crack=N/A"> - Cracked by N/A</a></font>'
        )
        self.assertEqual(content.kind, "doc")
        self.assertEqual(content.title, "Simpsons, Utopia")


if __name__ == "__main__":
    unittest.main()

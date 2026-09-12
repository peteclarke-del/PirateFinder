"""The Steem Automation catalogue importer, run on an excerpt of the page."""

from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

from catalogue_builder.context import BuildContext
from catalogue_builder.series import DATA_DIR, SeriesRegistry
from catalogue_builder.sources import steem

FIXTURES = Path(__file__).parent / "fixtures" / "steem"

SERIES = """
[[series]]
id = "automation"
name = "Automation"
platform = "atari-st"
kind = "menu"
"""


class SteemTest(unittest.TestCase):
    def setUp(self) -> None:
        temp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, temp)
        (temp / "series").mkdir()
        (temp / "series" / "series.toml").write_text(SERIES)
        shutil.copy(DATA_DIR / "match-steem.toml", temp / "series")
        self.ctx = BuildContext(
            cache_dir=temp / "cache",
            series=SeriesRegistry.load(temp / "series"),
            offline=True,
            log=lambda message: None,
        )
        target = self.ctx.cache_path(steem.PAGE)
        target.parent.mkdir(parents=True)
        shutil.copy(FIXTURES / "automation.htm", target)
        self.records = {record.key: record for record in steem.collect(self.ctx)}

    def test_every_entry_is_keyed(self) -> None:
        self.assertEqual(len(self.records), 11)
        self.assertNotIn(None, self.records)

    def test_contents_and_qualifiers(self) -> None:
        disk = self.records[("automation", 100, "", "")]
        self.assertEqual(
            [content.title for content in disk.contents],
            [
                "Chicago 30s",
                "Puffy's Saga",
                "Savage games 1-3",
                "Automation Games Listing 1-100, Information and Cheats",
            ],
        )
        self.assertEqual(disk.contents[3].kind, "other")
        red_alert = self.records[("automation", 102, "", "")]
        skateball = next(c for c in red_alert.contents if c.title == "Skateball")
        self.assertEqual(skateball.extra, "French")
        empire = next(c for c in red_alert.contents if c.title == "Empire")
        self.assertEqual(empire.extra, "with docs")

    def test_offensive_marker_line_is_not_a_title(self) -> None:
        disk = self.records[("automation", 102, "", "")]
        self.assertEqual(disk.contents[0].title, "Total Eclipse")

    def test_ste_version_note_is_version_two(self) -> None:
        self.assertIn(("automation", 0, "", "v2"), self.records)
        disk = self.records[("automation", 9, "", "v2")]  # note inside the first item
        self.assertEqual(disk.contents[0].title, "Nebulus")
        self.assertIn("STE", disk.notes)

    def test_parts_and_versions_from_file_names(self) -> None:
        self.assertIn(("automation", 69, "A", ""), self.records)  # A_069_1
        self.assertIn(("automation", 69, "E", ""), self.records)  # A_069_5
        self.assertIn(("automation", 148, "B", ""), self.records)
        self.assertIn(("automation", 500, "LA", ""), self.records)
        self.assertIn(("automation", 500, "E", "v2"), self.records)  # A_500_E2
        games = self.records[("automation", 148, "A", "")].contents
        self.assertEqual((games[0].title, games[0].extra), ("The Games: Summer Edition", "disk a"))

    def test_damaged_image_note(self) -> None:
        disk = self.records[("automation", 355, "", "")]
        self.assertIn("possibly damaged", disk.notes)
        self.assertEqual(disk.condition, "")


if __name__ == "__main__":
    unittest.main()

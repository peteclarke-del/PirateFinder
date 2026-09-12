"""The 8bitchip games-on-menu-disks importer, run on an excerpt of the list."""

from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

from catalogue_builder.context import BuildContext
from catalogue_builder.series import DATA_DIR, SeriesRegistry
from catalogue_builder.sources import menudg

FIXTURES = Path(__file__).parent / "fixtures" / "menudg"

SERIES_IDS = (
    "automation",
    "cynix",
    "d-bug",
    "flame-of-finland",
    "fuzion",
    "supergau",
    "medway-boys",
    "pompey-pirates-krappy-kompact",
    "pompey-pirates",
    "sewer-doc",
    "superior",
    "vectronix",
)


class MenuDGTest(unittest.TestCase):
    def setUp(self) -> None:
        temp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, temp)
        series = temp / "series"
        series.mkdir()
        (series / "series.toml").write_text(
            "".join(
                f'[[series]]\nid = "{series_id}"\nname = "{series_id}"\n'
                'platform = "atari-st"\nkind = "menu"\n\n'
                for series_id in SERIES_IDS
            )
        )
        shutil.copy(DATA_DIR / "match-menudg.toml", series)
        shutil.copy(DATA_DIR / "extra-crewlists.toml", series)  # declares terdd-doc
        self.messages: list[str] = []
        self.ctx = BuildContext(
            cache_dir=temp / "cache",
            series=SeriesRegistry.load(series),
            offline=True,
            log=self.messages.append,
        )
        target = self.ctx.cache_path(menudg.PAGE)
        target.parent.mkdir(parents=True)
        shutil.copy(FIXTURES / "MenuDG.html", target)
        self.records = {record.key: record for record in menudg.collect(self.ctx)}

    def test_index_is_turned_around_into_disk_contents(self) -> None:
        vectronix = self.records[("vectronix", 111, "", "")]
        self.assertEqual([content.title for content in vectronix.contents], ["10th Frame"])
        automation = self.records[("automation", 61, "", "")]
        self.assertEqual(automation.contents[0].title, "10th Frame")

    def test_continuation_rows_belong_to_the_game_above(self) -> None:
        self.assertEqual(self.records[("medway-boys", 63, "", "")].contents[0].title, "10th Frame")
        self.assertEqual(self.records[("supergau", 147, "", "")].contents[0].title, "1943")

    def test_parts_and_kinds(self) -> None:
        part = self.records[("pompey-pirates", 13, "F", "")].contents[0]
        self.assertEqual((part.title, part.kind), ("3D Construction Kit", "utility"))
        doc = self.records[("sewer-doc", 35, "", "")].contents[0]
        self.assertEqual(
            (doc.title, doc.kind, doc.extra), ("3D Construction Kit", "doc", "Instructions")
        )
        self.assertIn(("d-bug", 122, "B", ""), self.records)

    def test_empty_type_inherits_the_games_first_type(self) -> None:
        # "Boulderdash Construction Set" is an editor; its row on VE521 has no type.
        content = self.records[("vectronix", 521, "", "")].contents[0]
        self.assertEqual((content.title, content.kind), ("Boulderdash Construction Set", "utility"))

    def test_prefix_without_rule_is_reported(self) -> None:
        self.assertTrue(any("'SO'" in message for message in self.messages))


if __name__ == "__main__":
    unittest.main()

"""The Demozoo importer, run on a small synthetic excerpt of the database export."""

from __future__ import annotations

import gzip
import shutil
import tempfile
import unittest
from pathlib import Path

from catalogue_builder.context import BuildContext
from catalogue_builder.series import DATA_DIR, SeriesRegistry
from catalogue_builder.sources import demozoo

FIXTURE = Path(__file__).parent / "fixtures" / "demozoo" / "demozoo-export-excerpt.sql"

SERIES = """
[[series]]
id = "skid-row-compact"
name = "Skid Row Compact"
platform = "amiga"
kind = "menu"
group = "Skid Row"

[[series]]
id = "prevail-pack"
name = "Prevail Pack"
platform = "amiga"
kind = "pack"
group = "Effect"

[[series]]
id = "d-bug"
name = "D-Bug"
platform = "atari-st"
kind = "menu"
group = "D-Bug"
"""


def make_context(temp: Path, rules: bool = True) -> BuildContext:
    series = temp / "series"
    series.mkdir()
    (series / "series.toml").write_text(SERIES)
    if rules:
        text = (DATA_DIR / "match-demozoo.toml").read_text()
        known = {"skid-row-compact", "prevail-pack", "d-bug"}
        others = sorted(
            {line.split('"')[1] for line in text.splitlines() if line.startswith("series = ")}
            - known
        )
        (series / "match-demozoo.toml").write_text(text)
        (series / "others.toml").write_text(
            "".join(
                f'[[series]]\nid = "{name}"\nname = "{name}"\nplatform = "amiga"\nkind = "pack"\n\n'
                for name in others
            )
        )
    return BuildContext(
        cache_dir=temp / "cache",
        series=SeriesRegistry.load(series),
        offline=True,
        log=lambda message: None,
    )


class CopyFormatTest(unittest.TestCase):
    def test_escapes_and_null(self) -> None:
        self.assertIsNone(demozoo._unescape("\\N"))
        self.assertEqual(demozoo._unescape("a\\tb\\nc\\\\d"), "a\tb\nc\\d")
        self.assertEqual(demozoo._unescape("caf\\351"), "caf\u00e9")

    def test_only_wanted_tables_and_columns(self) -> None:
        lines = FIXTURE.read_text().splitlines(keepends=True)
        rows = list(demozoo.copy_rows(lines, {"platforms_platform": ("name", "id")}))
        self.assertEqual(rows[0], ("platforms_platform", ("Windows", "1")))
        self.assertEqual({table for table, _row in rows}, {"platforms_platform"})
        self.assertEqual(len(rows), 4)


class DemozooTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.temp)
        dump = self.temp / "demozoo-export.sql.gz"
        with gzip.open(dump, "wb") as handle:
            handle.write(FIXTURE.read_bytes())
        self.ctx = make_context(self.temp)
        self.ctx.inputs["demozoo"] = dump
        records = list(demozoo.collect(self.ctx))
        self.keyed = {record.key: record for record in records if record.key}
        self.loose = [record for record in records if record.key is None]

    def test_series_packs(self) -> None:
        self.assertEqual(
            sorted(self.keyed),
            [
                ("d-bug", 193, "A", ""),
                ("prevail-pack", 147, "", ""),
                ("skid-row-compact", 130, "", ""),
            ],
        )
        prevail = self.keyed[("prevail-pack", 147, "", "")]
        self.assertEqual(
            [(content.title, content.kind) for content in prevail.contents],
            [
                ("Merry X-Mas", "demo"),
                ("Some Unreleased Chiptunes", "music"),
                ("It's Us Again", "intro"),
            ],
        )
        self.assertEqual(prevail.publisher, "Effect & MvA")
        self.assertEqual(prevail.date, "1993-12")
        self.assertEqual(prevail.links, [("Demozoo", "https://demozoo.org/productions/200/")])
        self.assertEqual(self.keyed[("d-bug", 193, "A", "")].platform, "atari-st")

    def test_members_follow_pack_order(self) -> None:
        compact = self.keyed[("skid-row-compact", 130, "", "")]
        self.assertEqual(
            [content.title for content in compact.contents],
            ["Paperboy II +2", "Project X Mini Trainer"],
        )

    def test_cracktros_on_a_compact_are_games(self) -> None:
        compact = self.keyed[("skid-row-compact", 130, "", "")]
        self.assertEqual({content.kind for content in compact.contents}, {"game"})
        [loose] = self.loose
        self.assertEqual(loose.contents[0].kind, "intro")  # a cracktro in a demo pack

    def test_other_packs_keep_their_title_and_platforms_are_filtered(self) -> None:
        [loose] = self.loose
        self.assertEqual(loose.title, "Crazy Pack 5 (Some Group)")  # a tab in the title
        self.assertEqual(loose.kind, "pack")
        self.assertEqual(loose.platform, "amiga")
        self.assertEqual(loose.date, "1990")
        self.assertEqual(loose.contents[0].title, "Back\\Slash Intro")

    def test_name_fallback_without_rules(self) -> None:
        temp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, temp)
        ctx = make_context(temp, rules=False)
        pack = demozoo.Pack(1, "Prevail Pack #147", "", "amiga", "Effect & MvA", [])
        found = demozoo.identify(ctx.series, pack)
        self.assertEqual((found.series_id, found.number), ("prevail-pack", 147))
        stranger = demozoo.Pack(2, "Prevail Pack 3", "", "amiga", "Somebody Else", [])
        self.assertIsNone(demozoo.identify(ctx.series, stranger))


if __name__ == "__main__":
    unittest.main()

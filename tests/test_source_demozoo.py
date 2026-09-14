"""The Demozoo importer, run on a small synthetic excerpt of the database export."""

from __future__ import annotations

import gzip
import json
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

[[series]]
id = "automation"
name = "Automation"
platform = "atari-st"
kind = "menu"
group = "Automation"
aliases = ["automation cd"]

[[series]]
id = "medway-boys"
name = "Medway Boys"
platform = "atari-st"
kind = "menu"
group = "Medway Boys"
"""


def make_context(temp: Path, rules: bool = True) -> BuildContext:
    series = temp / "series"
    series.mkdir()
    (series / "series.toml").write_text(SERIES)
    if rules:
        text = (DATA_DIR / "match-demozoo.toml").read_text()
        known = {"skid-row-compact", "prevail-pack", "d-bug", "automation", "medway-boys"}
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
        self.records = list(demozoo.collect(self.ctx))
        packs = [record for record in self.records if record.contents]
        self.keyed = {record.key: record for record in packs if record.key}
        self.loose = [record for record in packs if record.key is None]
        self.menus = {
            record.key: record for record in self.records if not record.contents and record.key
        }

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

    def test_release_dates_keep_their_precision(self) -> None:
        self.assertEqual(self.keyed[("skid-row-compact", 130, "", "")].release_date, "1992-04-17")
        self.assertEqual(self.keyed[("prevail-pack", 147, "", "")].release_date, "1993-12")
        self.assertEqual(self.loose[0].release_date, "1990")

    def test_pack_screenshots_are_disc_pictures(self) -> None:
        compact = self.keyed[("skid-row-compact", 130, "", "")]
        disc = [item for item in compact.media if not item.content_title]
        [picture] = disc
        self.assertEqual(picture.kind, "menu")
        self.assertEqual(picture.url, "https://media.example/s/100.png")
        self.assertEqual(picture.thumb_url, "https://media.example/t/100.png")
        self.assertEqual((picture.width, picture.height), (400, 320))
        self.assertEqual((picture.rank, picture.source), (15, "demozoo"))
        self.assertEqual(picture.page_url, "https://demozoo.org/productions/100/")
        self.assertEqual(picture.credit, "Demozoo contributors, demozoo.org/productions/100/")
        prevail = self.keyed[("prevail-pack", 147, "", "")]
        shots = [(m.url, m.width, m.rank) for m in prevail.media if not m.content_title]
        # A screenshot without a standard size falls back to the original,
        # and three at most are kept of the four.
        self.assertEqual(
            shots,
            [
                ("https://media.example/o/200a.png", 320, 15),
                ("https://media.example/s/200b.png", 400, 16),
                ("https://media.example/s/200c.png", 320, 17),
            ],
        )

    def test_member_screenshots_are_title_pictures(self) -> None:
        compact = self.keyed[("skid-row-compact", 130, "", "")]
        [cracktro] = [item for item in compact.media if item.content_title]
        self.assertEqual((cracktro.content_title, cracktro.kind), ("Paperboy II +2", "intro"))
        self.assertEqual(cracktro.page_url, "https://demozoo.org/productions/101/")
        prevail = self.keyed[("prevail-pack", 147, "", "")]
        [demo] = [item for item in prevail.media if item.content_title]
        self.assertEqual((demo.content_title, demo.kind), ("Merry X-Mas", "demo"))
        self.assertEqual(demo.url, "https://media.example/s/201.png")  # the first one only
        # Two members of one title: the title gets the first one's picture.
        [loose] = self.loose
        self.assertEqual([m.url for m in loose.media], ["https://media.example/o/301.png"])

    def test_member_reference_ids(self) -> None:
        compact = self.keyed[("skid-row-compact", 130, "", "")]
        self.assertEqual(compact.contents[0].links, [("demozoo", "101")])

    def test_screenshots_of_other_platforms_are_not_kept(self) -> None:
        dump = demozoo.read_dump(FIXTURE.read_text().splitlines(keepends=True))
        self.assertNotIn(400, dump.screenshots)
        self.assertIn(700, dump.screenshots)

    def test_notes_are_plain_text_disc_trivia(self) -> None:
        [note] = self.keyed[("prevail-pack", 147, "", "")].trivia
        self.assertEqual(note.kind, "note")
        self.assertEqual(note.text, "Released at The Party in 1993.\nSecond line & more")
        self.assertEqual(note.url, "https://demozoo.org/productions/200/")
        self.assertEqual(self.loose[0].trivia[0].text, "line one\nline two")

    def test_menu_intros_give_their_menu_disk_a_picture(self) -> None:
        self.assertEqual(
            sorted(self.menus), [("automation", 155, "", "v2"), ("medway-boys", 1, "", "")]
        )
        menu = self.menus[("automation", 155, "", "v2")]
        self.assertEqual((menu.kind, menu.platform, menu.title), ("menu", "atari-st", ""))
        self.assertEqual(menu.release_date, "1990-11-02")
        [picture] = menu.media
        self.assertEqual((picture.kind, picture.url), ("menu", "https://media.example/s/700.png"))
        self.assertEqual(menu.trivia[0].text, "The menu of CD 155.")
        self.assertEqual(menu.links, [("Demozoo", "https://demozoo.org/productions/700/")])
        self.assertEqual(self.menus[("medway-boys", 1, "", "")].release_date, "1988")
        # "Prevail Pack #148 intro" names a pack series and "Automation CD #156
        # intro" is by another group: neither is a menu of a series.
        self.assertNotIn(("prevail-pack", 148, "", ""), self.menus)
        self.assertNotIn(("automation", 156, "", ""), self.menus)

    def test_numbered_titles_with_parts_and_versions(self) -> None:
        pack = demozoo.Pack(1, "D-BUG CD 157 A V2", "", "atari-st", "D-Bug", [])
        found = demozoo.identify(self.ctx.series, pack)
        self.assertEqual((found.series_id, found.number, found.part), ("d-bug", 157, "A"))
        self.assertEqual(demozoo._version(found.version), "v2")
        pack = demozoo.Pack(2, "Automation CD No 12", "", "atari-st", "Automation", [])
        found = demozoo.identify(self.ctx.series, pack)
        self.assertEqual((found.series_id, found.number), ("automation", 12))
        pack = demozoo.Pack(3, "Compact Menu 20", "", "atari-st", "The Medway Boys", [])
        self.assertEqual(demozoo.identify(self.ctx.series, pack).series_id, "medway-boys")

    def test_crews(self) -> None:
        found = list(demozoo.collect_crews(self.ctx))
        self.assertEqual(
            [(crew.name, crew.id, crew.platforms) for crew in found],
            [
                ("Skid Row", "10", {"amiga": 1}),
                ("D-Bug", "50", {"atari-st": 1}),
                ("Automation", "60", {"atari-st": 1}),
                # Another group of the same name, on the Amiga only. Its one
                # production is listed for two Amiga platforms.
                ("Automation", "65", {"amiga": 1}),
                ("Medway Boys", "70", {"atari-st": 1}),
            ],
        )
        crews = {crew.id: crew for crew in found}
        self.assertEqual(crews["60"].notes, "The Atari ST menu crew.")
        self.assertEqual(crews["65"].notes, "An Amiga demo group of the same name.")
        medway = crews["70"]  # "The Medway Boys" on Demozoo
        self.assertEqual(medway.members, ["Wurzel", "Gino"])  # current members first
        self.assertEqual(medway.notes, "Started by Wurzel on the C64.")
        self.assertEqual(medway.url, "https://demozoo.org/groups/70/")
        skid_row = crews["10"]
        self.assertEqual(skid_row.notes, "Formed by Metallica in 1990.[1]\n\n[1]: A cracktro.")
        self.assertEqual(skid_row.wikipedia, "Skid Row (warez group)")
        self.assertEqual(skid_row.source, "demozoo")
        self.assertEqual(crews["50"].notes, "Rose from the ashes of Automation.")
        self.assertEqual(crews["50"].wikipedia, "")

    def test_disks_list_the_groups_credited_with_them(self) -> None:
        self.assertEqual(self.keyed[("skid-row-compact", 130, "", "")].crew_ids, ["10"])
        self.assertEqual(self.keyed[("prevail-pack", 147, "", "")].crew_ids, ["20", "30"])
        self.assertEqual(self.menus[("automation", 155, "", "v2")].crew_ids, ["60"])
        self.assertEqual(self.menus[("medway-boys", 1, "", "")].crew_ids, ["70"])

    def test_the_is_ignored_when_naming_crews(self) -> None:
        # Without data/groups.toml, "The Medway Boys" still finds the series
        # group "Medway Boys".
        dump = demozoo.read_dump(FIXTURE.read_text().splitlines(keepends=True))
        names = {crew.name for crew in demozoo.crew_records(dump, self.ctx.series)}
        self.assertIn("Medway Boys", names)

    def test_crews_are_kept_from_collect(self) -> None:
        self.ctx.inputs["demozoo"].unlink()  # collect_crews must not read the dump again
        self.assertEqual(len(list(demozoo.collect_crews(self.ctx))), 5)

    def test_download_links_become_locations(self) -> None:
        prevail = self.keyed[("prevail-pack", 147, "", "")]
        # Untergrund forbids robots, an intro file and an LhA archive are no
        # disk images, and a link that is not a download is left out.
        self.assertEqual(
            [(loc.provider, loc.url, loc.container) for loc in prevail.locations],
            [
                (
                    "amigascne",
                    "https://ftp.scene.org/mirrors/amigascne/Packdisks/Prevail/PrevailPack147.dms",
                    "",
                )
            ],
        )
        self.assertEqual(prevail.locations[0].priority, demozoo.DOWNLOAD_PRIORITY)

    def test_a_pack_without_members_is_kept_when_it_can_be_downloaded(self) -> None:
        [lost] = [record for record in self.records if record.title == "Lost Pack 7"]
        self.assertEqual((lost.contents, lost.key, lost.kind), ([], None, "pack"))
        self.assertEqual(
            [loc.url for loc in lost.locations],
            ["https://ftp.scene.org/mirrors/amigascne/Packdisks/Lost/LostPack07.adf"],
        )
        # A memberless pack with no download is still left out.
        self.assertFalse(any(record.title.startswith("Empty Pack") for record in self.records))

    def test_fujiology_zips_need_their_listing_and_the_size_of_a_disk(self) -> None:
        dbug = self.keyed[("d-bug", 193, "A", "")]
        # Offline, with no listing cached: no Fujiology location, scene.org still.
        self.assertEqual([loc.provider for loc in dbug.locations], ["scene-org"])
        listings = {
            "ST/D/DBUG/": [{"name": "DBUG193A.ZIP", "size": 790114}],
            "ST/A/AUTOMATN/": [{"name": "AUTO155.ZIP", "size": 10973}],
        }
        for folder, entries in listings.items():
            url = "https://fujiology.org/" + folder
            target = self.ctx.cache_path(url, "listing.json")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(json.dumps(entries))
        records = list(demozoo.collect(self.ctx))
        dbug = next(r for r in records if r.key == ("d-bug", 193, "A", ""))
        fuji = [loc for loc in dbug.locations if loc.provider == "fujiology"]
        self.assertEqual(
            [(loc.url, loc.container, loc.size) for loc in fuji],
            [("https://fujiology.org/ST/D/DBUG/DBUG193A.ZIP", "zip", 790114)],
        )
        # The Automation menu's zip holds only its intro program.
        menu = next(r for r in records if r.key == ("automation", 155, "", "v2"))
        self.assertEqual(menu.locations, [])

    def test_plain_text(self) -> None:
        self.assertEqual(demozoo.plain_text("**a** and __b__ and *c*"), "a and b and c")
        self.assertEqual(
            demozoo.plain_text("# Heading\nx ![pic](http://p) [](http://u)"), "Heading\nx  http://u"
        )
        self.assertEqual(demozoo.plain_text("2*3*4"), "2*3*4")
        self.assertEqual(demozoo.plain_text(None), "")

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

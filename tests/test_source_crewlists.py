"""The crew lists importer, run offline on excerpts of the Pompey Pirates CD-R."""

from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

from catalogue_builder.context import BuildContext
from catalogue_builder.series import DATA_DIR, SeriesRegistry
from catalogue_builder.sources import crewlists

FIXTURES = Path(__file__).parent / "fixtures" / "crewlists"

SERIES = "".join(
    f'[[series]]\nid = "{series_id}"\nname = "{series_id}"\nplatform = "atari-st"\nkind = "{kind}"\n\n'
    for series_id, kind in (
        ("pompey-pirates", "menu"),
        ("pompey-pirates-krappy-kompact", "menu"),
        ("medway-boys", "menu"),
        ("flame-of-finland", "menu"),
        ("superior", "menu"),
        ("cynix", "menu"),
        ("delight", "menu"),
        ("sewer-doc", "pack"),
    )
)


class CrewListsTest(unittest.TestCase):
    def setUp(self) -> None:
        temp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, temp)
        series = temp / "series"
        series.mkdir()
        (series / "series.toml").write_text(SERIES)
        shutil.copy(DATA_DIR / "match-crewlists.toml", series)
        shutil.copy(DATA_DIR / "extra-crewlists.toml", series)
        self.ctx = BuildContext(
            cache_dir=temp / "cache",
            series=SeriesRegistry.load(series),
            offline=True,
            log=lambda message: None,
        )
        self.cache(crewlists.LISTING, "atari-st-iso-listing.html", "iso-listing.html")
        for member, fixture in (
            ("MENUS/POMPEY/POMPEY.TXT", "POMPEY.TXT"),
            ("MENUS/MEDWAY/MEDWAY.TXT", "MEDWAY.TXT"),
            ("MENUS/COMPLETE.TXT", "COMPLETE.TXT"),
            ("DOCS/SEWER/LIST.DOC", "LIST.DOC"),
        ):
            self.cache(crewlists.member_url(member), member.replace("/", "_"), fixture)
        records = list(crewlists.collect(self.ctx))
        self.records = {record.key: record for record in records if record.key}
        self.loose = [record for record in records if record.key is None]

    def cache(self, url: str, name: str, fixture: str) -> None:
        target = self.ctx.cache_path(url, name)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(FIXTURES / fixture, target)

    def titles(self, key: tuple) -> list[str]:
        return [content.title for content in self.records[key].contents]

    def test_titles_are_title_cased(self) -> None:
        self.assertEqual(
            self.titles(("pompey-pirates", 50, "", "")), ["Fighter Bomber Advanced Mission Disk"]
        )
        self.assertEqual(self.titles(("pompey-pirates", 13, "C", ""))[1], "S.E.U.C.K.")
        self.assertEqual(crewlists.title_case("XENON II - MEGABLAST"), "Xenon II - Megablast")
        self.assertEqual(crewlists.title_case("CJ'S ELEPHANT ANTICS"), "CJ's Elephant Antics")
        self.assertEqual(crewlists.title_case("JAM PACKER V.3.01"), "Jam Packer v3.01")
        self.assertEqual(crewlists.title_case("RETURN OF THE JEDI"), "Return of the Jedi")
        self.assertEqual(crewlists.title_case("ZX81 EMULATOR"), "ZX81 Emulator")

    def test_seven_disks_of_one_number_become_parts_a_to_g(self) -> None:
        parts = sorted(key[2] for key in self.records if key[:2] == ("pompey-pirates", 13))
        self.assertEqual(parts, list("ABCDEFG"))
        self.assertEqual(self.titles(("pompey-pirates", 13, "D", ""))[0], "Awesome")
        data = self.records[("pompey-pirates", 13, "E", "")].contents[0]
        self.assertEqual((data.title, data.extra), ("Awesome", "data"))

    def test_version_codes(self) -> None:
        self.assertEqual(self.titles(("pompey-pirates", 1, "", "")), ["New Zealand Story"])
        self.assertEqual(self.titles(("pompey-pirates", 1, "", "v2"))[0], "Gridrunner")
        # "CD13" and "CD13B" with no "CD13A" are two versions of Medway Boys 13. The
        # first is "v1", which the merge key spells like an unversioned disk.
        self.assertEqual(self.titles(("medway-boys", 13, "", "")), ["Dugger", "Hell Raisers"])
        self.assertEqual(self.records[("medway-boys", 13, "", "")].version, "v1")
        self.assertEqual(self.titles(("medway-boys", 13, "", "v2"))[0], "Hawkeye")

    def test_numbering_restart_starts_a_new_section(self) -> None:
        self.assertNotIn(
            "Pompey Pirates Crappy Compacts", self.titles(("pompey-pirates", 114, "", ""))
        )
        krappy = self.titles(("pompey-pirates-krappy-kompact", 1, "", ""))
        self.assertEqual(krappy[:2], ["Ace Invaders", "Defender"])
        self.assertNotIn(("pompey-pirates", 1, "", "v3"), self.records)

    def test_msa_files_become_locations_on_their_disk(self) -> None:
        def urls(key: tuple) -> list[str]:
            return [location.url.rsplit("%2F", 1)[-1] for location in self.records[key].locations]

        self.assertEqual(urls(("pompey-pirates", 13, "A", "")), ["PP_013.MSA"])
        self.assertEqual(urls(("pompey-pirates", 13, "B", "")), ["PP_013_2.MSA"])
        self.assertEqual(urls(("pompey-pirates", 13, "F", "")), ["PP_13_5A.MSA"])
        self.assertEqual(urls(("pompey-pirates", 1, "", "v2")), ["PP_001V2.MSA"])
        self.assertEqual(urls(("pompey-pirates-krappy-kompact", 1, "", "")), ["PPKC_001.MSA"])
        self.assertEqual(urls(("medway-boys", 13, "", "")), ["MED_013A.MSA"])
        self.assertEqual(urls(("medway-boys", 41, "", "v2")), ["MED_041B.MSA"])
        location = self.records[("pompey-pirates", 50, "", "")].locations[0]
        self.assertEqual(location.provider, "internet-archive")
        self.assertEqual(location.priority, 30)
        self.assertEqual(location.container, "")
        self.assertEqual(
            location.url,
            "https://archive.org/download/atari-st-collection-1997-cdr-alien-pompey-pirates/"
            "ATARI_ST.ISO/MENUS%2FPOMPEY%2FPP_050.MSA",
        )

    def test_merged_index_adds_missing_titles_only(self) -> None:
        titles = self.titles(("medway-boys", 20, "", ""))
        self.assertEqual(titles, ["Battle for the Thorne", "Weird Dreams", "Index Only Title"])

    def test_sewer_doc_list(self) -> None:
        docs = self.records[("sewer-doc", 6, "", "")].contents
        self.assertEqual(
            (docs[0].title, docs[0].kind, docs[0].extra), ("1st. Shapes", "doc", "Instructions")
        )
        self.assertIn(("terdd-doc", 1, "", ""), self.records)
        self.assertEqual(
            self.records[("terdd-doc", 1, "", "")].locations[0].url[-12:], "TERDD_01.MSA"
        )
        [spaced_out] = self.loose
        self.assertEqual(spaced_out.title, "Spaced Out Disk (Sewer Software doc list)")
        self.assertEqual(spaced_out.contents[0].title, "68000 Machine Code Course")


class AssignPartsTest(unittest.TestCase):
    def test_single_disk_keeps_no_part(self) -> None:
        final = crewlists.assign_parts([("s", 5, "", "")])
        self.assertEqual(final[("s", 5, "", "")], ("", ""))

    def test_lettered_parts_keep_their_order(self) -> None:
        final = crewlists.assign_parts([("s", 9, "A", ""), ("s", 9, "B", "")])
        self.assertEqual([final[("s", 9, p, "")] for p in "AB"], [("A", ""), ("B", "")])


if __name__ == "__main__":
    unittest.main()

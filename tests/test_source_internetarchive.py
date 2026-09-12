"""Tests for the Internet Archive importer: listings, naming and locations."""

from __future__ import annotations

import shutil
import tempfile
import unittest
import urllib.parse
from pathlib import Path
from unittest import mock

from catalogue_builder.context import BuildContext
from catalogue_builder.series import SeriesRegistry
from catalogue_builder.sources import internetarchive as ia

FIXTURES = Path(__file__).parent / "fixtures" / "internetarchive"
MENUS = ia.ArchiveSet("atari-st-collection", "[Menus].7z", "atari-st", "menu", 20)
TOSEC_2012 = ia.ArchiveSet(
    "Atari_ST_TOSEC_2012_04_23", "Atari_ST_TOSEC_2012_04_23.zip", "atari-st", "", 25
)
AMIGA = ia.ItemSet("commodore-amiga-compilations-games", "amiga", "", 20)


class Offline:
    """A BuildContext that serves fixture files from its cache and never fetches."""

    def __init__(self, test: unittest.TestCase) -> None:
        folder = tempfile.TemporaryDirectory()
        test.addCleanup(folder.cleanup)
        self.messages: list[str] = []
        self.ctx = BuildContext(
            cache_dir=Path(folder.name),
            series=SeriesRegistry.load(),
            offline=True,
            log=self.messages.append,
        )

    def seed(self, url: str, name: str | None, fixture: str) -> None:
        target = self.ctx.cache_path(url, name)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(FIXTURES / fixture, target)

    def seed_archive(self, spec: ia.ArchiveSet, fixture: str) -> None:
        url = ia.download_url(spec.item, spec.file) + "/"
        self.seed(url, f"{spec.item}-listing.html", fixture)

    def seed_item(self, item: str, fixture: str) -> None:
        self.seed(f"{ia.ARCHIVE}/metadata/{urllib.parse.quote(item)}", f"{item}.json", fixture)


def scrape_url(query: str, cursor: str = "") -> str:
    fields = {"q": query, "fields": "identifier,title", "count": "1000"}
    if cursor:
        fields["cursor"] = cursor
    return f"{ia.ARCHIVE}/services/search/v1/scrape?{urllib.parse.urlencode(fields)}"


class ListingTest(unittest.TestCase):
    def setUp(self) -> None:
        text = (FIXTURES / "menus-listing.html").read_text()
        self.members = ia.parse_listing(text, ia.download_url(MENUS.item, MENUS.file) + "/")

    def test_file_rows_only(self) -> None:
        paths = [member.path for member in self.members]
        self.assertEqual(len(paths), 9)
        self.assertNotIn("[Menus]/A", paths)
        self.assertIn("[Menus]/P/Pompey Pirates/pp menu list.rtf", paths)

    def test_absolute_member_urls_and_sizes(self) -> None:
        member = self.members[0]
        self.assertEqual(member.path, "[Menus]/A/A-HA/A-HA Menu - Eliminator - Nebulus (A-Ha).zip")
        self.assertEqual(
            member.url,
            "https://archive.org/download/atari-st-collection/%5BMenus%5D.7z/"
            "%5BMenus%5D%2FA%2FA-HA%2FA-HA%20Menu%20-%20Eliminator%20-%20Nebulus%20%28A-Ha%29.zip",
        )
        self.assertEqual(member.size, 360979)

    def test_listing_without_table(self) -> None:
        self.assertEqual(ia.parse_listing("<html><body>Item unavailable</body></html>", ""), [])


class NamingTest(unittest.TestCase):
    def test_extension_from_platform(self) -> None:
        self.assertEqual(
            ia.image_name("[Menus]/A/Automation/Automation Menu Disk 100 (1989).zip", "atari-st"),
            "Automation Menu Disk 100 (1989).st",
        )
        self.assertEqual(
            ia.image_name("Compact #004 (1990)(Skid Row).zip", "amiga"),
            "Compact #004 (1990)(Skid Row).adf",
        )

    def test_extension_from_format_folder(self) -> None:
        path = (
            "Atari ST [TOSEC]/Applications/[STX]/Atari ST - Applications - [STX] "
            "(TOSEC-v2009-09-01_CM)/3D Construction Kit, The v1.10 (1991)(Domark)(M4)[!].zip"
        )
        self.assertEqual(
            ia.image_name(path, "atari-st"),
            "3D Construction Kit, The v1.10 (1991)(Domark)(M4)[!].stx",
        )

    def test_flags_in_the_file_name_are_not_formats(self) -> None:
        self.assertEqual(ia.image_name("[Menus]/X/Menu 1 [STX].zip", "atari-st"), "Menu 1 [STX].st")

    def test_non_zip_members_are_skipped(self) -> None:
        self.assertIsNone(ia.image_name("[Menus]/P/Pompey Pirates/pp menu list.rtf", "atari-st"))


class RecordsTest(unittest.TestCase):
    def test_archive_members_become_location_only_records(self) -> None:
        offline = Offline(self)
        offline.seed_archive(MENUS, "menus-listing.html")
        records = list(ia.archive_records(offline.ctx, MENUS))
        self.assertEqual(len(records), 8)
        record = records[0]
        self.assertIsNone(record.key)
        self.assertEqual((record.series_key, record.images, record.contents), ("", [], []))
        self.assertEqual(
            (record.source, record.platform, record.kind), (ia.INFO.id, "atari-st", "menu")
        )
        [location] = record.locations
        self.assertEqual(location.provider, "internet-archive")
        self.assertEqual((location.container, location.member), ("zip", ""))
        self.assertEqual(location.image_name, "A-HA Menu - Eliminator - Nebulus (A-Ha).st")
        self.assertEqual(location.page_url, "https://archive.org/details/atari-st-collection")
        self.assertEqual((location.priority, location.size), (20, 360979))
        self.assertEqual((location.hash_kind, location.hash_value), ("", ""))

    def test_older_mirror_is_tried_later(self) -> None:
        offline = Offline(self)
        offline.seed_archive(TOSEC_2012, "tosec2012-listing.html")
        records = list(ia.archive_records(offline.ctx, TOSEC_2012))
        names = [record.locations[0].image_name for record in records]
        self.assertEqual(
            names,
            [
                "3D Construction Kit, The v1.10 (1991)(Domark)(M4)[!].stx",
                "Pompey Pirates Menu Disk 001 (19xx)(Pompey Pirates).st",
                "Pompey Pirates Menu Disk 001 (19xx)(Pompey Pirates)[a].st",
            ],
        )
        self.assertEqual({record.locations[0].priority for record in records}, {25})

    def test_only_included_folders_are_read(self) -> None:
        offline = Offline(self)
        spec = ia.ArchiveSet(
            TOSEC_2012.item,
            TOSEC_2012.file,
            "atari-st",
            "",
            25,
            ("Atari ST [TOSEC]/Compilations/",),
        )
        offline.seed_archive(spec, "tosec2012-listing.html")
        records = list(ia.archive_records(offline.ctx, spec))
        self.assertEqual(len(records), 2)
        self.assertTrue(records[0].locations[0].image_name.startswith("Pompey Pirates"))

    def test_item_files_become_downloads(self) -> None:
        offline = Offline(self)
        offline.seed_item(AMIGA.item, "metadata.json")
        records = list(ia.item_records(offline.ctx, AMIGA))
        self.assertEqual(len(records), 2)
        [location] = records[1].locations
        self.assertEqual(
            location.url,
            "https://archive.org/download/commodore-amiga-compilations-games/"
            "3D%20World%20Soccer%20%26%20Cubix%20%26%20Encounter%20%281991%29%28Pendale%20Europa%29.zip",
        )
        self.assertEqual(
            location.image_name, "3D World Soccer & Cubix & Encounter (1991)(Pendale Europa).adf"
        )
        self.assertEqual(location.size, 793482)
        self.assertEqual(records[1].platform, "amiga")

    def test_dark_item_has_no_files(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            (Path(folder) / "dark.json").write_text(
                '{"is_dark": true, "files": [{"name": "a.zip"}]}'
            )
            offline = Offline(self)
            offline.seed_item("dark", str(Path(folder) / "dark.json"))
            self.assertEqual(ia.item_files(offline.ctx, "dark"), [])


class DiscoveryTest(unittest.TestCase):
    def test_all_pages_are_read_and_titles_filtered(self) -> None:
        offline = Offline(self)
        [discovery] = ia.DISCOVERIES
        offline.seed(scrape_url(discovery.query), "scrape.json", "scrape-1.json")
        cursor = "W3siaWRlbnRpZmllciI6ImNvbW1vZG9yZSJ9XQ=="
        offline.seed(scrape_url(discovery.query, cursor), "scrape.json", "scrape-2.json")
        self.assertEqual(
            ia.discover(offline.ctx, discovery),
            [
                ("commodore-amiga-compilations-games", "Commodore Amiga - Compilations - Games"),
                ("commodore-amiga-games-adf-r_202301", "Commodore Amiga - Games - [ADF] (T)"),
            ],
        )


class CollectTest(unittest.TestCase):
    def test_unavailable_sets_are_logged_and_skipped(self) -> None:
        offline = Offline(self)
        offline.seed_archive(MENUS, "menus-listing.html")
        offline.seed_item(AMIGA.item, "metadata.json")
        missing = ia.ArchiveSet("gone", "gone.zip", "atari-st", "", 20)
        with (
            mock.patch.object(ia, "ARCHIVE_SETS", (missing, MENUS)),
            mock.patch.object(ia, "ITEM_SETS", (AMIGA,)),
            mock.patch.object(ia, "DISCOVERIES", ()),
        ):
            records = list(ia.collect(offline.ctx))
        self.assertEqual(len(records), 10)
        self.assertTrue(any("gone/gone.zip unavailable" in m for m in offline.messages))
        self.assertIn(
            "internet-archive: atari-st-collection/[Menus].7z: 8 locations", offline.messages
        )
        self.assertEqual(offline.messages[-1], "internet-archive: 10 locations from 3 sets")


if __name__ == "__main__":
    unittest.main()

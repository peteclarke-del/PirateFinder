"""Tests for the Internet Archive importer: listings, naming and locations."""

from __future__ import annotations

import dataclasses
import datetime
import hashlib
import io
import json
import shutil
import tempfile
import unittest
import urllib.parse
import zipfile
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
        self.assertEqual(
            offline.messages[-1],
            "internet-archive: 10 locations from 3 sets; 0 with a hash from an old TOSEC DAT, "
            "1 recognised by a series rule",
        )


# An old DAT in the clrmamepro text format of the 2012 packs.
OLD_2012 = """clrmamepro (
\tname "Atari ST - Compilations - Games - [ST]"
\tdescription "Atari ST - Compilations - Games - [ST] (TOSEC-v2011-08-31)"
)

game (
\tname "A-Ha Menu - Eliminator - Nebulus (A-Ha)"
\trom ( name "A-HA Menu - Eliminator - Nebulus (A-Ha).st" size 737280 crc a23d118d md5 dc44467fdfcea70672c74fa7fb6f7191 sha1 87addd20a56faf4936a25bde29aad507972e165d )
)

game (
\tname "Only A CRC (1990)(Crew)"
\trom ( name "Only A CRC (1990)(Crew).st" size 368640 crc 0badf00d )
)
"""
# A newer DAT, XML with a byte order mark, naming one image differently.
OLD_2020 = (
    '\ufeff<?xml version="1.0"?>\n<datafile><game name="x">'
    '<rom name="A-HA Menu - Eliminator - Nebulus (A-Ha).st" size="737280" crc="11111111" '
    'md5="22222222222222222222222222222222" sha1="3333333333333333333333333333333333333333"/>'
    "</game></datafile>"
)


def old_dat(offline: Offline, dat_id: str, released: str, file_name: str, data: bytes):
    """An OldDat served from the offline cache, with its SHA-1."""
    url = f"https://dats.example/{urllib.parse.quote(file_name)}"
    target = offline.ctx.cache_path(url, file_name)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    return ia.OldDat(
        dat_id,
        datetime.date.fromisoformat(released),
        url,
        hashlib.sha1(data).hexdigest(),
        (ia.re.compile("^Atari ST - Compilations - "),),
    )


def zipped(members: dict[str, str]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, text in members.items():
            archive.writestr(name, text.encode("utf-8"))
    return buffer.getvalue()


class OldNamesTest(unittest.TestCase):
    def setUp(self) -> None:
        self.offline = Offline(self)
        self.dats = [
            old_dat(
                self.offline,
                "2012",
                "2012-09-15",
                "pack-2012.zip",
                zipped(
                    {
                        "TOSEC/Atari ST - Compilations - Games - [ST] (TOSEC-v2011-08-31_CM).dat": (
                            OLD_2012
                        ),
                        "TOSEC/Atari ST - Coverdisks (TOSEC-v2011-08-31_CM).dat": OLD_2012.replace(
                            "A-HA Menu", "Cover Disk"
                        ),
                    }
                ),
            ),
            old_dat(
                self.offline,
                "2020",
                "2020-07-29",
                "Atari ST - Compilations - Games - [ST] (TOSEC-v2020-07-29_CM).dat",
                OLD_2020.encode("utf-8"),
            ),
        ]
        self.old = ia.OldNames.load(self.offline.ctx, self.dats)
        self.name = "A-HA Menu - Eliminator - Nebulus (A-Ha).st"

    def test_the_dat_nearest_the_upload_date_is_asked_first(self) -> None:
        found = self.old.lookup(self.name, datetime.date(2013, 3, 6))
        self.assertEqual(found, ia.ImageHash("sha1", "87addd20a56faf4936a25bde29aad507972e165d"))
        found = self.old.lookup(self.name, datetime.date(2021, 11, 5))
        self.assertEqual(found.value, "3333333333333333333333333333333333333333")
        # Without an upload date the newest DAT is asked first.
        self.assertEqual(self.old.lookup(self.name, None).value, found.value)

    def test_only_the_wanted_dats_of_a_pack_are_read_and_names_keep_their_format(self) -> None:
        self.assertIsNone(self.old.lookup("Cover Disk - Eliminator - Nebulus (A-Ha).st", None))
        self.assertIsNone(self.old.lookup("A-HA Menu - Eliminator - Nebulus (A-Ha).msa", None))

    def test_a_rom_with_only_a_crc_gives_the_crc_and_the_image_size(self) -> None:
        found = self.old.lookup("Only A CRC (1990)(Crew).st", datetime.date(2012, 4, 23))
        self.assertEqual(found, ia.ImageHash("crc32", "0badf00d", 368640))
        record = ia.location_record(
            url="https://ia/crc.zip",
            name="Only A CRC (1990)(Crew).st",
            platform="atari-st",
            kind="",
            item="item",
            size=1234,
            priority=25,
            found=found,
        )
        [location] = record.locations
        self.assertEqual((location.hash_kind, location.hash_value), ("crc32", "0badf00d"))
        self.assertEqual(location.size, 368640)

    def test_a_dat_with_the_wrong_sha1_is_left_out(self) -> None:
        wrong = [dataclasses.replace(self.dats[0], sha1="0" * 40)]
        old = ia.OldNames.load(self.offline.ctx, wrong)
        self.assertEqual(old.indexes, [])
        self.assertTrue(any("old DAT 2012 unavailable" in m for m in self.offline.messages))

    def test_archive_members_carry_the_hash_of_their_old_name(self) -> None:
        self.offline.seed_archive(MENUS, "menus-listing.html")
        with tempfile.TemporaryDirectory() as folder:
            metadata = Path(folder) / "menus.json"
            metadata.write_text(json.dumps({"metadata": {"publicdate": "2019-03-02 10:00:00"}}))
            self.offline.seed_item(MENUS.item, str(metadata))
            records = list(ia.archive_records(self.offline.ctx, MENUS, self.old))
        first = records[0].locations[0]
        self.assertEqual(first.image_name, self.name)
        # 2019 is nearer 2020 than 2012.
        self.assertEqual((first.hash_kind, first.hash_value), ("sha1", "3" * 40))
        self.assertEqual(records[1].locations[0].hash_value, "")


def listing(spec: ia.ArchiveSet, paths: list[str], *, damage: int = 0) -> str:
    """An Archive listing of ``paths`` in ``spec``; ``damage`` drops that many
    leading words from every path but the first, as the Archive's 7z listings do."""
    base = (
        f"//archive.org/download/{urllib.parse.quote(spec.item)}/{urllib.parse.quote(spec.file)}/"
    )
    rows = []
    for index, path in enumerate(paths):
        shown = path if index == 0 or not damage else path.split(" ", damage)[-1]
        href = base + urllib.parse.quote(shown, safe="")
        rows.append(
            f'<tr><td><a href="{href}">{shown}</a><td><td>2023-11-07<td id="size">819200</tr>'
        )
    return (
        '<html><body><table class="archext"><caption>listing</caption>'
        "<tr><th>file<th>as jpg<th>timestamp<th>size</tr>"
        + "".join(rows)
        + "</table></body></html>"
    )


def seed_listing(offline: Offline, spec: ia.ArchiveSet, text: str) -> None:
    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder) / "listing.html"
        path.write_text(text, encoding="utf-8")
        url = ia.download_url(spec.item, spec.file) + "/"
        target = offline.ctx.cache_path(url, f"{spec.item}-listing.html")
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, target)


class RawSetTest(unittest.TestCase):
    """Whole TOSEC sets of raw images: Name/Name.st, and damaged 7z listings."""

    FIX = ia.ArchiveSet(
        "fix-item", "FixDat_Atari ST - Games - [ST].zip", "atari-st", "", 20, current=True
    )
    FLAT = ia.ArchiveSet("np-item", "Atari ST - Games - [ST].7z", "atari-st", "", 24, repair="flat")
    FOLDERS = ia.ArchiveSet("full-item", "Amiga Games.7z", "amiga", "", 26, repair="folders")

    def test_a_raw_member_is_its_own_image_with_no_container(self) -> None:
        offline = Offline(self)
        name = "Toki (1991)(Ocean)[cr D-Bug][t][2MB].st"
        seed_listing(offline, self.FIX, listing(self.FIX, [f"{name[:-3]}/{name}"]))
        [record] = ia.archive_records(offline.ctx, self.FIX)
        [location] = record.locations
        self.assertEqual((location.image_name, location.container), (name, ""))
        self.assertEqual(record.key, None)
        self.assertTrue(location.url.endswith(urllib.parse.quote(f"{name[:-3]}/{name}", safe="")))

    def test_a_set_with_current_names_is_not_placed_by_an_old_dat(self) -> None:
        offline = Offline(self)
        name = "A-HA Menu - Eliminator - Nebulus (A-Ha).st"
        seed_listing(offline, self.FIX, listing(self.FIX, [f"{name[:-3]}/{name}"]))
        old = mock.Mock()
        old.lookup.return_value = ia.ImageHash("sha1", "1" * 40)
        [record] = ia.archive_records(offline.ctx, self.FIX, old)
        self.assertEqual(record.locations[0].hash_value, "")
        old.lookup.assert_not_called()

    def test_a_flat_7z_listing_gets_its_folder_back(self) -> None:
        offline = Offline(self)
        names = ["'Nam (1991)(Domark).st", "Klax (1990)(Tengen).st", "Zool (1992)(Gremlin).st"]
        paths = [f"Atari ST - Games - [ST]/{name}" for name in names]
        seed_listing(offline, self.FLAT, listing(self.FLAT, paths, damage=1))
        records = list(ia.archive_records(offline.ctx, self.FLAT))
        self.assertEqual([record.locations[0].image_name for record in records], names)
        base = ia.download_url(self.FLAT.item, self.FLAT.file) + "/"
        self.assertEqual(
            [record.locations[0].url for record in records],
            [base + urllib.parse.quote(path, safe="") for path in paths],
        )

    def test_a_7z_listing_of_folders_named_after_their_images_is_rebuilt(self) -> None:
        offline = Offline(self)
        names = [
            "Hard Drivin' (1989)(Domark)[b] & Paperboy (1989)(Elite)[cr MCA].adf",
            "Zool - Ninja of the Nth Dimension (1992)(Gremlin).adf",
        ]
        paths = [f"{name[:-4]}/{name}" for name in names]
        seed_listing(offline, self.FOLDERS, listing(self.FOLDERS, paths, damage=3))
        records = list(ia.archive_records(offline.ctx, self.FOLDERS))
        self.assertEqual([record.locations[0].image_name for record in records], names)
        base = ia.download_url(self.FOLDERS.item, self.FOLDERS.file) + "/"
        self.assertEqual(records[1].locations[0].url, base + urllib.parse.quote(paths[1], safe=""))

    def test_a_member_whose_file_name_is_damaged_is_dropped(self) -> None:
        members = [ia.Member("Set/Good.st", "u1", 1), ia.Member("Bad.st", "u2", 1)]
        repaired = ia.repair_paths(members, "flat", "https://ia/x.7z/")
        self.assertEqual([member.path for member in repaired], ["Set/Good.st"])


class SeriesRuleTest(unittest.TestCase):
    def test_a_short_menu_name_becomes_an_attach_only_keyed_record(self) -> None:
        offline = Offline(self)
        offline.seed_archive(MENUS, "menus-listing.html")
        records = list(ia.archive_records(offline.ctx, MENUS))
        [keyed] = [record for record in records if record.key is not None]
        self.assertEqual(keyed.key, ("pompey-pirates", 54, "", ""))
        self.assertTrue(keyed.attach_only)
        [location] = keyed.locations
        self.assertEqual((location.image_name, location.hash_value), ("", ""))
        self.assertTrue(location.url.endswith("%2FPP_054.zip"))
        self.assertEqual(location.page_url, "https://archive.org/details/atari-st-collection")

    def test_a_vectronix_lzh_joins_its_disc_by_number(self) -> None:
        offline = Offline(self)
        spec = next(spec for spec in ia.ARCHIVE_SETS if spec.item == ia.VECTRONIX)
        paths = ["AREA.1/159.LZH", "AREA.3/604.LZH", "AREA.5/F011.LZH", "TOOLS/LZH.TTP"]
        seed_listing(offline, spec, listing(spec, paths))
        records = list(ia.archive_records(offline.ctx, spec))
        from catalogue_builder.merge import canonical_key

        # The Falcon disks and the tools are outside the numbered series.
        self.assertEqual(
            [canonical_key(record) for record in records],
            [("vectronix", 159, "", ""), ("vectronix", 604, "", "")],
        )
        self.assertTrue(all(record.attach_only for record in records))
        [location] = records[1].locations
        self.assertEqual(
            (location.container, location.image_name, location.hash_value), ("lzh", "", "")
        )
        self.assertTrue(location.url.endswith("VECTRONIX1.iso/AREA.3%2F604.LZH"))
        self.assertEqual(location.page_url, f"https://archive.org/details/{ia.VECTRONIX}")

    def test_rules_give_parts_and_versions(self) -> None:
        ctx = Offline(self).ctx
        cases = {
            "[Menus]/Z/Zuul/ZUUL032A.zip": ("zuul", 32, "A", ""),
            "[Menus]/Z/Zuul/ZUUL004vbis.zip": ("zuul", 4, "", "v2"),
            "[Menus]/F/Fuzion/FUZ024V1.zip": ("fuzion", 24, "", ""),
            "[Menus]/P/Pompey Pirates/PP_013_2.zip": ("pompey-pirates", 13, "B", ""),
            "[Menus]/L/Lemmings/TLS02V3A.zip": ("lemmings", 2, "A", "v3"),
            "[Menus]/W/World's Picture Collection/WPC048B.zip": (
                "the-world-s-picture-collection",
                48,
                "B",
                "",
            ),
        }
        from catalogue_builder.merge import canonical_key

        for path, key in cases.items():
            record = ia.series_record(
                ctx, path=path, url="u", platform="atari-st", item="i", size=None, priority=20
            )
            self.assertEqual(canonical_key(record), key, path)
        # A TOSEC-named zip is left to be placed by its name.
        self.assertIsNone(
            ia.series_record(
                ctx,
                path="[Menus]/D/Dodgysoft/Dodgysoft Menu 034 (1991)(Dodgysoft).zip",
                url="u",
                platform="atari-st",
                item="i",
                size=None,
                priority=20,
            )
        )


if __name__ == "__main__":
    unittest.main()

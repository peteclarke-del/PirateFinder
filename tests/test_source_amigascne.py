"""The amigascne importers, run offline on an excerpt of the archive index."""

from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from catalogue_builder.context import BuildContext
from catalogue_builder.series import DATA_DIR, SeriesRegistry
from catalogue_builder.sources import amigascne, amigascne_menus

FIXTURES = Path(__file__).parent / "fixtures" / "amigascne"

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
id = "trsi-tuff-stuff"
name = "Tuff Stuff"
platform = "amiga"
kind = "pack"
group = "TRSI"

[[series]]
id = "group-selection"
name = "Group Selection"
platform = "amiga"
kind = "pack"
group = "Citron"
"""


def make_context(temp: Path, rules: bool = True) -> BuildContext:
    series = temp / "series"
    series.mkdir()
    (series / "series.toml").write_text(SERIES)
    if rules:
        shutil.copy(DATA_DIR / "match-amigascne.toml", series)
        # The rules file names every Amiga series it knows; declare the others.
        known = {"skid-row-compact", "prevail-pack", "trsi-tuff-stuff"}
        text = (series / "match-amigascne.toml").read_text()
        others = sorted(
            {line.split('"')[1] for line in text.splitlines() if line.startswith("series = ")}
            - known
        )
        (series / "others.toml").write_text(
            "".join(
                f'[[series]]\nid = "{name}"\nname = "{name}"\nplatform = "amiga"\nkind = "pack"\n\n'
                for name in others
            )
        )
    ctx = BuildContext(
        cache_dir=temp / "cache",
        series=SeriesRegistry.load(series),
        offline=True,
        log=lambda message: None,
    )
    target = ctx.cache_path(amigascne.INDEX)
    target.parent.mkdir(parents=True)
    shutil.copy(FIXTURES / "amigascne-index.txt", target)
    return ctx


class AmigaScneTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.temp)
        self.ctx = make_context(self.temp)
        records = list(amigascne.collect(self.ctx))
        self.keyed = {record.key: record for record in records if record.key}
        self.loose = {record.title: record for record in records if record.key is None}

    def test_series_numbers_parts_and_other_sections_are_skipped(self) -> None:
        self.assertEqual(
            sorted(self.keyed),
            [
                ("prevail-pack", 147, "", ""),
                ("prevail-pack", 148, "", ""),
                ("skid-row-compact", 31, "", ""),
                ("skid-row-compact", 111, "", ""),
                ("skid-row-compact", 113, "B", ""),
                ("skid-row-compact", 128, "", ""),
                ("skid-row-compact", 130, "", ""),
                ("trsi-tuff-stuff", 12, "", ""),
            ],
        )

    def test_adf_carries_its_crc_but_dms_does_not(self) -> None:
        adf = self.keyed[("prevail-pack", 147, "", "")]
        [image] = adf.images
        self.assertEqual((image.format, image.crc32, image.size), ("adf", "a08396fd", 901120))
        [location] = adf.locations
        self.assertEqual(
            location.url,
            "https://ftp.scene.org/mirrors/amigascne/Packdisks/Effect/Effect-PrevailPack147.adf",
        )
        self.assertEqual(
            location.page_url, "https://ftp.scene.org/mirrors/amigascne/Packdisks/Effect/"
        )
        self.assertEqual((location.hash_kind, location.hash_value), ("crc32", "a08396fd"))
        self.assertEqual(
            (location.provider, location.container, location.priority), ("amigascne", "", 50)
        )
        self.assertEqual(location.image_name, image.name)
        dms = self.keyed[("skid-row-compact", 130, "", "")]
        self.assertEqual(dms.images, [])
        self.assertEqual((dms.locations[0].hash_kind, dms.locations[0].hash_value), ("", ""))
        self.assertEqual(dms.locations[0].size, 228418)

    def test_every_address_is_on_the_scene_org_mirror(self) -> None:
        # ftp.amigascne.org forbids automated access (robots.txt "Disallow: /"
        # and "Mirroring via ftp or http is strictly prohibited"), so neither
        # the builder nor the application may be sent there.
        records = [*self.keyed.values(), *self.loose.values()]
        addresses = [
            address
            for record in records
            for location in record.locations
            for address in (location.url, location.page_url)
        ]
        addresses += [amigascne.INDEX, amigascne.INFO.url, amigascne_menus.INFO.url]
        self.assertTrue(addresses)
        for address in addresses:
            self.assertTrue(address.startswith("https://ftp.scene.org/mirrors/amigascne/"), address)
            self.assertNotIn("amigascne.org", address)

    def test_files_of_one_disk_share_a_record(self) -> None:
        disk = self.keyed[("skid-row-compact", 31, "", "")]
        self.assertEqual(len(disk.locations), 2)  # the ADF and a bad DMS
        self.assertEqual(len(disk.images), 1)

    def test_other_groups_tuff_stuff_is_not_trsi(self) -> None:
        self.assertIn("Tuff Stuff 05 (DTect)", self.loose)
        self.assertNotIn(("trsi-tuff-stuff", 5, "", ""), self.keyed)

    def test_unrecognised_pack_keeps_title_and_hash(self) -> None:
        pack = self.loose["Group Selection 011 (Reflex)"]
        self.assertEqual(pack.kind, "pack")
        self.assertEqual(pack.platform, "amiga")
        self.assertEqual([image.crc32 for image in pack.images], ["50dec2f1"])
        self.assertEqual(len(pack.locations), 2)

    def test_name_fallback_needs_the_same_group(self) -> None:
        # "Group Selection" is declared for Citron, so Reflex's disk is not claimed.
        self.assertIsNone(
            amigascne.identify(self.ctx.series, "GroupSelection", "Reflex-GroupSelection011")
        )
        found = amigascne.identify(self.ctx.series, "Citron", "Citron-GroupSelection125")
        self.assertEqual((found.series_id, found.number), ("group-selection", 125))


class NameFallbackTest(unittest.TestCase):
    def test_series_name_matches_without_rules(self) -> None:
        temp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, temp)
        ctx = make_context(temp, rules=False)
        found = amigascne.identify(ctx.series, "Effect", "Effect-PrevailPack147")
        self.assertEqual((found.series_id, found.number), ("prevail-pack", 147))
        found = amigascne.identify(ctx.series, "Skid_Row", "SKID_ROW-Compact130")
        self.assertEqual((found.series_id, found.number), ("skid-row-compact", 130))


class MenuTextTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.temp)
        self.ctx = make_context(self.temp)
        entries = list(amigascne.parse_index(amigascne.fetch_index(self.ctx)))
        self.plan = list(amigascne_menus.plan(self.ctx, entries))

    def test_only_texts_tied_to_a_disk_are_planned(self) -> None:
        paths = [path for path, _record in self.plan]
        self.assertEqual(
            paths,
            [
                "Scrollers/S-Groupstext/Skid_Row/Skid_Row-Compact031-menu.txt",
                "Scrollers/R-Groupstext/Reflex/Reflex-GroupSelection011-menu.txt",
            ],
        )
        keyed, loose = (record for _path, record in self.plan)
        self.assertEqual(keyed.key, ("skid-row-compact", 31, "", ""))
        self.assertTrue(keyed.attach_only)
        self.assertEqual([image.crc32 for image in loose.images], ["50dec2f1"])

    def test_menu_texts_are_built_by_default(self) -> None:
        # The mirror allows automated access, and the texts are cached for 30
        # days, so every build includes them.
        self.assertTrue(amigascne_menus.DEFAULT_ENABLED)

    def test_text_is_fetched_cleaned_and_limited(self) -> None:
        path = "Scrollers/S-Groupstext/Skid_Row/Skid_Row-Compact031-menu.txt"
        target = self.ctx.cache_path(amigascne.file_url(path))
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(FIXTURES / "Skid_Row-Compact031-menu.txt", target)
        messages: list[str] = []
        self.ctx.log = messages.append
        with mock.patch.dict(os.environ, {amigascne_menus.LIMIT_VARIABLE: "1"}):
            [record] = list(amigascne_menus.collect(self.ctx))
        # The second planned text is not cached; the limit stops before asking for it.
        self.assertEqual(messages, ["amigascne-menus: 1 menu texts, 0 failed"])
        self.assertEqual(
            record.menu_text, "SKID ROW PRESENTS COMPACT 31\nF1 - GAME ONE +2\nF2 - GAME TWO"
        )
        self.assertEqual(record.source, "amigascne-menus")


if __name__ == "__main__":
    unittest.main()

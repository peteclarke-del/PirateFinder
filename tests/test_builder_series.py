"""Series definitions, TOSEC match rules and group abbreviations in data/."""

from __future__ import annotations

import re
import tempfile
import unittest
from pathlib import Path

from catalogue_builder.series import (
    DATA_DIR,
    GROUPS_FILE,
    GroupRegistry,
    SeriesDef,
    SeriesRegistry,
)
from piratefinder.catalogue.naming import normalise
from piratefinder.models import DiskKind, Platform

# Real TOSEC names and what they must be recognised as.
TOSEC_NAMES = {
    "Automation Menu Disk 250 (1990)(Automation)[a]": ("automation", 250, "", ""),
    "Automation Menu Disk 000 v2.0 (19xx)(Automation)": ("automation", 0, "", "v2.0"),
    "D-Bug Menu Disk 193 (2006-03-09)(D-Bug)(Disk 5 of 5)": ("d-bug", 193, "", ""),
    "Pompey Pirates Menu Disk 051 (19xx)(Pompey Pirates)": ("pompey-pirates", 51, "", ""),
    "Medway Boys Menu Disk 088 (1990)(Medway Boys)(Disk 1 of 2)(Part A)": (
        "medway-boys",
        88,
        "",
        "",
    ),
    "Flame of Finland Menu 54 (1991)(FOF)": ("flame-of-finland", 54, "", ""),
    "Zuul 004 bis (19xx)(Zuul)": ("zuul", 4, "", "bis"),
    "Vectronix Compilation 996 (19xx)(Vectronix)": ("vectronix", 996, "", ""),
    "SuperGAU Compilation 959 (19xx)(SuperGAU)": ("supergau", 959, "", ""),
    "Persistance of Vision 165 (1991)(POV)": ("persistence-of-vision", 165, "", ""),
    "Source Menu 028, The v2 (19xx)(The Source)[m Megatari]": ("the-source", 28, "", "v2"),
    "Delicious Disk 100 Game (19xx)(Syndicate)": ("delicious", 100, "GAME", ""),
    "Compact #128 (1992)(Skid Row)": ("skid-row-compact", 128, "", ""),
    "FDT Compacted Disk #110 (1988-10-08)(Alpha Flight - Spy & Mind)": (
        "alpha-flight-fdt",
        110,
        "",
        "",
    ),
    "Amiga Compact Disc #08 (19xx)(BSA Factories)": ("bsa-compact", 8, "", ""),
    "Compact Disk #04 (19xx)(Defjam - CCS)[m]": ("defjam-ccs-compact", 4, "", ""),
    "Prevail Pack #037 (1992)(Effect)[b loader][rebuilt]": ("prevail-pack", 37, "", ""),
    "Mind Funk #118 - The Party (1992-12)(The Electronic Knights)": ("tek-mind-funk", 118, "", ""),
    "Xad's Pack #06 (1991-08-15)(Nightfall)": ("nightfall-xads-pack", 6, "", ""),
    "Games Pack #01 (199x)(Effect)": ("effect-games-pack", 1, "", ""),
    "Tuff Stuff 01 (1991)(Rebels)": ("trsi-tuff-stuff", 1, "", ""),
    "Tuff Stuff #17 (1991)(TRSI)": ("trsi-tuff-stuff", 17, "", ""),
    "Tuff Stuff 55 (2016-08-09)(The Electronic Knights)": ("trsi-tuff-stuff", 55, "", ""),
}

NOT_SERIES = [
    "Compact #124 (1993)(Apollo)",
    "Games Pack #22 (19xx)(The Avenger Hawks)",
    "Pompey Pirates Menu Disk 51, 23, 86 (19xx)(Pompey Pirates)",
    "Games Galore 1200 Remix Disk 5 (1993)(Henderson, Gary)",
    "Tuff Stuff #05 (1990-11-13)(D-Tect)",
]


class SeriesDataTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.registry = SeriesRegistry.load()

    def test_every_declared_series_is_well_formed(self) -> None:
        platforms = {str(platform) for platform in Platform}
        kinds = {str(kind) for kind in DiskKind}
        for series in self.registry.all():
            with self.subTest(series=series.id):
                self.assertRegex(series.id, r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
                self.assertIn(series.platform, platforms)
                self.assertIn(series.kind, kinds)
                self.assertTrue(series.format_label(7).strip())
                for alias in series.aliases:
                    self.assertTrue(normalise(alias), alias)

    def test_key_series_are_declared_with_the_aliases_people_type(self) -> None:
        expected = {
            "automation": {"auto", "a", "au", "automation cd"},
            "pompey-pirates": {"pp", "pompey", "pompey pirates cd"},
            "medway-boys": {"mb", "medway"},
            "flame-of-finland": {"fof"},
            "d-bug": {"dbug", "d bug", "db"},
            "vectronix": {"vec", "ve"},
            "persistence-of-vision": {"pov"},
            "skid-row-compact": {"sr compact", "skid row", "skidrow compact"},
        }
        for series_id, aliases in expected.items():
            with self.subTest(series=series_id):
                self.assertLessEqual(aliases, set(self.registry.get(series_id).aliases))

    def test_tosec_patterns_recognise_real_names(self) -> None:
        for name, expected in TOSEC_NAMES.items():
            with self.subTest(name=name):
                found = self.registry.match("tosec", name)
                self.assertIsNotNone(found)
                self.assertEqual(
                    (found.series_id, found.number, found.part, found.version), expected
                )

    def test_tosec_patterns_leave_other_names_alone(self) -> None:
        for name in NOT_SERIES:
            with self.subTest(name=name):
                self.assertIsNone(self.registry.match("tosec", name))

    def test_every_tosec_pattern_has_a_number_group(self) -> None:
        for series in self.registry.all():
            for pattern in series.patterns.get("tosec", ()):
                self.assertIn("number", pattern.groupindex, f"{series.id}: {pattern.pattern}")

    def test_default_label_formats(self) -> None:
        # Guards a slots dataclass pitfall: SeriesDef.label read on the class
        # is a member descriptor, not the default format string.
        with tempfile.TemporaryDirectory() as folder:
            (Path(folder) / "s.toml").write_text(
                '[[series]]\nid = "x"\nname = "X Menu"\nplatform = "amiga"\nkind = "menu"\n'
            )
            series = SeriesRegistry.load(Path(folder)).get("x")
        self.assertEqual(series.format_label(12, "B", "v2"), "X Menu 12 B v2")
        self.assertEqual(SeriesDef("y", "Y", "amiga", "menu").format_label(3), "Y 3")

    def test_the_data_folder_is_the_one_in_the_repository(self) -> None:
        self.assertTrue((DATA_DIR / "series.toml").is_file())
        self.assertTrue((DATA_DIR / "match-tosec.toml").is_file())


class GroupsTest(unittest.TestCase):
    def test_abbreviations_expand(self) -> None:
        groups = GroupRegistry.load()
        self.assertEqual(groups.expand("QTX"), "Quartex")
        self.assertEqual(groups.expand("SR"), "Skid Row")
        self.assertEqual(groups.expand("CSL"), "Crystal")
        self.assertEqual(groups.expand("PDX"), "Paradox")
        self.assertEqual(groups.expand("FLT"), "Fairlight")
        self.assertEqual(groups.expand("VF"), "Vision Factory")
        self.assertEqual(groups.expand("Hotline - MCA"), "Hotline - MCA")
        self.assertEqual(groups.expand("QTX - SR"), "Quartex - Skid Row")
        self.assertEqual(groups.expand("skidrow"), "Skid Row")
        self.assertEqual(groups.spellings("QTX"), ["QTX", "Quartex"])

    def test_every_group_entry_is_well_formed(self) -> None:
        import tomllib

        document = tomllib.loads(GROUPS_FILE.read_text())
        # Two groups may share a name on different platforms ("The Exceptions").
        names = [(entry["name"], *entry.get("platforms", [])) for entry in document["group"]]
        self.assertEqual(len(names), len(set(names)), "a group is listed twice")
        tags = [
            (tag, platform)
            for entry in document["group"]
            for tag in entry.get("abbreviations", [])
            for platform in entry.get("platforms", ["every platform"])
        ]
        self.assertEqual(len(tags), len(set(tags)), "an abbreviation names two groups")
        for tag, platform in tags:
            self.assertRegex(tag, re.compile(r"^\S+$"))
            self.assertIn(platform, ("every platform", "amiga", "atari-st"))

    def test_an_abbreviation_can_mean_a_group_on_one_platform_only(self) -> None:
        groups = GroupRegistry.load()
        self.assertEqual(groups.expand("MCA", "atari-st"), "The Menacing Cracking Alliance")
        self.assertEqual(groups.expand("MCA", "amiga"), "MCA")
        self.assertEqual(groups.expand("MCA"), "MCA")
        self.assertEqual(groups.expand("ICS", "amiga"), "Italian Cracking Service")
        # On the ST, ICS is the name of a menu series and stays as it is.
        self.assertEqual(groups.expand("ICS", "atari-st"), "ICS")
        self.assertEqual(groups.expand("PSG - QTX", "amiga"), "Prestige - Quartex")
        self.assertEqual(groups.spellings("PSG", "amiga"), ["PSG", "Prestige"])
        self.assertEqual(groups.spellings("PSG", "atari-st"), ["PSG"])
        self.assertIn("prestige", groups.crew_keys("PSG", "amiga"))
        self.assertEqual(groups.crew_keys("PSG", "atari-st"), {"psg"})

    def test_a_tag_naming_two_groups_on_one_platform_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "groups.toml"
            path.write_text(
                '[[group]]\nname = "A"\nplatforms = ["amiga"]\nabbreviations = ["X"]\n'
                '[[group]]\nname = "B"\nplatforms = ["amiga", "atari-st"]\nabbreviations = ["X"]\n'
            )
            with self.assertRaisesRegex(ValueError, "names two groups"):
                GroupRegistry.load(path)
            path.write_text(path.read_text().replace('["amiga", "atari-st"]', '["atari-st"]'))
            groups = GroupRegistry.load(path)
            self.assertEqual(
                (groups.expand("X", "amiga"), groups.expand("X", "atari-st")), ("A", "B")
            )


if __name__ == "__main__":
    unittest.main()

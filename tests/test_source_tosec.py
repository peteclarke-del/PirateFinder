"""The TOSEC importer, on small excerpts of real DATs in tests/fixtures/tosec."""

from __future__ import annotations

import io
import tempfile
import unittest
import zipfile
from html import escape
from pathlib import Path

from catalogue_builder.context import BuildContext
from catalogue_builder.series import SeriesDef, SeriesRegistry
from catalogue_builder.sources import tosec

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "tosec"

DOWNLOADS_PAGE = """
<a href="/downloads/category/58-2024-05-17">2024-05-17</a>
<a href="/downloads/category/59-2025-03-13">2025-03-13</a>
<a href="/downloads/category/57-2023-07-10">2023-07-10</a>
"""
CATEGORY_PAGE = """
<a class="" href="/downloads/category/59-2025-03-13?download=117:tosec-dat-pack-complete-4743-tosec-v2025-03-13&amp;x=1">TOSEC - DAT Pack - Complete</a>
"""


def context(cache: Path, **options: object) -> BuildContext:
    return BuildContext(
        cache_dir=cache, series=SeriesRegistry.load(), log=lambda _: None, **options
    )


def collect(inputs: Path) -> tuple[BuildContext, list]:
    with tempfile.TemporaryDirectory() as cache:
        ctx = context(Path(cache), inputs={"tosec": inputs})
        return ctx, list(tosec.collect(ctx))


def by_key(records: list) -> dict:
    return {record.key: record for record in records if record.key is not None}


class FetchingContext(BuildContext):
    """A context that serves the TOSEC pages from memory and records fetches."""

    fetched: list

    def fetch(self, url: str, *, name: str | None = None, **options: object) -> Path:
        self.fetched.append((url, name))
        target = self.cache_path(url, name)
        target.parent.mkdir(parents=True, exist_ok=True)
        if url.endswith("/downloads"):
            target.write_text(DOWNLOADS_PAGE)
        elif "?download=" in url:
            target.write_bytes(b"zip")
        else:
            target.write_text(CATEGORY_PAGE)
        return target


class SeriesDisksTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.ctx, cls.records = collect(FIXTURES)
        cls.keyed = by_key(cls.records)

    def test_declared_series_are_recognised_and_alternates_collapse(self) -> None:
        record = self.keyed[("automation", 250, "", "")]
        self.assertEqual(record.kind, "menu")
        self.assertEqual(record.title, "Automation Menu Disk 250 (1990)(Automation)")
        flags = [image.flags for image in record.images]
        self.assertEqual(flags, ["", "[a]", "[m LGD]"])
        self.assertEqual(record.images[0].format, "st")
        self.assertEqual(len(record.images[0].sha1), 40)

    def test_version_suffix_is_kept_in_one_spelling(self) -> None:
        record = self.keyed[("automation", 0, "", "v2")]
        self.assertEqual(record.version, "v2")
        self.assertEqual(len(record.images), 4)

    def test_disk_of_and_part_fields_give_the_part(self) -> None:
        self.assertIn(("d-bug", 100, "A", ""), self.keyed)
        self.assertIn(("d-bug", 100, "B", ""), self.keyed)
        self.assertIn(("automation", 69, "A", ""), self.keyed)

    def test_combined_numbers_are_not_taken_for_one_disk(self) -> None:
        self.assertIn(("pompey-pirates", 51, "", ""), self.keyed)
        unkeyed = [r for r in self.records if r.key is None and r.title.startswith("Pompey")]
        self.assertEqual(len(unkeyed), 1)
        self.assertIn("51, 23, 86", unkeyed[0].title)

    def test_publisher_in_the_pattern_tells_series_apart(self) -> None:
        self.assertIn(("skid-row-compact", 128, "", ""), self.keyed)
        apollo = [r for r in self.records if "Apollo" in r.title]
        self.assertEqual(len(apollo), 1)
        self.assertIsNone(apollo[0].key)
        bad = self.keyed[("skid-row-compact", 31, "", "")]
        self.assertTrue(bad.images[0].bad)

    def test_numbered_names_register_a_series(self) -> None:
        self.assertIn(("games-compil", 2, "", ""), self.keyed)
        self.assertIn(("games-compil", 3, "", ""), self.keyed)
        series = self.ctx.series.get("games-compil")
        self.assertEqual((series.platform, series.kind), ("atari-st", "menu"))
        self.assertEqual(series.format_label(2), "Games Compil 2")

    def test_compilation_names_list_their_contents(self) -> None:
        aha = next(r for r in self.records if r.title.startswith("A-Ha Menu"))
        self.assertEqual([c.title for c in aha.contents], ["Eliminator", "Nebulus"])
        tmf = next(r for r in self.records if r.title.startswith("TMF Compact - "))
        self.assertEqual(
            [c.title for c in tmf.contents],
            ["Quadralien", "Carrier Command", "Championship Cricket"],
        )
        combined = next(r for r in self.records if r.title.startswith("Imperium"))
        self.assertEqual(combined.kind, "compilation")
        self.assertEqual([c.title for c in combined.contents], ["Imperium", "Pyramax"])
        # MCA is the Atari ST tag of The Menacing Cracking Alliance (data/groups.toml).
        self.assertEqual(combined.contents[0].cracker, "Hotline - The Menacing Cracking Alliance")


class DeclaredByNameTest(unittest.TestCase):
    def test_a_numbered_name_joins_a_declared_series_of_the_same_publisher(self) -> None:
        registry = SeriesRegistry(
            {
                "x-pack": SeriesDef(
                    "x-pack", "X Pack", "amiga", "pack", "Xcrew", aliases=["zap disk"]
                )
            }
        )
        with tempfile.TemporaryDirectory() as cache:
            ctx = BuildContext(cache_dir=Path(cache), series=registry, log=lambda _: None)
        games = "".join(
            f'<game name="Zap Disk #{n} (1990)({crew})"><rom name="{crew}{n}.adf" '
            f'size="1" crc="0000000{n}" md5="{crew}{n}" sha1="{crew}{n}"/></game>'
            for crew in ("Xcrew", "Other")
            for n in (1, 2)
        )
        dat = f"<datafile><header><name>x</name></header>{games}</datafile>".encode()
        dat_set = next(d for d in tosec.DAT_SETS if d.name == "Commodore Amiga - Demos - Packs")
        keys = sorted(record.key for record in tosec.read_dat(ctx, dat_set, io.BytesIO(dat)))
        self.assertEqual(
            keys,
            [
                ("other-zap-disk", 1, "", ""),
                ("other-zap-disk", 2, "", ""),
                ("x-pack", 1, "", ""),
                ("x-pack", 2, "", ""),
            ],
        )
        self.assertEqual(ctx.series.get("other-zap-disk").format_label(2), "Zap Disk 2 (Other)")


class SingleDisksTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.ctx, cls.records = collect(FIXTURES)
        cls.singles = [r for r in cls.records if r.kind == "single"]

    def test_dumps_of_one_release_collapse(self) -> None:
        chaos = [r for r in self.singles if r.title.startswith("Chaos Engine")]
        self.assertEqual(len(chaos), 1)
        record = chaos[0]
        self.assertEqual(len(record.images), 6)
        self.assertEqual(record.part, "1 of 2")
        self.assertEqual(record.cracker, "Cynix")
        self.assertEqual(record.title, "Chaos Engine, The (1993)(Renegade)(Disk 1 of 2)[cr Cynix]")
        self.assertEqual(
            [image.flags for image in record.images],
            [
                "[cr Cynix]",
                "[cr Cynix][a]",
                "[cr Cynix][a2]",
                "[cr Cynix][a3]",
                "[cr Cynix][t]",
                "[cr Cynix][t][a]",
            ],
        )
        content = record.contents[0]
        self.assertEqual((content.title, content.publisher), ("The Chaos Engine", "Renegade"))

    def test_different_cracks_stay_apart(self) -> None:
        xenon = {r.cracker: r for r in self.singles if r.title.startswith("Xenon")}
        self.assertEqual(set(xenon), {"", "D-Bug", "Hernoice", "Outlaw", "Self"})
        self.assertEqual(len(xenon[""].images), 4)
        self.assertEqual(xenon[""].images[0].flags, "")
        self.assertEqual(len(xenon["Self"].images), 2)
        self.assertIn("[cr][t +2 Avengers]", [image.flags for image in xenon[""].images])
        self.assertEqual(xenon["Self"].contents[0].extra, "trainer")

    def test_crack_groups_are_expanded(self) -> None:
        dat = (
            b'<?xml version="1.0"?><datafile><header><name>x</name></header>'
            b'<game name="Xenon 2 - Megablast (1989)(Image Works)[cr QTX][t +2 SR]">'
            b'<rom name="x.adf" size="901120" crc="0000abcd" md5="aa" sha1="bb"/></game>'
            b"</datafile>"
        )
        dat_set = next(d for d in tosec.DAT_SETS if d.name == "Commodore Amiga - Games - [ADF]")
        (record,) = tosec.read_dat(self.ctx, dat_set, io.BytesIO(dat))
        self.assertEqual(record.cracker, "Quartex")
        self.assertEqual(record.contents[0].cracker, "Quartex")
        self.assertEqual(record.contents[0].extra, "+2 trainer by Skid Row")
        self.assertEqual(record.images[0].format, "adf")


class PackTest(unittest.TestCase):
    def test_reads_the_dats_from_a_zip(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            pack = Path(folder) / "TOSEC - DAT Pack - Complete (TOSEC-v2025-03-13).zip"
            with zipfile.ZipFile(pack, "w") as archive:
                for dat in FIXTURES.glob("*.dat"):
                    archive.write(dat, f"TOSEC/{dat.name}")
                archive.writestr("TOSEC-ISO/Atari ST - Games - [ST] (TOSEC-v1_CM).dat", "junk")
            _ctx, from_zip = collect(pack)
            self.assertEqual(tosec.RETRIEVED, "2025-03-13")
        _ctx, from_folder = collect(FIXTURES)
        self.assertEqual(len(from_zip), len(from_folder))
        self.assertEqual({r.title for r in from_zip}, {r.title for r in from_folder})

    def test_finds_the_newest_pack_on_the_downloads_page(self) -> None:
        with tempfile.TemporaryDirectory() as cache:
            ctx = FetchingContext(
                cache_dir=Path(cache), series=SeriesRegistry.load(), log=lambda _: None
            )
            ctx.fetched = []
            path = tosec._download_pack(ctx)
            urls = [url for url, _name in ctx.fetched]
            self.assertEqual(urls[1], "https://www.tosecdev.org/downloads/category/59-2025-03-13")
            self.assertEqual(
                urls[2],
                "https://www.tosecdev.org/downloads/category/59-2025-03-13"
                "?download=117:tosec-dat-pack-complete-4743-tosec-v2025-03-13&x=1",
            )
            self.assertTrue(path.name.endswith("tosec-dat-pack-2025-03-13.zip"))

    def test_offline_build_uses_a_cached_pack(self) -> None:
        with tempfile.TemporaryDirectory() as cache:
            cached = Path(cache) / "www.tosecdev.org" / "0123-tosec-dat-pack-2024-05-17.zip"
            cached.parent.mkdir()
            cached.write_bytes(b"zip")
            ctx = context(Path(cache), offline=True)
            self.assertEqual(tosec._download_pack(ctx), cached)


if __name__ == "__main__":
    unittest.main()


class JoinedNamesTest(unittest.TestCase):
    """Compilation names that join their titles with " & "."""

    NAMES = {
        "Rick Dangerous & Cybernoid II & Arkanoid - Revenge of Doh (19xx)(Dominators)": [
            "Rick Dangerous",
            "Cybernoid II",
            "Arkanoid - Revenge of Doh",
        ],
        "Astronut & Wise Man - Spike In Transylvania & Dizzy Diamonds (1992)"
        "(Astronut - Wise Man)": ["Spike In Transylvania", "Dizzy Diamonds"],
        "Amiga Games 9 - Missile & Cosmo & Defender (19xx)(-)(PD)": [
            "Missile",
            "Cosmo",
            "Defender",
        ],
        "Xenon 2 - Megablast & 4 Others (1990)(Imageworks)": [],
        "Othello & Cardsharp (19xx)(17-Bit Software)(PD)": ["Othello", "Cardsharp"],
        "Copy & Utility Disk v4.0 (1989)(Sphinx)": [],
        "Packer & Tools Disc 11 (19xx)(Penguin)": [],
        "Soundtracker & Sound-FX Systemdisk (1989-06-06)(Bamiga Sector One)": [],
        "Great Medusa, The - Mercenary 1 & 2 Collection (1990)(Novagen)": [],
        "Repton 1 & 2 & Editor (1989)(Superior)": ["Repton 1 & 2", "Editor"],
        "Another Great Pack - 2 Games (1990)(Crew)": [],
        "Saving Compilation Disk - Moonbase & 2 Others (1991)(Crew)": ["Moonbase"],
    }

    def test_joined_titles_become_contents_and_generic_pairs_do_not(self) -> None:
        with tempfile.TemporaryDirectory() as cache:
            ctx = BuildContext(cache_dir=Path(cache), series=SeriesRegistry({}), log=lambda _: None)
        names = list(self.NAMES)
        games = "".join(
            f'<game name="{escape(name)}"><rom name="{index}.adf" size="1" '
            f'crc="{index:08x}" md5="m{index}" sha1="s{index}"/></game>'
            for index, name in enumerate(names)
        )
        dat = f"<datafile><header><name>x</name></header>{games}</datafile>".encode()
        dat_set = next(
            d for d in tosec.DAT_SETS if d.name == "Commodore Amiga - Compilations - Games"
        )
        found = {
            names[int(record.images[0].name.split(".")[0])]: [c.title for c in record.contents]
            for record in tosec.read_dat(ctx, dat_set, io.BytesIO(dat))
        }
        for name, expected in self.NAMES.items():
            with self.subTest(name=name):
                self.assertEqual(found[name], expected)


class VirusFlagsTest(unittest.TestCase):
    """Each image carries the virus flags of its own dump."""

    def test_virus_damage_and_antivirus_flags_reach_the_images(self) -> None:
        with tempfile.TemporaryDirectory() as cache:
            ctx = BuildContext(cache_dir=Path(cache), series=SeriesRegistry({}), log=lambda _: None)
        names = [
            "Xenon (1988)(Melbourne House)",
            "Xenon (1988)(Melbourne House)[v Saddam 1]",
            "Xenon (1988)(Melbourne House)[b virus damage]",
            "Xenon (1988)(Melbourne House)[m The Medway Boys Protector IV]",
        ]
        games = "".join(
            f'<game name="{escape(name)}"><rom name="{escape(name)}.adf" size="1" '
            f'crc="{index:08x}" md5="m{index}" sha1="s{index}"/></game>'
            for index, name in enumerate(names)
        )
        dat = f"<datafile><header><name>x</name></header>{games}</datafile>".encode()
        dat_set = next(d for d in tosec.DAT_SETS if d.name == "Commodore Amiga - Games - [ADF]")
        (record,) = tosec.read_dat(ctx, dat_set, io.BytesIO(dat))
        flags = {
            image.name.split(")", 2)[-1]: (
                image.virus,
                image.virus_damage,
                image.antivirus,
                image.bad,
            )
            for image in record.images
        }
        self.assertEqual(
            flags,
            {
                ".adf": ("", False, "", False),
                "[v Saddam 1].adf": ("Saddam 1", False, "", False),
                "[b virus damage].adf": ("", True, "", True),
                "[m The Medway Boys Protector IV].adf": (
                    "",
                    False,
                    "The Medway Boys Protector IV",
                    False,
                ),
            },
        )

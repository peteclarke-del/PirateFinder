"""Generated disk definitions, checked against the real gw tool when it is installed.

The gw checks need no hardware: ``gw convert`` encodes a sector image
through a disk definition exactly as ``gw write`` would, so comparing its
output shows both that every cylinder survives and that a generated
definition encodes tracks exactly as gw's built-in one does.
"""

from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

from piratefinder.greaseweazle.client import find_gw
from piratefinder.greaseweazle.diskdefs import (
    DiskDefError,
    builtin_format,
    custom_definition,
    write_definition,
)
from piratefinder.images.prepare import prepare
from piratefinder.images.vendor.msa import parse_msa
from piratefinder.models import Geometry, Platform
from tests.test_images_synthetic import adf_image, msa_archive, pattern, st_image

GW = find_gw()


class DefinitionTests(unittest.TestCase):
    def test_builtin_formats(self) -> None:
        self.assertEqual(builtin_format(Geometry(80, 2, 10), Platform.ATARI_ST), "atarist.800")
        self.assertEqual(builtin_format(Geometry(80, 1, 9), Platform.ATARI_ST), "atarist.360")
        self.assertEqual(builtin_format(Geometry(80, 2, 11), Platform.AMIGA), "amiga.amigados")
        self.assertEqual(builtin_format(Geometry(80, 2, 22), Platform.AMIGA), "amiga.amigados_hd")
        self.assertIsNone(builtin_format(Geometry(82, 2, 10), Platform.ATARI_ST))
        self.assertIsNone(builtin_format(Geometry(81, 2, 11), Platform.AMIGA))

    def test_st_definition_matches_the_verified_example(self) -> None:
        definition = custom_definition(Geometry(82, 2, 10), Platform.ATARI_ST)
        self.assertEqual(definition.name, "st_82_2_10")
        self.assertEqual(
            definition.text,
            "disk st_82_2_10\n"
            "    cyls = 82\n"
            "    heads = 2\n"
            "    tracks * ibm.mfm\n"
            "        secs = 10\n"
            "        bps = 512\n"
            "        gap3 = 30\n"
            "        rate = 250\n"
            "        iam = no\n"
            "    end\n"
            "end\n",
        )

    def test_nine_sector_skews_follow_the_head_count(self) -> None:
        single = custom_definition(Geometry(82, 1, 9), Platform.ATARI_ST).text
        double = custom_definition(Geometry(82, 2, 9), Platform.ATARI_ST).text
        self.assertIn("cskew = 2", single)
        self.assertNotIn("hskew", single)
        self.assertIn("cskew = 4", double)
        self.assertIn("hskew = 2", double)

    def test_eleven_sectors_use_the_faster_rate(self) -> None:
        text = custom_definition(Geometry(83, 2, 11), Platform.ATARI_ST).text
        self.assertIn("gap3 = 3", text)
        self.assertIn("rate = 261", text)

    def test_amiga_definition(self) -> None:
        definition = custom_definition(Geometry(82, 2, 11), Platform.AMIGA)
        self.assertEqual(definition.name, "amiga_82_2_11")
        self.assertIn("tracks * amiga.amigados", definition.text)
        self.assertIn("secs = 11", definition.text)

    def test_layouts_without_parameters_are_refused(self) -> None:
        for geometry, platform in (
            (Geometry(82, 2, 12), Platform.ATARI_ST),
            (Geometry(82, 2, 10), Platform.AMIGA),
            (Geometry(120, 2, 10), Platform.ATARI_ST),
            (Geometry(82, 3, 10), Platform.ATARI_ST),
        ):
            with (
                self.subTest(geometry=geometry, platform=platform),
                self.assertRaises(DiskDefError),
            ):
                custom_definition(geometry, platform)


@unittest.skipUnless(GW, "the Greaseweazle host tools (gw) are not installed")
class RealToolTests(unittest.TestCase):
    """gw 1.23 accepts the definitions and keeps every cylinder."""

    def setUp(self) -> None:
        self._folder = tempfile.TemporaryDirectory()
        self.folder = Path(self._folder.name)

    def tearDown(self) -> None:
        self._folder.cleanup()

    def gw(self, *arguments: str) -> None:
        completed = subprocess.run(
            [GW, *arguments], cwd=self.folder, capture_output=True, text=True, timeout=300
        )
        self.assertEqual(completed.returncode, 0, completed.stderr[-2000:])
        self.assertNotIn("FATAL", completed.stderr)

    def custom(self, geometry: Geometry, platform: Platform) -> tuple[str, str]:
        definition = custom_definition(geometry, platform)
        path = write_definition(definition, self.folder / f"{definition.name}.cfg")
        return f"--diskdefs={path}", f"--format={definition.name}"

    def test_generated_tracks_encode_exactly_like_the_builtin_ones(self) -> None:
        cases = [
            (Geometry(80, 1, 9), Platform.ATARI_ST),
            (Geometry(80, 2, 9), Platform.ATARI_ST),
            (Geometry(80, 2, 10), Platform.ATARI_ST),
            (Geometry(80, 2, 11), Platform.ATARI_ST),
            (Geometry(80, 2, 18), Platform.ATARI_ST),
            (Geometry(80, 2, 11), Platform.AMIGA),
        ]
        for geometry, platform in cases:
            with self.subTest(geometry=geometry, platform=platform):
                suffix = ".adf" if platform is Platform.AMIGA else ".st"
                source = self.folder / f"in{suffix}"
                source.write_bytes(pattern(geometry.track_count * geometry.sectors * 512, 7))
                # Six cylinders cover the per-cylinder skews and keep the run short.
                tracks = "--tracks=c=0-5"
                self.gw("convert", *self.custom(geometry, platform), tracks, source.name, "c.hfe")
                builtin = f"--format={builtin_format(geometry, platform)}"
                self.gw("convert", builtin, tracks, source.name, "b.hfe")
                self.assertEqual(
                    (self.folder / "c.hfe").read_bytes(), (self.folder / "b.hfe").read_bytes()
                )

    def test_all_82_cylinders_of_an_st_survive(self) -> None:
        raw = st_image(82, 2, 10)
        (self.folder / "in.st").write_bytes(raw)
        self.gw("convert", *self.custom(Geometry(82, 2, 10), Platform.ATARI_ST), "in.st", "o.msa")
        written = parse_msa((self.folder / "o.msa").read_bytes())
        self.assertEqual(written.end_track, 81)
        self.assertEqual(written.sectors(), raw)
        # The built-in format drops cylinders 80 and 81, which is why the
        # generated definition exists.
        self.gw("convert", "--format=atarist.800", "in.st", "b.msa")
        self.assertEqual(parse_msa((self.folder / "b.msa").read_bytes()).end_track, 79)

    def test_all_84_cylinders_of_an_adf_survive(self) -> None:
        adf = adf_image(84)
        (self.folder / "in.adf").write_bytes(adf)
        self.gw("convert", *self.custom(Geometry(84, 2, 11), Platform.AMIGA), "in.adf", "o.adf")
        self.assertEqual((self.folder / "o.adf").read_bytes(), adf)

    def test_a_prepared_84_cylinder_flux_image_keeps_every_cylinder(self) -> None:
        (self.folder / "in.st").write_bytes(st_image(84, 2, 10))
        self.gw("convert", *self.custom(Geometry(84, 2, 10), Platform.ATARI_ST), "in.st", "in.hfe")
        data = (self.folder / "in.hfe").read_bytes()
        prepared = prepare(data, "in.hfe", self.folder / "work", label="Flux", platform=None)
        self.assertEqual(prepared.tracks, "c=0-83")
        # Byte 9 of an HFE header is its cylinder count.
        self.gw("convert", f"--tracks={prepared.tracks}", prepared.write_path, "out.hfe")
        self.assertEqual((self.folder / "out.hfe").read_bytes()[9], 84)
        # Without --tracks gw stops at cylinder 81.
        self.gw("convert", prepared.write_path, "short.hfe")
        self.assertEqual((self.folder / "short.hfe").read_bytes()[9], 82)

    def test_prepared_images_convert_with_their_own_arguments(self) -> None:
        cases = [
            (msa_archive(st_image(82, 2, 10), 82, 2, 10), "menu.msa", st_image(82, 2, 10)),
            (st_image(83, 2, 11), "menu.st", st_image(83, 2, 11)),
            (st_image(80, 2, 9), "menu.st", st_image(80, 2, 9)),
        ]
        for data, name, raw in cases:
            with self.subTest(name=name, size=len(raw)):
                prepared = prepare(data, name, self.folder / "work", label="Menu", platform=None)
                arguments = [f"--format={prepared.gw_format}"]
                if prepared.diskdefs_path:
                    arguments.insert(0, f"--diskdefs={prepared.diskdefs_path}")
                self.gw("convert", *arguments, prepared.write_path, "out.msa")
                written = parse_msa((self.folder / "out.msa").read_bytes())
                self.assertEqual(written.end_track + 1, prepared.geometry.cylinders)
                self.assertEqual(written.sectors(), raw)


if __name__ == "__main__":
    unittest.main()

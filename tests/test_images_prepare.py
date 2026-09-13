"""prepare() follows the Writing table in docs/DESIGN.md row by row."""

from __future__ import annotations

import contextlib
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from piratefinder.greaseweazle import caps
from piratefinder.images.prepare import PrepareError, prepare
from piratefinder.models import Geometry, Platform
from tests.test_images_synthetic import (
    adf_image,
    dms_archive,
    gzip_bytes,
    hfe_header,
    ipf_header,
    msa_archive,
    scp_header,
    st_image,
    stx_archive,
)


@contextlib.contextmanager
def caps_library(installed: bool, machine: str | None = None):
    """A data folder with or without the SPS Decoder Library PirateFinder installs.

    The library is a stand-in file, which is all prepare() looks for; a copy
    on this computer is kept out of the way.
    """
    with (
        tempfile.TemporaryDirectory() as data,
        mock.patch.dict(os.environ, {"XDG_DATA_HOME": data}),
        mock.patch.object(caps, "system_library", return_value=None),
        mock.patch.object(caps, "machine", return_value=machine or caps.machine()),
    ):
        if installed:
            caps.folder().mkdir(parents=True)
            (caps.folder() / caps.LIBRARY_NAME).write_bytes(b"\x7fELF stand-in")
        yield


class PrepareTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._folder = tempfile.TemporaryDirectory()
        self.workdir = Path(self._folder.name) / "work"

    def tearDown(self) -> None:
        self._folder.cleanup()

    def run_prepare(self, data: bytes, name: str, platform: Platform | None = None, label="Disk"):
        return prepare(data, name, self.workdir, label=label, platform=platform)

    def assert_written(self, prepared, data: bytes, suffix: str) -> None:
        path = Path(prepared.write_path)
        self.assertEqual(path.parent, self.workdir)
        self.assertEqual(path.suffix, suffix)
        self.assertEqual(path.read_bytes(), data)

    def diskdefs(self, prepared) -> str:
        return Path(prepared.diskdefs_path).read_text()


class AmigaTests(PrepareTestCase):
    def test_standard_adf_is_written_as_it_is(self) -> None:
        adf = adf_image()
        prepared = self.run_prepare(adf, "Compact 1.adf", Platform.AMIGA)
        self.assert_written(prepared, adf, ".adf")
        self.assertEqual(prepared.gw_format, "amiga.amigados")
        self.assertEqual(prepared.diskdefs_path, "")
        self.assertEqual(prepared.geometry, Geometry(80, 2, 11))
        self.assertEqual(prepared.platform, Platform.AMIGA)
        self.assertTrue(prepared.verifiable)
        self.assertEqual(prepared.notes, ())

    def test_high_density_adf(self) -> None:
        prepared = self.run_prepare(adf_image(sectors=22), "hd.adf")
        self.assertEqual(prepared.gw_format, "amiga.amigados_hd")

    def test_adf_with_extra_cylinders_gets_a_definition_with_all_of_them(self) -> None:
        for cylinders in (81, 82, 83, 84):
            with self.subTest(cylinders=cylinders):
                adf = adf_image(cylinders)
                prepared = self.run_prepare(adf, "x.adf")
                self.assert_written(prepared, adf, ".adf")
                self.assertEqual(prepared.gw_format, f"amiga_{cylinders}_2_11")
                self.assertIn(f"cyls = {cylinders}", self.diskdefs(prepared))
                self.assertIn("amiga.amigados", self.diskdefs(prepared))
                self.assertTrue(any("disk definition" in note for note in prepared.notes))

    def test_dms_is_decoded_to_adf(self) -> None:
        adf = adf_image()
        prepared = self.run_prepare(dms_archive(adf, mode=1), "pack.dms")
        self.assert_written(prepared, adf, ".adf")
        self.assertEqual(prepared.gw_format, "amiga.amigados")
        self.assertIn("Decoded the DMS archive to an ADF image.", prepared.notes)

    def test_adz_is_decompressed_to_adf(self) -> None:
        adf = adf_image(82)
        prepared = self.run_prepare(gzip_bytes(adf), "disk.adz")
        self.assert_written(prepared, adf, ".adf")
        self.assertEqual(prepared.gw_format, "amiga_82_2_11")
        self.assertEqual(prepared.notes[0], "Decompressed the ADZ file to an ADF image.")

    def test_extended_adf_is_refused(self) -> None:
        with self.assertRaisesRegex(PrepareError, "extended ADF") as caught:
            self.run_prepare(b"UAE-1ADF" + bytes(2048), "game.adf")
        self.assertTrue(caught.exception.message.endswith("."))

    def test_short_adf_is_padded(self) -> None:
        adf = adf_image()[: 79 * 11264]
        prepared = self.run_prepare(adf, "short.adf")
        self.assertEqual(len(Path(prepared.write_path).read_bytes()), 901_120)
        self.assertIn("79 cylinders", prepared.notes[0])

    def test_adf_of_the_wrong_size_is_refused(self) -> None:
        with self.assertRaisesRegex(PrepareError, "not a whole Amiga disk"):
            self.run_prepare(bytes(901_120 + 512), "odd.adf")


class AtariTests(PrepareTestCase):
    def test_standard_layouts_use_builtin_formats(self) -> None:
        cases = {
            (80, 1, 9): "atarist.360",
            (80, 1, 10): "atarist.400",
            (80, 1, 11): "atarist.440",
            (80, 2, 9): "atarist.720",
            (80, 2, 10): "atarist.800",
            (80, 2, 11): "atarist.880",
        }
        for layout, gw_format in cases.items():
            with self.subTest(layout=layout):
                raw = st_image(*layout)
                prepared = self.run_prepare(raw, "menu.st", Platform.ATARI_ST)
                self.assert_written(prepared, raw, ".st")
                self.assertEqual(prepared.gw_format, gw_format)
                self.assertEqual(prepared.diskdefs_path, "")
                self.assertEqual(prepared.geometry, Geometry(*layout))

    def test_82_cylinders_get_a_generated_definition(self) -> None:
        # gw writes only cylinders 0-79 of an 839,680-byte image with atarist.800.
        raw = st_image(82, 2, 10)
        prepared = self.run_prepare(raw, "menu.st")
        self.assertNotEqual(prepared.gw_format, "atarist.800")
        self.assertEqual(prepared.gw_format, "st_82_2_10")
        text = self.diskdefs(prepared)
        self.assertIn("cyls = 82", text)
        self.assertIn("gap3 = 30", text)
        self.assertEqual(prepared.geometry, Geometry(82, 2, 10))

    def test_other_non_standard_layouts(self) -> None:
        for layout in ((83, 2, 11), (81, 1, 9), (82, 2, 9)):
            with self.subTest(layout=layout):
                prepared = self.run_prepare(st_image(*layout), "menu.st")
                self.assertEqual(prepared.gw_format, "st_{}_{}_{}".format(*layout))

    def test_boot_sector_that_disagrees_with_the_size_is_not_trusted(self) -> None:
        raw = bytearray(st_image(82, 2, 10))
        raw[19:21] = (1600).to_bytes(2, "little")
        prepared = self.run_prepare(bytes(raw), "menu.st")
        self.assertEqual(prepared.geometry, Geometry(82, 2, 10))

    def test_layout_guessed_from_size_is_noted(self) -> None:
        prepared = self.run_prepare(st_image(84, 2, 10, bpb=False), "menu.st")
        self.assertEqual(prepared.gw_format, "st_84_2_10")
        self.assertTrue(any("taken from the file size" in note for note in prepared.notes))

    def test_msa_is_unpacked_then_handled_as_st(self) -> None:
        raw = st_image(82, 2, 10)
        prepared = self.run_prepare(msa_archive(raw, 82, 2, 10), "menu.msa")
        self.assert_written(prepared, raw, ".st")
        self.assertEqual(prepared.gw_format, "st_82_2_10")
        self.assertEqual(prepared.notes[0], "Unpacked the MSA archive to a plain sector image.")

    def test_msa_header_settles_an_ambiguous_size(self) -> None:
        raw = st_image(80, 1, 9, bpb=False)  # 368,640 bytes: 80 x 1 x 9 or 40 x 2 x 9
        prepared = self.run_prepare(msa_archive(raw, 40, 2, 9), "odd.msa")
        self.assertEqual(prepared.geometry, Geometry(40, 2, 9))
        self.assertEqual(prepared.gw_format, "st_40_2_9")

    def test_unprotected_stx_is_converted(self) -> None:
        raw = st_image()
        prepared = self.run_prepare(stx_archive(raw, 80, 2, 10), "game.stx")
        self.assert_written(prepared, raw, ".st")
        self.assertEqual(prepared.gw_format, "atarist.800")
        self.assertTrue(prepared.notes[0].startswith("Converted the Pasti STX image"))

    def test_protected_stx_is_refused(self) -> None:
        data = stx_archive(st_image(), 80, 2, 10, protect=True)
        with self.assertRaisesRegex(PrepareError, "copy protection"):
            self.run_prepare(data, "game.stx")

    def test_st_of_no_known_size_is_refused_with_the_size(self) -> None:
        with self.assertRaisesRegex(PrepareError, "1,000 bytes"):
            self.run_prepare(bytes(1000), "broken.st")

    def test_unsupported_sector_count_is_refused(self) -> None:
        raw = st_image(82, 2, 12)
        with self.assertRaisesRegex(PrepareError, "12 sectors per track"):
            self.run_prepare(raw, "twelve.st")


class FluxTests(PrepareTestCase):
    def test_scp_and_hfe_are_written_directly_without_verification(self) -> None:
        for data, name in ((scp_header(), "x.scp"), (hfe_header(), "x.hfe")):
            with self.subTest(name=name):
                prepared = self.run_prepare(data, name)
                self.assert_written(prepared, data, Path(name).suffix)
                self.assertFalse(prepared.verifiable)
                self.assertEqual(prepared.gw_format, "")
                self.assertTrue(any("cannot verify" in note for note in prepared.notes))
                self.assertEqual(prepared.platform, Platform.ATARI_ST)

    def test_flux_images_beyond_82_cylinders_name_every_track(self) -> None:
        # gw writes cylinders 0-81 of an image without a format unless told.
        cases = [
            (scp_header(end_track=167), "x.scp", "c=0-83"),
            (hfe_header(cylinders=84), "x.hfe", "c=0-83"),
            (hfe_header(cylinders=82), "x.hfe", ""),
            (scp_header(end_track=159), "x.scp", ""),
        ]
        for data, name, tracks in cases:
            with self.subTest(name=name, tracks=tracks):
                self.assertEqual(self.run_prepare(data, name).tracks, tracks)
        with caps_library(installed=True):
            self.assertEqual(self.run_prepare(ipf_header(cylinders=84), "x.ipf").tracks, "c=0-83")

    def test_ipf_without_the_caps_library_is_refused(self) -> None:
        with caps_library(installed=False), self.assertRaises(PrepareError) as caught:
            self.run_prepare(ipf_header(), "game.ipf")
        self.assertEqual(
            caught.exception.message,
            "Writing an IPF image needs the SPS Decoder Library (libcapsimage), which is not "
            "installed. Install it with IPF Support in Preferences, on the Greaseweazle page, "
            "or choose another dump of this disk.",
        )

    def test_ipf_on_a_processor_without_a_build_is_refused(self) -> None:
        with (
            caps_library(installed=False, machine="aarch64"),
            self.assertRaisesRegex(PrepareError, r"no build of it for .* \(aarch64\)"),
        ):
            self.run_prepare(ipf_header(), "game.ipf")

    def test_ipf_with_the_caps_library_is_written_directly(self) -> None:
        data = ipf_header(platform=1)
        with caps_library(installed=True):
            prepared = self.run_prepare(data, "game.ipf")
        self.assert_written(prepared, data, ".ipf")
        self.assertTrue(prepared.verifiable)
        self.assertEqual(prepared.platform, Platform.AMIGA)

    def test_ipf_with_a_library_on_the_system_is_written(self) -> None:
        with (
            caps_library(installed=False),
            mock.patch.object(caps, "system_library", return_value="libcapsimage.so.5"),
        ):
            prepared = self.run_prepare(ipf_header(), "game.ipf")
        self.assertTrue(prepared.write_path.endswith(".ipf"))

    def test_unknown_flux_platform_falls_back_with_a_note(self) -> None:
        prepared = self.run_prepare(scp_header(disk_type=0x80), "x.scp")
        self.assertTrue(any("could not tell" in note for note in prepared.notes))
        prepared = self.run_prepare(scp_header(disk_type=0x80), "x.scp", Platform.ATARI_ST)
        self.assertEqual(prepared.platform, Platform.ATARI_ST)


class GeneralTests(PrepareTestCase):
    def test_not_an_image(self) -> None:
        with self.assertRaisesRegex(PrepareError, "readme.txt is not a disk image"):
            self.run_prepare(b"hello", "folder/readme.txt")

    def test_label_becomes_a_safe_file_name(self) -> None:
        prepared = self.run_prepare(adf_image(), "x.adf", label="Menu::12 / A?")
        name = Path(prepared.write_path).name
        self.assertNotIn("::", name)
        self.assertNotIn("/", name)
        self.assertEqual(Path(prepared.write_path).parent, self.workdir)
        self.assertEqual(prepared.label, "Menu::12 / A?")

    def test_platform_mismatch_is_noted(self) -> None:
        prepared = self.run_prepare(st_image(), "x.st", Platform.AMIGA)
        self.assertEqual(prepared.platform, Platform.ATARI_ST)
        self.assertIn("listed for the Amiga", prepared.notes[0])

    def test_messages_and_notes_are_plain_ascii_sentences(self) -> None:
        prepared = self.run_prepare(
            gzip_bytes(msa_archive(st_image(82, 2, 10), 82, 2, 10)), "a.msa.gz"
        )
        for note in prepared.notes:
            self.assertTrue(note.isascii() and note.endswith("."), note)
        for data, name in ((b"UAE--ADF" + bytes(64), "a.adf"), (b"x", "a.txt")):
            with self.assertRaises(PrepareError) as caught:
                self.run_prepare(data, name)
            self.assertTrue(caught.exception.message.isascii())


if __name__ == "__main__":
    unittest.main()

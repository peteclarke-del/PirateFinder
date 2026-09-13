"""Image identification, decoding to raw sectors, hashing and listings."""

from __future__ import annotations

import hashlib
import random
import unittest
import zlib

from piratefinder.images import inspect
from piratefinder.images.inspect import Hashes, detect_format, inspect_bytes
from piratefinder.images.vendor import msa as vendor_msa
from piratefinder.models import Geometry, LocalFile, Platform
from tests.test_images_synthetic import (
    adf_image,
    dms_archive,
    gzip_bytes,
    hfe_header,
    ipf_header,
    msa_archive,
    pattern,
    scp_header,
    st_image,
    stx_archive,
)


class HashTests(unittest.TestCase):
    def test_hashes_are_lowercase_hex_of_the_bytes(self) -> None:
        data = pattern(4096)
        hashes = Hashes.of(data)
        self.assertEqual(hashes.crc32, f"{zlib.crc32(data):08x}")
        self.assertRegex(hashes.crc32, r"^[0-9a-f]{8}$")
        self.assertEqual(hashes.md5, hashlib.md5(data).hexdigest())
        self.assertEqual(hashes.sha1, hashlib.sha1(data).hexdigest())
        self.assertEqual(hashes.sha512, hashlib.sha512(data).hexdigest())

    def test_crc32_keeps_leading_zeros(self) -> None:
        data = next(
            bytes([a, b])
            for a in range(256)
            for b in range(256)
            if zlib.crc32(bytes([a, b])) < 0x10000000
        )
        self.assertEqual(len(Hashes.of(data).crc32), 8)
        self.assertTrue(Hashes.of(data).crc32.startswith("0"))


class DetectionTests(unittest.TestCase):
    def test_magic_numbers(self) -> None:
        raw = st_image()
        cases = {
            "msa": msa_archive(raw, 80, 2, 10),
            "dms": dms_archive(adf_image()),
            "gz": gzip_bytes(raw),
            "stx": stx_archive(raw, 80, 2, 10),
            "ipf": ipf_header(),
            "scp": scp_header(),
            "hfe": hfe_header(),
            "adf-ext": b"UAE-1ADF" + bytes(1024),
        }
        for expected, data in cases.items():
            with self.subTest(expected):
                self.assertEqual(detect_format(data, "image.bin"), expected)

    def test_adz_is_named_by_suffix(self) -> None:
        self.assertEqual(detect_format(gzip_bytes(adf_image()), "disk.adz"), "adz")

    def test_amiga_sizes_including_extra_cylinders_and_high_density(self) -> None:
        for cylinders in (80, 81, 82, 83, 84):
            with self.subTest(cylinders=cylinders):
                data = adf_image(cylinders)
                self.assertEqual(detect_format(data, "disk.adf"), "adf")
                self.assertEqual(inspect.amiga_geometry(len(data)), Geometry(cylinders, 2, 11))
        self.assertEqual(inspect.amiga_geometry(1_802_240), Geometry(80, 2, 22))

    def test_the_880k_size_is_told_apart_by_content(self) -> None:
        # 901,120 bytes is both an 80-cylinder ADF and an 80 x 2 x 11 ST image.
        self.assertEqual(detect_format(adf_image(), "disk"), "adf")
        self.assertEqual(detect_format(st_image(80, 2, 11), "disk"), "st")
        self.assertEqual(detect_format(st_image(80, 2, 11), "disk.adf"), "adf")
        self.assertEqual(detect_format(adf_image(), "disk.st"), "st")

    def test_st_sizes(self) -> None:
        for layout in ((80, 1, 9), (80, 2, 9), (80, 2, 10), (82, 2, 10), (83, 2, 11)):
            with self.subTest(layout=layout):
                self.assertEqual(detect_format(st_image(*layout), "disk"), "st")

    def test_not_an_image(self) -> None:
        self.assertEqual(detect_format(b"hello world", "readme.txt"), "")
        self.assertEqual(detect_format(b"", "empty.st"), "")
        self.assertEqual(detect_format(bytes(1000), "odd.st"), "")


class GeometryTests(unittest.TestCase):
    def test_boot_sector_is_believed_when_it_matches_the_size(self) -> None:
        geometry, source = inspect.st_geometry(st_image(82, 2, 10))
        self.assertEqual((geometry, source), (Geometry(82, 2, 10), "boot"))

    def test_boot_sector_that_disagrees_with_the_size_is_ignored(self) -> None:
        raw = bytearray(st_image(82, 2, 10))
        raw[19:21] = (80 * 2 * 10).to_bytes(2, "little")  # claims 80 cylinders
        geometry, source = inspect.st_geometry(bytes(raw))
        self.assertEqual(geometry, Geometry(82, 2, 10))
        self.assertEqual(source, "size")

    def test_sizes_the_table_lacks_are_guessed_as_hatari_does(self) -> None:
        geometry, source = inspect.st_geometry(st_image(84, 2, 10, bpb=False))
        self.assertEqual((geometry, source), (Geometry(84, 2, 10), "guess"))

    def test_ambiguous_size_prefers_eighty_cylinders(self) -> None:
        # 368,640 bytes is 80 x 1 x 9 or 40 x 2 x 9.
        geometry, source = inspect.st_geometry(st_image(80, 1, 9, bpb=False))
        self.assertEqual((geometry, source), (Geometry(80, 1, 9), "guess"))

    def test_a_container_hint_wins(self) -> None:
        raw = st_image(80, 1, 9, bpb=False)
        geometry, source = inspect.st_geometry(raw, Geometry(40, 2, 9))
        self.assertEqual((geometry, source), (Geometry(40, 2, 9), "hint"))


class InspectTests(unittest.TestCase):
    def test_st_is_its_own_raw_image_with_label_and_listing(self) -> None:
        raw = st_image(
            files={"README.TXT": b"x" * 3000, "GAMES/RUN.PRG": b"y" * 100}, label="MENU 7"
        )
        found = inspect_bytes(raw, "Menu 7.st")
        self.assertEqual(found.format, "st")
        self.assertEqual(found.platform, Platform.ATARI_ST)
        self.assertIs(found.raw, raw)
        self.assertEqual(found.raw_hashes, found.hashes)
        self.assertEqual(found.volume_label, "MENU 7")
        self.assertEqual(found.listing, ("README.TXT", "GAMES/", "GAMES/RUN.PRG"))

    def test_st_label_comes_from_the_root_directory_not_the_boot_code(self) -> None:
        found = inspect_bytes(st_image(files={"A.PRG": b"a"}), "x.st")
        self.assertEqual(found.volume_label, "")

    def test_msa_hashes_as_the_st_it_holds(self) -> None:
        raw = st_image(82, 2, 10, files={"A.PRG": bytes(9000)}, label="DISK")
        packed = msa_archive(raw, 82, 2, 10)
        found = inspect_bytes(packed, "disk.msa")
        self.assertEqual(found.format, "msa")
        self.assertEqual(found.raw, raw)
        self.assertEqual(found.geometry, Geometry(82, 2, 10))
        self.assertEqual(found.raw_hashes, Hashes.of(raw))
        self.assertEqual(found.hashes, Hashes.of(packed))
        self.assertNotEqual(found.hashes, found.raw_hashes)
        self.assertEqual(found.volume_label, "DISK")

    def test_damaged_msa_reports_a_problem_without_raising(self) -> None:
        packed = msa_archive(st_image(), 80, 2, 10)
        found = inspect_bytes(packed[: len(packed) // 2], "cut.msa")
        self.assertEqual(found.format, "msa")
        self.assertIsNone(found.raw)
        self.assertTrue(found.problem)
        self.assertEqual(found.raw_hashes, found.hashes)

    def test_dms_decodes_to_the_adf(self) -> None:
        adf = adf_image(name="Pack 3", files={"intro": b"z" * 700})
        # NOCOMP, SIMPLE and QUICK tracks in turn, sharing one decoder.
        found = inspect_bytes(dms_archive(adf, mode=(0, 1, 2)), "pack.dms")
        self.assertEqual(found.format, "dms")
        self.assertEqual(found.platform, Platform.AMIGA)
        self.assertEqual(found.raw, adf, found.problem)
        self.assertEqual(found.raw_hashes, Hashes.of(adf))
        self.assertEqual(found.volume_label, "Pack 3")

    def test_dms_checks_unpacked_tracks_with_the_additive_sum(self) -> None:
        # xDMS stores a plain sum of the unpacked bytes, not a CRC; an archive
        # built that way must decode.
        adf = adf_image()
        self.assertEqual(inspect_bytes(dms_archive(adf), "x.dms").raw, adf)

    def test_dms_file_id_diz_track_is_not_part_of_the_disk(self) -> None:
        adf = adf_image()
        archive = dms_archive(adf, extra=((80, b"Greetings text\r\n" * 10),))
        found = inspect_bytes(archive, "x.dms")
        self.assertEqual(found.raw, adf)
        self.assertEqual(found.geometry, Geometry(80, 2, 11))

    def test_dms_with_missing_last_tracks_is_padded_to_eighty(self) -> None:
        adf = adf_image()
        short = adf[: 60 * 11264]
        found = inspect_bytes(dms_archive(short), "x.dms")
        self.assertEqual(len(found.raw or b""), len(adf))
        self.assertEqual(found.raw[: len(short)], short)

    def test_high_density_dms(self) -> None:
        adf = adf_image(sectors=22)
        found = inspect_bytes(dms_archive(adf), "hd.dms")
        self.assertEqual(found.raw, adf)
        self.assertEqual(found.geometry, Geometry(80, 2, 22))

    def test_adz_decodes_to_the_adf(self) -> None:
        adf = adf_image(name="Work")
        found = inspect_bytes(gzip_bytes(adf), "work.adz")
        self.assertEqual(found.format, "adz")
        self.assertEqual(found.raw, adf)
        self.assertEqual(found.raw_hashes, Hashes.of(adf))
        self.assertEqual(found.volume_label, "Work")

    def test_gzip_of_an_msa_hashes_as_the_st(self) -> None:
        raw = st_image()
        found = inspect_bytes(gzip_bytes(msa_archive(raw, 80, 2, 10)), "disk.msa.gz")
        self.assertEqual(found.format, "gz")
        self.assertEqual(found.raw, raw)

    def test_stx_without_protection_decodes(self) -> None:
        raw = st_image()
        found = inspect_bytes(stx_archive(raw, 80, 2, 10), "disk.stx")
        self.assertEqual(found.raw, raw)
        self.assertEqual(found.geometry, Geometry(80, 2, 10))

    def test_protected_stx_has_no_raw_image(self) -> None:
        found = inspect_bytes(stx_archive(st_image(), 80, 2, 10, protect=True), "disk.stx")
        self.assertIsNone(found.raw)
        self.assertIn("protection", found.problem)

    def test_flux_headers_give_platform_and_tracks(self) -> None:
        cases = [
            (ipf_header(platform=1), "x.ipf", Platform.AMIGA, 84),
            (ipf_header(platform=2), "x.ipf", Platform.ATARI_ST, 84),
            (scp_header(disk_type=0x04, end_track=165), "x.scp", Platform.AMIGA, 83),
            (scp_header(disk_type=0x80), "x.scp", None, 82),
            (hfe_header(interface=2), "x.hfe", Platform.ATARI_ST, 82),
            (hfe_header(interface=7, encoding=1), "x.hfe", Platform.AMIGA, 82),
        ]
        for data, name, platform, cylinders in cases:
            with self.subTest(name=name, platform=platform):
                found = inspect_bytes(data, name)
                self.assertIsNone(found.raw)
                self.assertEqual(found.platform, platform)
                self.assertEqual(found.geometry.cylinders, cylinders)

    def test_garbage_never_raises(self) -> None:
        generator = random.Random(9)
        samples = [b"", b"\x0e\x0f" + bytes(20), b"DMS!" + bytes(60), b"RSY\0" + bytes(40)]
        samples += [b"\x1f\x8b" + bytes(30), b"CAPS", b"SCP", b"HXCPICFE"]
        samples += [generator.randbytes(size) for size in (1, 512, 737_280, 901_120)]
        for index, data in enumerate(samples):
            with self.subTest(index=index):
                found = inspect_bytes(data, f"sample{index}.st")
                self.assertEqual(found.size, len(data))

    def test_damaged_filesystem_gives_an_empty_listing(self) -> None:
        raw = bytearray(st_image(files={"A.PRG": b"a"}))
        raw[11 * 512 : 18 * 512] = b"\xff" * (7 * 512)  # root directory overwritten
        found = inspect_bytes(bytes(raw), "x.st")
        self.assertEqual(found.format, "st")
        self.assertIsNotNone(found.raw)


class PlatformTests(unittest.TestCase):
    """The one table of which platform a format belongs to, used by every layer."""

    def test_every_recognised_format_maps_to_its_platform(self) -> None:
        amiga, st = Platform.AMIGA, Platform.ATARI_ST
        expected = {
            **dict.fromkeys(("adf", "adf-ext", "adz", "dms"), amiga),
            **dict.fromkeys(("st", "msa", "stx"), st),
            # Flux images and plain gzip hold either platform; only the contents tell.
            **dict.fromkeys(("ipf", "scp", "hfe", "gz"), None),
        }
        suffixes = {suffix.lstrip(".") for suffix in inspect.IMAGE_SUFFIXES}
        self.assertLessEqual(suffixes, set(expected), "a new image suffix needs a platform here")
        for image_format, platform in expected.items():
            with self.subTest(format=image_format):
                self.assertIs(inspect.format_platform(image_format), platform)
                self.assertIs(inspect.format_platform(image_format.upper()), platform)
                local = LocalFile(path=f"/nas/Game.{image_format}", format=image_format)
                self.assertIs(inspect.local_platform(local), platform)

    def test_a_sector_image_is_an_adf_on_the_amiga_and_an_st_otherwise(self) -> None:
        self.assertEqual(inspect.sector_suffix(Platform.AMIGA), ".adf")
        self.assertEqual(inspect.sector_suffix(Platform.ATARI_ST), ".st")
        self.assertEqual(inspect.sector_suffix(None), ".st")

    def test_a_file_without_a_format_goes_by_its_suffix(self) -> None:
        for suffix in inspect.IMAGE_SUFFIXES:
            with self.subTest(suffix=suffix):
                bare = LocalFile(path="/nas/menus.zip", member=f"Menus/Game{suffix.upper()}")
                self.assertIs(inspect.local_platform(bare), inspect.format_platform(suffix[1:]))

    def test_decoded_images_agree_with_the_table(self) -> None:
        cases = [
            (adf_image(), "x.adf"),
            (dms_archive(adf_image()), "x.dms"),
            (gzip_bytes(adf_image()), "x.adz"),
            (st_image(), "x.st"),
            (msa_archive(st_image(), 80, 2, 10), "x.msa"),
            (stx_archive(st_image(), 80, 2, 10), "x.stx"),
        ]
        for data, name in cases:
            with self.subTest(name=name):
                found = inspect_bytes(data, name)
                self.assertIsNotNone(found.raw, found.problem)
                self.assertIs(found.platform, inspect.format_platform(found.format))


class DMSChecksumTests(unittest.TestCase):
    """The vendored crc16 was made table driven; its output must not change."""

    def test_matches_the_bitwise_crc(self) -> None:
        from piratefinder.images.vendor.dms import crc16

        def bitwise(data: bytes) -> int:
            crc = 0
            for value in data:
                crc ^= value
                for _ in range(8):
                    crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
            return crc

        for size in (0, 1, 2, 20, 4096):
            data = pattern(size, size)
            with self.subTest(size=size):
                self.assertEqual(crc16(data), bitwise(data))


class MSAUnpackTests(unittest.TestCase):
    """The vendored unpack_track was rewritten for speed; its output must not change."""

    @staticmethod
    def reference(packed: bytes, expected: int) -> bytes:
        out = bytearray()
        index = 0
        while index < len(packed):
            value = packed[index]
            index += 1
            if value != 0xE5:
                out.append(value)
                continue
            if index + 3 > len(packed):
                raise vendor_msa.MSAError("cut off")
            count = int.from_bytes(packed[index + 1 : index + 3], "big")
            out += bytes((packed[index],)) * count
            index += 3
            if len(out) > expected:
                raise vendor_msa.MSAError("too long")
        if len(out) != expected:
            raise vendor_msa.MSAError("wrong length")
        return bytes(out)

    def test_matches_the_byte_by_byte_reference(self) -> None:
        generator = random.Random(3)
        for trial in range(200):
            track = bytearray()
            while len(track) < 5120:
                choice = generator.random()
                if choice < 0.2:
                    track += bytes((generator.choice((0, 0xE5, 0x4E)),)) * generator.randint(1, 900)
                else:
                    track += generator.randbytes(generator.randint(1, 300))
            track = bytes(track[:5120])
            packed = vendor_msa.pack_track(track)
            with self.subTest(trial=trial):
                self.assertEqual(
                    vendor_msa.unpack_track(packed, 5120), self.reference(packed, 5120)
                )
                self.assertEqual(vendor_msa.unpack_track(packed, 5120), track)

    def test_errors_match_the_reference(self) -> None:
        bad = [b"\xe5\x00", b"abc", b"\xe5\x00\x20\x00" + b"x", b"x" * 5121]
        for packed in bad:
            with self.subTest(packed=packed[:8]):
                with self.assertRaises(vendor_msa.MSAError):
                    self.reference(packed, 5120)
                with self.assertRaises(vendor_msa.MSAError):
                    vendor_msa.unpack_track(packed, 5120)


if __name__ == "__main__":
    unittest.main()

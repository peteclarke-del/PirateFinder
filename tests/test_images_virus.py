"""Boot block virus detection and removal on synthetic Amiga and ST images.

No real virus is used anywhere. The "viruses" are boot blocks built from a
seed: the Amiga ones are named by a synthetic brainfile in
``tests/fixtures/virus`` or by signature and marker files a test writes for
its own synthetic block, the ST ones by a signature file each test writes for
its own synthetic virus body. Marker tests put the shipped marker values
(numbers only) into otherwise synthetic sectors. The helpers here are shared
by the library, finder and job tests.
"""

from __future__ import annotations

import hashlib
import io
import os
import random
import shutil
import struct
import tempfile
import unittest
import zipfile
import zlib
from pathlib import Path
from unittest import mock

from piratefinder.images import virus
from piratefinder.models import Platform, VirusStatus
from piratefinder.online.http import DownloadCancelled
from tests.test_library_helpers import make_st_image

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "virus"
SECTOR = 512
ADF_SIZE = 80 * 2 * 11 * SECTOR

# SHA-1 of the boot blocks the Kickstart 1.3 and 2.0 install commands write
# for DOS types 0 to 5, taken from blocks written by those commands.
INSTALL_13 = (
    "bbdc75ccbb8bc0d469e153e1075d8dc743f7726c",
    "1e0b308522da329a6adc62a0eb7cbb1fce4ef0a8",
    "62d839329c519066c92d314c2d897537cfed7e06",
    "4fe6225dc5c953c274d0597c69492cb3649c6225",
    "b46e86262efb4afed9d28692b9a02f8befb4a7a3",
    "425b7b941dc9fe87acbc76c7324236d5742fdc4a",
)
INSTALL_20 = (
    "55dd0a3fd62d490158a907d08a398ed2f3c7fed1",
    "1eccd7a184cf8da7571c15a9327bf3daf15d818b",
    "6ca22cd7f88a7f738ee6dbdefa8e6432923bc992",
    "3e227406d63f0e2ee51fe038f812dbee98fbe9c5",
    "bf2afc684c5ecfc657793f8288c8d208ea23e0ae",
    "0a4d8bd1263fccf92bab2bf94b51508df7ab5eaa",
)


# Builders ---------------------------------------------------------------------


def sequential_checksum(block: bytes) -> int:
    """The boot block checksum computed longword by longword, as the ROM does."""
    total = 0
    for offset in range(0, 1024, 4):
        if offset == 4:
            continue
        total += struct.unpack_from(">L", block, offset)[0]
        if total > 0xFFFFFFFF:
            total = (total & 0xFFFFFFFF) + 1
    return ~total & 0xFFFFFFFF


def checksummed(block: bytes | bytearray) -> bytes:
    fixed = bytearray(block)
    struct.pack_into(">L", fixed, 4, sequential_checksum(fixed))
    return bytes(fixed)


def synthetic_boot(seed: str, flags: int = 0, marker: bytes = b"") -> bytes:
    """A bootable Amiga boot block holding seeded bytes after ``marker``."""
    rng = random.Random(seed)
    block = bytearray(1024)
    block[0:4] = b"DOS" + bytes([flags])
    struct.pack_into(">L", block, 8, 880)
    body = marker + rng.randbytes(300)
    block[12 : 12 + len(body)] = body
    return checksummed(block)


def crc_virus(flags: int = 0) -> bytes:
    """Named "Synthetic CRC Virus" by the fixture brainfile's CRC entry."""
    return synthetic_boot("crc virus", flags, b"SYNTHETIC CRC VIRUS")


def raw_crc_loader() -> bytes:
    return synthetic_boot("raw crc loader", 1, b"SYNTHETIC RAW CRC LOADER")


def recogniser_antivirus() -> bytes:
    return synthetic_boot("recogniser antivirus", 0, b"SYNTHETIC ANTIVIRUS")


def byte_string_virus() -> bytes:
    return synthetic_boot("byte string", 0, b"xx SYNTHETIC BYTE STRING VIRUS xx")


def amiga_disk(boot: bytes, *, root: bool = True, seed: int = 7) -> bytes:
    """A double-density ADF with ``boot`` and, when asked, a valid root block at 880."""
    image = bytearray(random.Random(seed).randbytes(ADF_SIZE))
    image[0:1024] = boot
    if root:
        block = bytearray(SECTOR)
        struct.pack_into(">L", block, 0, 2)  # T_HEADER
        struct.pack_into(">L", block, 12, 72)  # hash table size
        struct.pack_into(">L", block, 508, 1)  # ST_ROOT
        block[432] = 4
        block[433:437] = b"TEST"
        total = sum(struct.unpack(">128L", block)) & 0xFFFFFFFF
        struct.pack_into(">L", block, 20, (-total) & 0xFFFFFFFF)
        image[880 * SECTOR : 881 * SECTOR] = block
    return bytes(image)


def st_executable(sector: bytearray) -> bytes:
    """``sector`` with its last word set so the word sum is 0x1234."""
    struct.pack_into(">H", sector, 510, 0)
    total = sum(struct.unpack(">256H", sector)) & 0xFFFF
    struct.pack_into(">H", sector, 510, (0x1234 - total) & 0xFFFF)
    return bytes(sector)


def st_body(seed: str = "st virus", length: int = 430) -> bytes:
    return b"SYNTHETIC ST VIRUS" + random.Random(seed).randbytes(length - 18)


def st_infected(raw: bytes, body: bytes, offset: int = 0x1E) -> bytes:
    """An ST image whose boot sector carries ``body`` and is executable."""
    sector = bytearray(raw[:SECTOR])
    sector[offset : offset + len(body)] = body
    return st_executable(sector) + raw[SECTOR:]


def st_signature_toml(name: str, body: bytes, offset: int = 0x1E) -> str:
    digest = hashlib.sha1(body).hexdigest()
    return (
        f'[[signature]]\nname = "{name}"\noffset = {offset}\nlength = {len(body)}\n'
        f'sha1 = "{digest}"\nsource = "test"\nnote = "A synthetic test virus."\n'
    )


def window_toml(name: str, block: bytes, offset: int, length: int) -> str:
    """A signature entry for ``length`` bytes of ``block`` at ``offset``."""
    return st_signature_toml(name, block[offset : offset + length], offset)


def rule_toml(name: str, checks: list[tuple[int, bytes]], *, code: bool = False) -> str:
    items = ", ".join(f'{{ offset = {offset}, bytes = "{data.hex()}" }}' for offset, data in checks)
    return (
        f'[[rule]]\nname = "{name}"\nchecks = [{items}]\ncode = {str(code).lower()}\n'
        'source = "test"\n'
    )


def marker_toml(name: str, offset: int, size: int, value: int, tier: str = "reliable") -> str:
    return (
        f'[[marker]]\nname = "{name}"\noffset = {offset}\nsize = {size}\nvalue = {value}\n'
        f'tier = "{tier}"\nsource = "test"\n'
    )


# 68000 code patterns from indicators.toml, placed at even offsets.
ST_FLOPWR = bytes.fromhex("3F3C00094E4E")  # XBIOS Flopwr
AMIGA_CMD_WRITE = bytes.fromhex("337C0003001C")  # trackdisk CMD_WRITE into io_Command


class VirusEnvironment:
    """Keeps a test away from the user's data: its own data folder and signatures."""

    def use_environment(self: unittest.TestCase, *, brainfile: bool = False) -> Path:
        folder = Path(tempfile.mkdtemp(prefix="pf-virus-"))
        self.addCleanup(shutil.rmtree, folder, True)
        patcher = mock.patch.dict(os.environ, {"XDG_DATA_HOME": str(folder / "data")})
        patcher.start()
        self.addCleanup(patcher.stop)
        virus.forget_brainfile()
        self.addCleanup(virus.forget_brainfile)
        if brainfile:
            install_fixture_brainfile()
        return folder

    def use_data(self: unittest.TestCase, attribute: str, text: str) -> None:
        """Point the data file ``virus.<attribute>`` at ``text`` for this test."""
        loader = {
            "ST_SIGNATURES": virus.st_signatures,
            "ST_MARKERS": virus.st_markers,
            "AMIGA_SIGNATURES": virus.amiga_signatures,
            "AMIGA_MARKERS": virus.amiga_rules,
        }[attribute]
        folder = Path(tempfile.mkdtemp(prefix="pf-signatures-"))
        self.addCleanup(shutil.rmtree, folder, True)
        path = folder / getattr(virus, attribute).name
        path.write_text(text)
        patcher = mock.patch.object(virus, attribute, path)
        patcher.start()
        self.addCleanup(patcher.stop)
        loader.cache_clear()
        self.addCleanup(loader.cache_clear)

    def use_st_signatures(self: unittest.TestCase, text: str) -> None:
        self.use_data("ST_SIGNATURES", text)

    def shipped(self: unittest.TestCase, attribute: str, loader):
        """What ``loader`` reads from the shipped data file, whatever a test patched."""
        with mock.patch.object(virus, attribute, virus.DATA_DIR / getattr(virus, attribute).name):
            loader.cache_clear()
            try:
                return loader()
            finally:
                loader.cache_clear()


def install_fixture_brainfile() -> Path:
    """Copy the synthetic brainfile to where an installed one lives."""
    target = virus.brainfile_folder()
    target.mkdir(parents=True, exist_ok=True)
    for name in virus.BRAINFILE_FILES:
        shutil.copyfile(FIXTURES / name, target / name)
    virus.forget_brainfile()
    return target


# Amiga ------------------------------------------------------------------------


class AmigaFormatTests(VirusEnvironment, unittest.TestCase):
    def setUp(self) -> None:
        self.use_environment()

    def test_checksum_is_the_carry_wraparound_sum(self) -> None:
        rng = random.Random(3)
        for _ in range(200):
            block = rng.randbytes(1024)
            self.assertEqual(virus.amiga_checksum(block), sequential_checksum(block))
        full = b"\xff" * 1024
        self.assertEqual(virus.amiga_checksum(full), sequential_checksum(full))

    def test_standard_blocks_are_those_the_install_commands_write(self) -> None:
        for flags in range(6):
            with self.subTest(flags=flags):
                block13 = virus.standard_boot_block(flags, "1.3")
                block20 = virus.standard_boot_block(flags, "2.0")
                self.assertEqual(hashlib.sha1(block13).hexdigest(), INSTALL_13[flags])
                self.assertEqual(hashlib.sha1(block20).hexdigest(), INSTALL_20[flags])
                for block in (block13, block20):
                    report = virus.detect(amiga_disk(block), Platform.AMIGA)
                    self.assertEqual(report.status, VirusStatus.CLEAN, report.explanation)
        self.assertEqual(virus.standard_boot_block(0), virus.standard_boot_block(0, "1.3"))
        self.assertEqual(virus.standard_boot_block(1), virus.standard_boot_block(1, "2.0"))

    def test_leftover_bytes_after_standard_code_are_clean(self) -> None:
        block = bytearray(virus.standard_boot_block(0))
        block[200:260] = random.Random(1).randbytes(60)
        report = virus.detect(checksummed(block), Platform.AMIGA)
        self.assertEqual(report.status, VirusStatus.CLEAN)
        self.assertIn("Leftover bytes", report.explanation)

    def test_blocks_that_cannot_run_are_clean(self) -> None:
        not_dos = bytearray(crc_virus())
        not_dos[0:3] = b"KIC"
        uninstalled = bytearray(1024)
        uninstalled[0:4] = b"DOS\0"
        bad_checksum = bytearray(crc_virus())
        bad_checksum[5] ^= 0xFF
        for block, words in (
            (not_dos, "no AmigaDOS boot block"),
            (uninstalled, "formatted but not installed"),
            (bad_checksum, "checksum is wrong"),
            (b"DOS\0", "too short"),
        ):
            with self.subTest(words=words):
                report = virus.detect(bytes(block), Platform.AMIGA)
                self.assertEqual(report.status, VirusStatus.CLEAN)
                self.assertIn(words, report.explanation)

    def test_unknown_code_is_information_only(self) -> None:
        code = bytearray(1024)
        code[0:4] = b"DOS\0"
        struct.pack_into(">L", code, 8, 880)
        # move.l #$00FC0000,$2E(a6), a trackdisk CMD_WRITE and SetFunction.
        code[12:30] = bytes.fromhex("2D7C00FC0000002E337C0003001C4EAEFE5C")
        report = virus.detect(amiga_disk(checksummed(code)), Platform.AMIGA)
        self.assertEqual(report.status, VirusStatus.UNKNOWN_BOOT)
        self.assertFalse(report.removable)
        self.assertIn("CoolCapture", report.explanation)
        self.assertIn("writes to the disk", report.explanation)
        self.assertIn("patches system functions", report.explanation)
        self.assertIn("brainfile in Preferences", report.explanation)
        self.assertFalse(report.infected)

    def test_an_unknown_virus_is_not_removed(self) -> None:
        with self.assertRaises(virus.VirusError) as caught:
            virus.clean(amiga_disk(crc_virus()), Platform.AMIGA)
        self.assertIn("cannot identify", caught.exception.message)

    def test_clean_disks_are_not_changed(self) -> None:
        with self.assertRaises(virus.VirusError) as caught:
            virus.clean(amiga_disk(virus.standard_boot_block(0)), Platform.AMIGA)
        self.assertIn("nothing to remove", caught.exception.message)

    def test_platform_is_guessed_when_not_given(self) -> None:
        report = virus.detect(amiga_disk(virus.standard_boot_block(0)), None)
        self.assertIn("AmigaDOS", report.explanation)


class BrainfileTests(VirusEnvironment, unittest.TestCase):
    def setUp(self) -> None:
        self.use_environment(brainfile=True)

    def test_fixture_crc_is_the_normalised_block_crc(self) -> None:
        # The fixture was written with these values; a change to the
        # normalisation would stop real brainfile CRCs matching.
        self.assertEqual(virus.abr_crc(crc_virus()), 0xA5C88719)
        self.assertEqual(virus.abr_crc(crc_virus(flags=3)), 0xA5C88719)
        self.assertEqual(zlib.crc32(raw_crc_loader()), 0x6D408BDC)

    def test_status_counts_every_entry(self) -> None:
        self.assertEqual(virus.brainfile_status(), (True, "", 5))

    def test_crc_entry_names_a_virus_whatever_the_dos_type(self) -> None:
        for flags in (0, 1, 3):
            with self.subTest(flags=flags):
                report = virus.detect(amiga_disk(crc_virus(flags)), Platform.AMIGA)
                self.assertEqual(report.status, VirusStatus.VIRUS)
                self.assertEqual(report.name, "Synthetic CRC")
                self.assertEqual(report.kind, "boot")
                self.assertEqual(report.source, "Amiga Bootblock Reader")
                self.assertTrue(report.removable)
                self.assertIn("custom intro or loader", report.explanation)
                self.assertTrue(report.infected)

    def test_raw_crc_byte_strings_and_recognisers(self) -> None:
        loader = virus.detect(amiga_disk(raw_crc_loader()), Platform.AMIGA)
        self.assertEqual(
            (loader.status, loader.name), (VirusStatus.KNOWN_BOOT, "Synthetic Raw CRC Loader")
        )
        self.assertIn("(Boot loader)", loader.explanation)
        strings = virus.detect(amiga_disk(byte_string_virus()), Platform.AMIGA)
        self.assertEqual(
            (strings.status, strings.name), (VirusStatus.VIRUS, "Synthetic Byte String")
        )
        antivirus = virus.detect(amiga_disk(recogniser_antivirus()), Platform.AMIGA)
        self.assertEqual(antivirus.status, VirusStatus.ANTIVIRUS)
        self.assertEqual(antivirus.name, "Synthetic Recogniser Antivirus")
        self.assertFalse(antivirus.infected)

    def test_a_recogniser_needs_all_seven_bytes(self) -> None:
        block = bytearray(recogniser_antivirus())
        block[300] ^= 0x01  # the last of the seven recogniser bytes
        report = virus.detect(amiga_disk(checksummed(block)), Platform.AMIGA)
        self.assertEqual(report.status, VirusStatus.UNKNOWN_BOOT)

    def test_a_virus_without_a_file_system_is_not_removable(self) -> None:
        image = amiga_disk(crc_virus(), root=False)
        report = virus.detect(image, Platform.AMIGA)
        self.assertEqual(report.status, VirusStatus.VIRUS)
        self.assertFalse(report.removable)
        self.assertIn("no intact AmigaDOS file system", report.explanation)
        with self.assertRaises(virus.VirusError):
            virus.clean(image, Platform.AMIGA)

    def test_a_damaged_root_block_is_not_intact(self) -> None:
        image = bytearray(amiga_disk(crc_virus()))
        image[880 * SECTOR + 100] ^= 0x40
        self.assertFalse(virus.amiga_root_intact(bytes(image)))
        self.assertFalse(virus.detect(bytes(image), Platform.AMIGA).removable)

    def test_cleaning_writes_the_standard_block_for_the_dos_type(self) -> None:
        for flags, release in ((0, "1.3"), (1, "2.0"), (3, "2.0")):
            with self.subTest(flags=flags):
                image = amiga_disk(crc_virus(flags))
                cleaned = virus.clean(image, Platform.AMIGA)
                self.assertEqual(cleaned[:1024], virus.standard_boot_block(flags, release))
                self.assertEqual(cleaned[3], flags)
                self.assertEqual(struct.unpack_from(">L", cleaned, 8)[0], 880)
                self.assertEqual(cleaned[1024:], image[1024:])
                self.assertEqual(len(cleaned), len(image))
                after = virus.detect(cleaned, Platform.AMIGA)
                self.assertEqual(after.status, VirusStatus.CLEAN)

    def test_an_inactive_copy_is_named_but_clean(self) -> None:
        block = bytearray(crc_virus())
        block[4] ^= 0x01
        report = virus.detect(amiga_disk(bytes(block)), Platform.AMIGA)
        self.assertEqual(report.status, VirusStatus.CLEAN)
        self.assertIn("inactive copy of the Synthetic CRC virus", report.explanation)

    def test_brainfile_changes_are_picked_up(self) -> None:
        first = virus.brainfile()
        self.assertIs(virus.brainfile(), first, "loaded once")
        folder = virus.brainfile_folder()
        text = (
            (folder / "brainfile.xml").read_text().replace("Synthetic CRC Virus", "Renamed Virus")
        )
        (folder / "brainfile.xml").write_text(text + "\n")
        report = virus.detect(amiga_disk(crc_virus()), Platform.AMIGA)
        self.assertEqual(report.name, "Renamed")

    def test_document_type_declarations_are_refused(self) -> None:
        hostile = (
            b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "aaaa">]>'
            b"<Bootblocks><Bootblock><Name>&a;</Name><Class>v</Class></Bootblock></Bootblocks>"
        )
        with self.assertRaises(virus.BrainfileError):
            virus.Brainfile.parse(hostile)
        with self.assertRaises(virus.BrainfileError):
            virus.Brainfile.parse(b"<Bootblocks/>")

    def test_a_broken_brainfile_is_treated_as_absent(self) -> None:
        (virus.brainfile_folder() / "brainfile.xml").write_text("<Bootblocks><Bootblock>")
        self.assertIsNone(virus.brainfile())
        self.assertEqual(virus.brainfile_status(), (False, "", 0))
        report = virus.detect(amiga_disk(crc_virus()), Platform.AMIGA)
        self.assertEqual(report.status, VirusStatus.UNKNOWN_BOOT)


def coded_block(value_at: int, value: bytes, *, code: bool) -> bytes:
    """A bootable block with ``value`` at ``value_at``, and a disk write when ``code``."""
    block = bytearray(1024)
    block[0:4] = b"DOS\0"
    struct.pack_into(">L", block, 8, 880)
    block[12:14] = b"\x60\xfe"  # BRA.S to itself
    if code:
        block[0x20 : 0x20 + len(AMIGA_CMD_WRITE)] = AMIGA_CMD_WRITE
    block[value_at : value_at + len(value)] = value
    return checksummed(block)


class AmigaBuiltInTests(VirusEnvironment, unittest.TestCase):
    """Built-in signatures and marker rules name Amiga viruses without the brainfile."""

    def setUp(self) -> None:
        self.use_environment()
        self.block = synthetic_boot("built-in virus", 0, b"SYNTHETIC BUILT-IN VIRUS")
        self.use_data("AMIGA_SIGNATURES", window_toml("Built-in Window", self.block, 40, 200))
        self.use_data("AMIGA_MARKERS", "")

    def test_a_signature_names_the_virus_and_it_can_be_removed(self) -> None:
        self.assertIsNone(virus.brainfile())
        for flags, release in ((0, "1.3"), (1, "2.0")):
            with self.subTest(flags=flags):
                block = bytearray(self.block)
                block[3] = flags
                image = amiga_disk(checksummed(block))
                report = virus.detect(image, Platform.AMIGA)
                self.assertEqual(report.status, VirusStatus.VIRUS)
                self.assertEqual((report.name, report.kind), ("Built-in Window", "boot"))
                self.assertEqual(report.source, "built-in")
                self.assertTrue(report.removable)
                self.assertIn("A synthetic test virus.", report.explanation)
                cleaned = virus.clean(image, Platform.AMIGA)
                self.assertEqual(cleaned[:1024], virus.standard_boot_block(flags, release))
                self.assertEqual(cleaned[1024:], image[1024:])

    def test_the_signature_covers_its_window_only(self) -> None:
        block = bytearray(self.block)
        block[500:600] = bytes(100)  # outside the window, like a counter the virus changes
        report = virus.detect(amiga_disk(checksummed(block)), Platform.AMIGA)
        self.assertEqual(report.name, "Built-in Window")
        block[100] ^= 0x01
        report = virus.detect(amiga_disk(checksummed(block)), Platform.AMIGA)
        self.assertEqual(report.status, VirusStatus.UNKNOWN_BOOT)

    def test_the_root_block_rule_applies_to_built_in_names(self) -> None:
        image = amiga_disk(self.block, root=False)
        report = virus.detect(image, Platform.AMIGA)
        self.assertEqual((report.status, report.removable), (VirusStatus.VIRUS, False))
        self.assertIn("no intact AmigaDOS file system", report.explanation)
        with self.assertRaises(virus.VirusError):
            virus.clean(image, Platform.AMIGA)

    def test_an_inactive_copy_is_named_but_clean(self) -> None:
        block = bytearray(self.block)
        block[4] ^= 0x01
        report = virus.detect(amiga_disk(bytes(block)), Platform.AMIGA)
        self.assertEqual(report.status, VirusStatus.CLEAN)
        self.assertIn("inactive copy of the Built-in Window virus", report.explanation)

    def test_a_rule_needs_every_check(self) -> None:
        checks = [(0x40, b"\x12\x34\x56\x78"), (0x80, b"\x9a\xbc\xde\xf0")]
        self.use_data("AMIGA_MARKERS", rule_toml("Pair Rule", checks))
        block = bytearray(coded_block(0x40, checks[0][1], code=False))
        block[0x80:0x84] = checks[1][1]
        report = virus.detect(amiga_disk(checksummed(block)), Platform.AMIGA)
        self.assertEqual((report.status, report.name), (VirusStatus.VIRUS, "Pair Rule"))
        self.assertTrue(report.removable)
        block[0x83] ^= 0x01
        report = virus.detect(amiga_disk(checksummed(block)), Platform.AMIGA)
        self.assertEqual(report.status, VirusStatus.UNKNOWN_BOOT)

    def test_a_single_value_rule_needs_virus_code_as_well(self) -> None:
        # One AntiCicloVir value alone also holds on some loaders and anti-virus blocks.
        self.use_data("AMIGA_MARKERS", rule_toml("Single Value", [(0x100, b"TEST")], code=True))
        bare = virus.detect(amiga_disk(coded_block(0x100, b"TEST", code=False)), Platform.AMIGA)
        self.assertEqual(bare.status, VirusStatus.UNKNOWN_BOOT)
        coded = virus.detect(amiga_disk(coded_block(0x100, b"TEST", code=True)), Platform.AMIGA)
        self.assertEqual((coded.status, coded.name), (VirusStatus.VIRUS, "Single Value"))

    def test_signatures_come_before_rules_and_rules_go_in_order(self) -> None:
        value = self.block[300:304]
        self.use_data(
            "AMIGA_MARKERS",
            rule_toml("First Rule", [(300, value)]) + rule_toml("Second Rule", [(300, value)]),
        )
        self.assertEqual(
            virus.detect(amiga_disk(self.block), Platform.AMIGA).name, "Built-in Window"
        )
        self.use_data("AMIGA_SIGNATURES", "")
        self.assertEqual(virus.detect(amiga_disk(self.block), Platform.AMIGA).name, "First Rule")

    def test_malformed_rules_are_ignored(self) -> None:
        text = (
            '[[rule]]\nname = "No checks"\nchecks = []\n'
            + rule_toml("Past the end", [(1022, b"\x01\x02\x03\x04")])
            + '[[rule]]\nname = "Bad hex"\nchecks = [{ offset = 16, bytes = "zz" }]\n'
            + '[[rule]]\nname = "String flag"\nchecks = [{ offset = 16, bytes = "01" }]\n'
            + 'code = "yes"\n'
        )
        self.use_data("AMIGA_MARKERS", text)
        rules = virus.amiga_rules()
        self.assertEqual([rule.name for rule in rules], ["String flag"])
        self.assertFalse(rules[0].code, "only a real true asks for code patterns")

    def test_the_brainfile_still_decides_when_it_names_the_block(self) -> None:
        # Built-in windows over the fixture's own blocks, under other names.
        self.use_data(
            "AMIGA_SIGNATURES",
            window_toml("Built-in CRC", crc_virus(), 40, 200)
            + window_toml("Built-in Loader", raw_crc_loader(), 40, 200)
            + window_toml("Built-in Antivirus", recogniser_antivirus(), 40, 200),
        )
        self.assertEqual(virus.detect(amiga_disk(crc_virus()), Platform.AMIGA).name, "Built-in CRC")
        install_fixture_brainfile()
        named = virus.detect(amiga_disk(crc_virus()), Platform.AMIGA)
        self.assertEqual((named.name, named.source), ("Synthetic CRC", "Amiga Bootblock Reader"))
        loader = virus.detect(amiga_disk(raw_crc_loader()), Platform.AMIGA)
        self.assertEqual(loader.status, VirusStatus.KNOWN_BOOT, "the brainfile's class wins")
        antivirus = virus.detect(amiga_disk(recogniser_antivirus()), Platform.AMIGA)
        self.assertEqual(antivirus.status, VirusStatus.ANTIVIRUS)
        # The recogniser finds the anti-virus whatever its checksum, and its class
        # still wins over the built-in window when the block cannot run.
        stale = bytearray(recogniser_antivirus())
        stale[4] ^= 0x01
        report = virus.detect(amiga_disk(bytes(stale)), Platform.AMIGA)
        self.assertEqual(report.status, VirusStatus.CLEAN)
        self.assertNotIn("inactive copy", report.explanation)


class ShippedAmigaDataTests(VirusEnvironment, unittest.TestCase):
    """The shipped Amiga signatures and rules: their shape, and no false alarms."""

    def setUp(self) -> None:
        self.use_environment()

    def test_the_shipped_signatures_are_well_formed(self) -> None:
        signatures = virus.amiga_signatures()
        text = virus.AMIGA_SIGNATURES.read_text(encoding="utf-8")
        self.assertTrue(text.isascii())
        self.assertEqual(text.count("[[signature]]"), len(signatures), "every entry loads")
        self.assertEqual(len(signatures), 34)
        self.assertEqual(len({s.sha1 for s in signatures}), len(signatures))
        for item in signatures:
            with self.subTest(name=item.name):
                self.assertEqual(len(bytes.fromhex(item.sha1)), 20)
                self.assertGreaterEqual(item.offset, 12, "never the DOS type or checksum")
                self.assertLessEqual(item.offset + item.length, 1024)
                self.assertGreaterEqual(item.length, 50)
                self.assertTrue(item.source.startswith("Derived from sample"))
        names = {item.name for item in signatures}
        for name in ("SCA", "Byte Bandit", "Byte Warrior", "Lamer Exterminator 1", "Saddam"):
            self.assertIn(name, names)
        # Names that virus-kinds.toml describes keep their notes.
        self.assertEqual(virus.virus_kind("Lamer Exterminator 1", "amiga").kind, "boot")
        self.assertIn("Disk-Validator", virus.virus_kind("Saddam", "amiga").note)

    def test_the_shipped_rules_are_well_formed(self) -> None:
        rules = virus.amiga_rules()
        text = virus.AMIGA_MARKERS.read_text(encoding="utf-8")
        self.assertTrue(text.isascii())
        self.assertEqual(text.count("[[rule]]"), len(rules), "every entry loads")
        for credit in ("AntiCicloVir 2.4", "Matthias Gutt", "public domain", "VirusX 4.0"):
            self.assertIn(credit, text)
        self.assertIn("Steve Tibbett and Dan James", text)
        virusx = [rule for rule in rules if rule.source.startswith("VirusX 4.0")]
        acv = [rule for rule in rules if rule.source.startswith("AntiCicloVir 2.4")]
        self.assertEqual((len(virusx), len(acv)), (25, len(rules) - 25))
        for rule in rules:
            with self.subTest(name=rule.name, source=rule.source):
                for _offset, data in rule.checks:
                    self.assertEqual(len(data), 4)
                    self.assertNotIn(data, (bytes(4), b"\xff" * 4))
                # A lone value is weak: it must come with the code check.
                self.assertTrue(len(rule.checks) >= 2 or rule.code)
        for rule in virusx:
            self.assertEqual((len(rule.checks), rule.code), (2, False))
        # These AntiCicloVir values also hold on boot blocks that are not viruses.
        for noisy in ("WAFT", "GX.Team", "BLF", "Virus Construction Set", "Incognito"):
            self.assertNotIn(noisy, {rule.source.split(", ", 1)[1] for rule in acv})

    def test_synthetic_blocks_raise_no_false_alarm(self) -> None:
        rng = random.Random(11)
        vectors = bytes.fromhex("2D7C00FC0000002E")  # move.l #$FC0000,$2E(a6): CoolCapture
        for index in range(300):
            block = bytearray(1024)
            block[0:4] = b"DOS" + bytes([index % 6])
            struct.pack_into(">L", block, 8, 880)
            block[12:1024] = rng.randbytes(1012)
            at = 12 + 2 * rng.randrange(400)
            block[at : at + 14] = AMIGA_CMD_WRITE + vectors
            report = virus.detect(amiga_disk(checksummed(block)), Platform.AMIGA)
            self.assertEqual(report.status, VirusStatus.UNKNOWN_BOOT, index)
        for flags in range(6):
            for release in ("1.3", "2.0"):
                block = virus.standard_boot_block(flags, release)
                self.assertIsNone(virus.amiga_identify(block))


# Atari ST -----------------------------------------------------------------------


class AtariTests(VirusEnvironment, unittest.TestCase):
    def setUp(self) -> None:
        self.use_environment()
        self.body = st_body()
        self.use_st_signatures(st_signature_toml("Synthetic Ghost", self.body))
        self.raw = make_st_image("st virus test")

    def test_a_non_executable_sector_is_clean(self) -> None:
        report = virus.detect(self.raw, Platform.ATARI_ST)
        self.assertEqual(report.status, VirusStatus.CLEAN)
        self.assertIn("not executable", report.explanation)

    def test_a_signature_names_the_virus_and_cleaning_keeps_the_parameters(self) -> None:
        infected = st_infected(self.raw, self.body)
        report = virus.detect(infected, Platform.ATARI_ST)
        self.assertEqual((report.status, report.name), (VirusStatus.VIRUS, "Synthetic Ghost"))
        self.assertTrue(report.removable)
        self.assertIn("A synthetic test virus.", report.explanation)
        cleaned = virus.clean(infected, Platform.ATARI_ST)
        self.assertEqual(cleaned[:0x1E], infected[:0x1E], "branch, OEM, serial and BPB kept")
        self.assertEqual(cleaned[0x1E:0x1FE], bytes(0x1FE - 0x1E))
        self.assertNotEqual(virus.st_word_sum(cleaned[:512]), 0x1234)
        self.assertEqual(cleaned[512:], infected[512:])
        self.assertEqual(virus.detect(cleaned, Platform.ATARI_ST).status, VirusStatus.CLEAN)

    def test_the_signature_covers_the_body_not_the_sector(self) -> None:
        infected = bytearray(st_infected(self.raw, self.body))
        infected[8:11] = b"\x12\x34\x56"  # another serial number
        other = st_executable(infected[:512]) + bytes(infected[512:])
        self.assertEqual(virus.detect(other, Platform.ATARI_ST).name, "Synthetic Ghost")
        changed = bytearray(self.body)
        changed[100] ^= 0x01
        report = virus.detect(st_infected(self.raw, bytes(changed)), Platform.ATARI_ST)
        self.assertEqual(report.status, VirusStatus.UNKNOWN_BOOT)

    def test_the_sum_of_a_cleaned_sector_never_makes_it_executable(self) -> None:
        # A parameter block whose words alone add up to 0x1234.
        sector = bytearray(self.raw[:512])
        sector[0x1E:] = bytes(512 - 0x1E)
        partial = sum(struct.unpack(">15H", sector[:30])) & 0xFFFF
        struct.pack_into(
            ">H", sector, 0, (struct.unpack_from(">H", sector, 0)[0] + 0x1234 - partial) & 0xFFFF
        )
        raw = st_infected(bytes(sector) + self.raw[512:], self.body)
        cleaned = virus.clean(raw, Platform.ATARI_ST)
        self.assertNotEqual(virus.st_word_sum(cleaned[:512]), 0x1234)

    def test_parameters_that_do_not_describe_the_image_block_removal(self) -> None:
        infected = st_infected(self.raw, self.body)
        for image in (infected[:-512], infected + bytes(512)):
            with self.subTest(size=len(image)):
                report = virus.detect(image, Platform.ATARI_ST)
                self.assertEqual(report.status, VirusStatus.VIRUS)
                self.assertFalse(report.removable)
                with self.assertRaises(virus.VirusError):
                    virus.clean(image, Platform.ATARI_ST)
        track = 9 * 2 * 512
        self.assertTrue(virus.detect(infected + bytes(track), Platform.ATARI_ST).removable)

    def test_immunisers_and_loaders_are_recognised(self) -> None:
        short = bytearray(self.raw[:512])
        short[0:2] = b"\x60\x38"
        short[0x3A:0x3C] = b"\x4e\x75"
        word = bytearray(self.raw[:512])
        word[0:4] = b"\x60\x00\x00\x40"
        word[0x42:0x44] = b"\x4e\x75"
        for sector in (short, word):
            report = virus.detect(st_executable(sector) + self.raw[512:], Platform.ATARI_ST)
            self.assertEqual((report.status, report.name), (VirusStatus.ANTIVIRUS, "Immuniser"))
        loader = bytearray(self.raw[:512])
        loader[2:8] = b"Loader"
        loader[0x3A:0x40] = b"\x3f\x3c\x00\x09\x4e\x4e"
        report = virus.detect(st_executable(loader) + self.raw[512:], Platform.ATARI_ST)
        self.assertEqual((report.status, report.name), (VirusStatus.KNOWN_BOOT, "TOS boot loader"))

    def test_unknown_code_lists_its_patterns(self) -> None:
        sector = bytearray(self.raw[:512])
        sector[0x3A:0x40] = bytes.fromhex("3F3C00094E4E")  # Flopwr
        sector[0x41:0x43] = b"\x12\x34"  # at an odd offset: not code
        report = virus.detect(st_executable(sector) + self.raw[512:], Platform.ATARI_ST)
        self.assertEqual(report.status, VirusStatus.UNKNOWN_BOOT)
        self.assertIn("writes floppy sectors", report.explanation)
        self.assertNotIn("0x1234", report.explanation)

    def test_the_shipped_signatures_are_well_formed(self) -> None:
        # setUp points the module at a synthetic file; read the shipped one here.
        signatures = self.shipped("ST_SIGNATURES", virus.st_signatures)
        shipped = {item.name: item for item in signatures}
        self.assertEqual(len(shipped), len(signatures), "one signature per virus")
        self.assertEqual(
            set(shipped),
            {"Ghost", "Signum/BPL", "Kobold #2", "OLI", "Mad", "C'T", "Blot/Swiss/FAT", "Toubab"},
        )
        # The 66-byte window of Ghost's install code matches every Ghost
        # variant found on real disks; the old 430-byte window matched none.
        ghost = shipped["Ghost"]
        self.assertEqual((ghost.offset, ghost.length), (0x1E, 66))
        self.assertEqual(ghost.sha1, "0fe84e3489f1a5152858d45b297a645952634151")
        self.assertNotIn("f4bd0d2be07dff39b35e11aac14175a01456d293", {s.sha1 for s in signatures})
        for item in signatures:
            with self.subTest(name=item.name):
                self.assertEqual(len(bytes.fromhex(item.sha1)), 20)
                self.assertLessEqual(item.offset + item.length, 0x1FE, "never the checksum word")
                self.assertGreaterEqual(item.offset, 0x1E, "never the parameter block")
                self.assertLessEqual(item.length, 200, "the invariant code, not the sector")
                self.assertTrue(item.source and item.note)
                self.assertTrue(item.note.isascii() and item.source.isascii())

    def test_a_copy_that_cannot_run_is_named_but_clean(self) -> None:
        infected = bytearray(st_infected(self.raw, self.body))
        infected[0x1FE] ^= 0x01  # the sum no longer makes it executable
        report = virus.detect(bytes(infected), Platform.ATARI_ST)
        self.assertEqual(report.status, VirusStatus.CLEAN)
        self.assertIn("not executable", report.explanation)
        self.assertIn("inactive copy of the Synthetic Ghost virus", report.explanation)
        # An opening branch straight to a return: the body after it never runs.
        immunised = bytearray(st_infected(self.raw, self.body))
        immunised[0:2] = b"\x60\x1c"
        immunised[0x1E:0x20] = b"\x4e\x75"
        self.use_st_signatures(st_signature_toml("Synthetic Ghost", self.body[2:], 0x20))
        sector = st_executable(immunised[:512]) + bytes(immunised[512:])
        report = virus.detect(sector, Platform.ATARI_ST)
        self.assertEqual((report.status, report.name), (VirusStatus.ANTIVIRUS, "Immuniser"))
        self.assertIn("inactive copy of the Synthetic Ghost virus", report.explanation)
        with self.assertRaises(virus.VirusError):
            virus.clean(sector, Platform.ATARI_ST)

    def test_a_virus_on_a_disk_whose_oem_text_says_loader_is_found(self) -> None:
        # Ghost keeps the OEM text of the disk it infects; real infected menu
        # disks read "Loader" there, so the loader check must come after.
        infected = bytearray(st_infected(self.raw, self.body))
        infected[2:8] = b"Loader"
        sector = st_executable(infected[:512]) + bytes(infected[512:])
        report = virus.detect(sector, Platform.ATARI_ST)
        self.assertEqual((report.status, report.name), (VirusStatus.VIRUS, "Synthetic Ghost"))
        self.assertTrue(report.removable)


class StMarkerTests(VirusEnvironment, unittest.TestCase):
    """UVK markers in synthetic sectors, with the shipped marker values."""

    def setUp(self) -> None:
        self.use_environment()
        self.use_st_signatures(st_signature_toml("Synthetic Ghost", st_body()))
        self.raw = make_st_image("st marker test")
        self.markers = {marker.name: marker for marker in virus.st_markers()}

    def sector(self, *markers: str, code: bool = True, start: bytes = b"") -> bytes:
        """An executable boot sector carrying the shipped markers of ``markers``."""
        sector = bytearray(self.raw[:512])
        if start:
            sector[0 : len(start)] = start
        if code:
            sector[0x60 : 0x60 + len(ST_FLOPWR)] = ST_FLOPWR
        for name in markers:
            for marker in virus.st_markers():
                if marker.name == name:
                    sector[marker.offset : marker.offset + marker.size] = marker.data
        return st_executable(sector) + self.raw[512:]

    def test_a_marker_with_virus_code_is_probably_that_virus(self) -> None:
        image = self.sector("Goblin")
        report = virus.detect(image, Platform.ATARI_ST)
        self.assertEqual(report.status, VirusStatus.VIRUS)
        self.assertEqual((report.name, report.kind), ("probably Goblin", "boot"))
        self.assertFalse(report.removable, "never cleaned on a marker alone")
        self.assertTrue(report.infected)
        self.assertIn("probably infected with the Goblin virus", report.explanation)
        self.assertIn("by its marker only", report.explanation)
        self.assertIn("writes floppy sectors", report.explanation)
        self.assertIn("clean dump", report.explanation)
        with self.assertRaises(virus.VirusError) as caught:
            virus.clean(image, Platform.ATARI_ST)
        self.assertIn("by its marker only", caught.exception.message)
        self.assertNotIn("file system", caught.exception.message)

    def test_a_marker_without_virus_code_is_not_a_virus(self) -> None:
        report = virus.detect(self.sector("Goblin", code=False), Platform.ATARI_ST)
        self.assertEqual(report.status, VirusStatus.UNKNOWN_BOOT)

    def test_markers_only_count_on_a_sector_that_runs(self) -> None:
        image = bytearray(self.sector("Goblin"))
        image[0x1FF] ^= 0x01
        self.assertEqual(virus.detect(bytes(image), Platform.ATARI_ST).status, VirusStatus.CLEAN)
        immuniser = bytearray(self.sector("Goblin")[:512])
        immuniser[0x3A:0x3C] = b"\x4e\x75"  # the BRA.S at 0 now lands on a return
        report = virus.detect(st_executable(immuniser) + self.raw[512:], Platform.ATARI_ST)
        self.assertEqual(report.status, VirusStatus.ANTIVIRUS)
        loader = bytearray(self.sector("Goblin")[:512])
        loader[2:8] = b"Loader"
        report = virus.detect(st_executable(loader) + self.raw[512:], Platform.ATARI_ST)
        self.assertEqual(report.status, VirusStatus.KNOWN_BOOT)

    def test_an_immunised_sector_carrying_several_markers_is_not_flagged(self) -> None:
        # UVK immunisation writes the Puke B, Goblin and P.M.S. values side by side.
        report = virus.detect(self.sector("Puke B", "Goblin", "P.M.S."), Platform.ATARI_ST)
        self.assertEqual(report.status, VirusStatus.UNKNOWN_BOOT)
        self.assertIsNone(virus.st_marker(self.sector("Goblin", "P.M.S.")))
        self.assertEqual(virus.st_marker(self.sector("P.M.S.")), "P.M.S.")

    def test_a_virus_with_two_marker_words_needs_both(self) -> None:
        # The 0x3A word alone collides with menu boot sectors that use disk functions.
        word = next(m for m in virus.st_markers() if m.name == "Grim Reaper" and m.offset)
        only = bytearray(self.raw[:512])
        only[0x60:0x66] = ST_FLOPWR
        only[word.offset : word.offset + 2] = word.data
        report = virus.detect(st_executable(only) + self.raw[512:], Platform.ATARI_ST)
        self.assertEqual(report.status, VirusStatus.UNKNOWN_BOOT)
        report = virus.detect(self.sector("Grim Reaper"), Platform.ATARI_ST)
        self.assertEqual((report.status, report.name), (VirusStatus.VIRUS, "probably Grim Reaper"))

    def test_a_signature_names_the_virus_before_any_marker(self) -> None:
        body = st_body()
        image = bytearray(self.sector("Goblin")[:512])
        image[0x1E : 0x1E + len(body)] = body
        image[0x1A2:0x1A6] = self.markers["Goblin"].data  # inside the body's span
        self.use_st_signatures(st_signature_toml("Synthetic Ghost", bytes(image[0x1E:0x1BE])))
        report = virus.detect(st_executable(image) + self.raw[512:], Platform.ATARI_ST)
        self.assertEqual((report.name, report.removable), ("Synthetic Ghost", True))

    def test_weak_markers_never_flag(self) -> None:
        # 0.W $6038 is the branch TOS itself writes; the synthetic image starts with it.
        self.use_data("ST_MARKERS", marker_toml("Signum", 0, 2, 0x6038, "weak"))
        self.assertEqual(virus.st_markers(), ())
        report = virus.detect(self.sector(), Platform.ATARI_ST)
        self.assertEqual(report.status, VirusStatus.UNKNOWN_BOOT)
        # The same entry in the reliable tier would flag it, so the tier is what stops it.
        self.use_data("ST_MARKERS", marker_toml("Signum", 0, 2, 0x6038))
        self.assertEqual(virus.detect(self.sector(), Platform.ATARI_ST).status, VirusStatus.VIRUS)

    def test_malformed_marker_entries_are_ignored(self) -> None:
        text = (
            marker_toml("Too wide", 0, 3, 1)
            + marker_toml("Too big", 0, 1, 0x100)
            + marker_toml("Past the end", 511, 2, 1)
            + '[[marker]]\nname = "No value"\noffset = 0\nsize = 1\ntier = "reliable"\n'
            + marker_toml("Fine", 0x1A2, 4, 0x01020304)
        )
        self.use_data("ST_MARKERS", text)
        self.assertEqual([marker.name for marker in virus.st_markers()], ["Fine"])

    def test_the_shipped_markers_are_reliable_and_specific(self) -> None:
        markers = self.shipped("ST_MARKERS", virus.st_markers)
        text = (virus.DATA_DIR / "st-markers.toml").read_text(encoding="utf-8")
        self.assertTrue(text.isascii())
        self.assertIn("Richard Karsmakers", text)
        self.assertEqual(text.count("[[marker]]"), len(markers), "every entry loads")
        widths: dict[str, int] = {}
        for marker in markers:
            self.assertEqual(marker.tier, "reliable")
            widths[marker.name] = widths.get(marker.name, 0) + marker.size
        self.assertEqual(
            set(widths),
            {"Goblin", "Evil", "P.M.S.", "Puke B", "Grim Reaper", "Zoch", "Macumba 5.2"},
        )
        for name, width in widths.items():
            self.assertGreaterEqual(width, 4, f"{name}: a byte or a word alone collides")
        for common in (0x60, 0x6038, 0x601C, 0xEB34, 0x4E71, 0x263C0000):
            self.assertNotIn(
                common, {marker.value for marker in markers if marker.offset in (0, 0x1E)}
            )


# Catalogue flags ------------------------------------------------------------------


class CatalogueFlagTests(VirusEnvironment, unittest.TestCase):
    def setUp(self) -> None:
        self.use_environment()

    def test_a_file_virus_is_flagged_not_removable(self) -> None:
        image = amiga_disk(virus.standard_boot_block(0))
        report = virus.detect(image, Platform.AMIGA, catalogue_virus="Saddam 1")
        self.assertEqual(report.status, VirusStatus.FLAGGED)
        self.assertEqual((report.name, report.kind, report.source), ("Saddam 1", "system", "TOSEC"))
        self.assertFalse(report.removable)
        self.assertIn("lives outside the boot block", report.explanation)
        self.assertIn("Disk-Validator", report.explanation)
        self.assertTrue(report.infected)
        with self.assertRaises(virus.VirusError):
            virus.clean(image, Platform.AMIGA)

    def test_an_unknown_name_is_probably_a_file_virus(self) -> None:
        image = amiga_disk(virus.standard_boot_block(0))
        report = virus.detect(image, Platform.AMIGA, catalogue_virus="Mystery 3")
        self.assertEqual((report.status, report.kind), (VirusStatus.FLAGGED, ""))
        self.assertIn("probably a file or link virus", report.explanation)

    def test_a_boot_virus_the_brainfile_would_name_suggests_the_download(self) -> None:
        image = amiga_disk(crc_virus())
        report = virus.detect(image, Platform.AMIGA, catalogue_virus="SCA")
        self.assertEqual((report.status, report.kind), (VirusStatus.FLAGGED, "boot"))
        self.assertIn("Download the Amiga Bootblock Reader brainfile", report.explanation)
        install_fixture_brainfile()
        same = virus.detect(image, Platform.AMIGA, catalogue_virus="Synthetic CRC Virus")
        self.assertEqual((same.status, same.source), (VirusStatus.VIRUS, "Amiga Bootblock Reader"))
        self.assertNotIn("TOSEC", same.explanation)
        other = virus.detect(image, Platform.AMIGA, catalogue_virus="Saddam")
        self.assertEqual((other.status, other.name), (VirusStatus.VIRUS, "Synthetic CRC"))
        self.assertIn("TOSEC lists this dump as infected with Saddam as well", other.explanation)

    def test_a_flag_without_an_image(self) -> None:
        report = virus.flagged("Byte Bandit 1", Platform.AMIGA)
        self.assertEqual((report.status, report.kind), (VirusStatus.FLAGGED, "boot"))
        self.assertIn("not at hand", report.explanation)

    def test_names_match_by_leading_words(self) -> None:
        self.assertTrue(virus.same_virus("SCA", "SCA Virus"))
        self.assertTrue(virus.same_virus("Byte Bandit", "Byte Bandit 1"))
        self.assertFalse(virus.same_virus("Byte Bandit", "Byte Warrior"))
        self.assertEqual(virus.virus_kind("Lamer Exterminator 4", "amiga").kind, "boot")
        self.assertIsNone(virus.virus_kind("Saddam", Platform.ATARI_ST))


# Installing the brainfile -----------------------------------------------------------


def release_zip(members: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, data in members.items():
            archive.writestr(name, data)
    return buffer.getvalue()


class FakeDownloader:
    def __init__(self, release: dict, archive: bytes, *, cancel: bool = False) -> None:
        self.release = release
        self.archive = archive
        self.cancel = cancel
        self.urls: list[str] = []

    def get_json(self, url: str, *, headers=None, cancel=None, **options):
        self.urls.append(url)
        return self.release

    def download(self, url: str, target: Path, *, progress=None, cancel=None) -> Path:
        self.urls.append(url)
        if self.cancel:
            raise DownloadCancelled("The download was cancelled.")
        Path(target).write_bytes(self.archive)
        if progress:
            progress(len(self.archive), len(self.archive))
        return Path(target)


class InstallTests(VirusEnvironment, unittest.TestCase):
    def setUp(self) -> None:
        self.use_environment()
        self.fixture = {name: (FIXTURES / name).read_bytes() for name in virus.BRAINFILE_FILES}
        self.release = {
            "tag_name": "v6.1",
            "html_url": "https://github.invalid/release",
            "zipball_url": "https://api.github.invalid/zipball",
            "assets": [
                {"name": "notes.txt", "browser_download_url": "https://github.invalid/notes.txt"},
                {"name": "ABR v6.zip", "browser_download_url": "https://github.invalid/abr.zip"},
            ],
        }

    def test_the_release_zip_is_unpacked_and_used(self) -> None:
        archive = release_zip(
            {
                "Old/Debug/brainfile.xml": b"<Bootblocks/>",
                "brainfile.xml": self.fixture["brainfile.xml"],
                "searchbrain.xml": self.fixture["searchbrain.xml"],
                "catlist.xml": self.fixture["catlist.xml"],
                "AmigaBBReader.exe": b"not needed",
            }
        )
        downloader = FakeDownloader(self.release, archive)
        progress: list[tuple[int, int | None]] = []
        self.assertEqual(virus.brainfile_status(), (False, "", 0))
        folder = virus.install_brainfile(downloader, lambda d, t: progress.append((d, t)))
        self.assertEqual(folder, virus.brainfile_folder())
        self.assertEqual(
            downloader.urls, [virus.BRAINFILE_RELEASE_API, "https://github.invalid/abr.zip"]
        )
        self.assertEqual(
            sorted(p.name for p in folder.iterdir()),
            sorted([*virus.BRAINFILE_FILES, "release.json"]),
        )
        self.assertEqual(virus.brainfile_status(), (True, "v6.1", 5))
        self.assertTrue(progress)
        report = virus.detect(amiga_disk(crc_virus()), Platform.AMIGA)
        self.assertEqual(report.status, VirusStatus.VIRUS)
        self.assertEqual([p.name for p in folder.parent.iterdir()], ["abr"], "no scratch left")

    def test_a_release_without_assets_uses_the_source_zip(self) -> None:
        self.release["assets"] = []
        archive = release_zip({"repo-abc/brainfile.xml": self.fixture["brainfile.xml"]})
        downloader = FakeDownloader(self.release, archive)
        virus.install_brainfile(downloader)
        self.assertEqual(downloader.urls[-1], "https://api.github.invalid/zipball")
        self.assertEqual(virus.brainfile_status(), (True, "v6.1", 4))

    def test_a_bad_download_keeps_the_installed_brainfile(self) -> None:
        install_fixture_brainfile()
        before = virus.brainfile_status()
        for archive in (b"not a zip", release_zip({"readme.txt": b"no brainfile"})):
            with self.subTest(size=len(archive)), self.assertRaises(virus.BrainfileError):
                virus.install_brainfile(FakeDownloader(self.release, archive))
        broken = release_zip({"brainfile.xml": b"<Bootblocks><Bootblock>"})
        with self.assertRaises(virus.BrainfileError):
            virus.install_brainfile(FakeDownloader(self.release, broken))
        self.assertEqual(virus.brainfile_status(), before)
        self.assertEqual([p.name for p in virus.brainfile_folder().parent.iterdir()], ["abr"])

    def test_cancelling_installs_nothing(self) -> None:
        downloader = FakeDownloader(self.release, b"", cancel=True)
        with self.assertRaises(DownloadCancelled):
            virus.install_brainfile(downloader)
        self.assertEqual(virus.brainfile_status(), (False, "", 0))


class PrepareTests(VirusEnvironment, unittest.TestCase):
    """``prepare(clean_virus=True)`` writes a cleaned copy and says so."""

    def setUp(self) -> None:
        self.folder = self.use_environment(brainfile=True)
        self.body = st_body()
        self.use_st_signatures(st_signature_toml("Synthetic Ghost", self.body))

    def prepare(self, data: bytes, name: str, **options):
        from piratefinder.images.prepare import prepare

        workdir = Path(tempfile.mkdtemp(dir=self.folder))
        prepared = prepare(data, name, workdir, label="Test", platform=None, **options)
        return prepared, Path(prepared.write_path).read_bytes()

    def test_an_amiga_virus_is_removed_from_the_written_copy(self) -> None:
        from tests.test_images_synthetic import dms_archive

        image = amiga_disk(crc_virus())
        for data, name in ((image, "menu.adf"), (dms_archive(image), "menu.dms")):
            with self.subTest(name=name):
                prepared, written = self.prepare(data, name, clean_virus=True)
                self.assertEqual(written[:1024], virus.standard_boot_block(0))
                self.assertEqual(written[1024:], image[1024:])
                self.assertIn(
                    "Removed the Synthetic CRC boot block virus before writing; the stored image "
                    "is unchanged.",
                    prepared.notes,
                )

    def test_an_st_virus_is_removed_from_the_written_copy(self) -> None:
        image = st_infected(make_st_image("prepare"), self.body)
        prepared, written = self.prepare(image, "menu.st", clean_virus=True)
        self.assertEqual(written, virus.clean(image, Platform.ATARI_ST))
        self.assertTrue(any("Synthetic Ghost" in note for note in prepared.notes))

    def test_the_image_is_written_as_it_is_otherwise(self) -> None:
        image = amiga_disk(crc_virus())
        prepared, written = self.prepare(image, "menu.adf")
        self.assertEqual(written, image)
        self.assertTrue(any("removal was switched off" in note for note in prepared.notes))
        stuck = amiga_disk(crc_virus(), root=False)
        prepared, written = self.prepare(stuck, "menu.adf", clean_virus=True)
        self.assertEqual(written, stuck)
        self.assertTrue(any("cannot remove" in note for note in prepared.notes))
        clean = amiga_disk(virus.standard_boot_block(0))
        prepared, written = self.prepare(clean, "menu.adf", clean_virus=True)
        self.assertEqual((written, prepared.notes), (clean, ()))


class SpeedTests(VirusEnvironment, unittest.TestCase):
    def test_a_library_of_standard_disks_is_checked_quickly(self) -> None:
        self.use_environment(brainfile=True)
        image = amiga_disk(virus.standard_boot_block(1))
        unknown = amiga_disk(synthetic_boot("unknown"))
        import time

        started = time.perf_counter()
        for _ in range(500):
            virus.detect(image, Platform.AMIGA)
            virus.detect(unknown, Platform.AMIGA)
        self.assertLess(time.perf_counter() - started, 5.0)


class FingerprintTests(VirusEnvironment, unittest.TestCase):
    """What the library compares to know its boot blocks need checking again."""

    def setUp(self) -> None:
        self.use_environment()

    def test_it_is_stable_until_the_data_code_or_brainfile_changes(self) -> None:
        first = virus.fingerprint()
        self.assertEqual(virus.fingerprint(), first)
        with mock.patch.object(virus, "DETECTION_VERSION", virus.DETECTION_VERSION + 1):
            self.assertNotEqual(virus.fingerprint(), first)
        install_fixture_brainfile()
        with_brainfile = virus.fingerprint()
        self.assertNotEqual(with_brainfile, first)
        shutil.rmtree(virus.brainfile_folder())
        self.assertEqual(virus.fingerprint(), first)
        self.use_st_signatures(st_signature_toml("Synthetic Ghost", st_body()))
        self.assertNotEqual(virus.fingerprint(), first)

    def test_every_shipped_data_file_is_one_detection_reads(self) -> None:
        read = {
            virus.ST_SIGNATURES,
            virus.ST_MARKERS,
            virus.AMIGA_SIGNATURES,
            virus.AMIGA_MARKERS,
            virus.VIRUS_KINDS,
            virus.INDICATORS,
        }
        self.assertEqual({path for path in virus.DATA_DIR.iterdir() if path.is_file()}, read)


if __name__ == "__main__":
    unittest.main()

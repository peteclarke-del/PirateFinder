from __future__ import annotations

import gzip
import io
import os
import shutil
import subprocess
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from piratefinder.images import archives
from piratefinder.images.inspect import inspect_bytes
from piratefinder.library.scanner import Cancellation, Scanner, read_image_bytes
from piratefinder.library.userdb import UserDatabase
from tests.test_library_helpers import make_adf, make_msa, make_st_image


def zip_bytes(members: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in members.items():
            archive.writestr(name, data)
    return buffer.getvalue()


class CountingInspect:
    def __init__(self) -> None:
        self.names: list[str] = []

    def __call__(self, data: bytes, name: str):
        self.names.append(name)
        return inspect_bytes(data, name)


class ScannerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.folder = Path(tempfile.mkdtemp(prefix="pf-scan-"))
        self.addCleanup(shutil.rmtree, self.folder, True)
        self.library = self.folder / "library"
        self.library.mkdir()
        self.db = UserDatabase.open(self.folder / "user.sqlite")
        self.addCleanup(self.db.close)
        self.inspect = CountingInspect()
        self.scanner = Scanner(self.db, inspect_bytes=self.inspect, archives=archives)

    def write(self, relative: str, data: bytes) -> Path:
        path = self.library / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    def test_finds_plain_images_archives_and_nested_archives(self) -> None:
        st = make_st_image("plain", files=("SONIC.PRG",))
        self.write("st/Plain.st", st)
        self.write("st/Packed.msa", make_msa(make_st_image("msa")))
        self.write("amiga/Disk.adf.gz", gzip.compress(make_adf("gz")))
        self.write("amiga/Direct.adz", gzip.compress(make_adf("adz")))
        inner = zip_bytes({"Inner.st": make_st_image("inner")})
        self.write(
            "sets/Set.zip",
            zip_bytes({"a/One.st": make_st_image("one"), "b.zip": inner, "readme.txt": b"hi"}),
        )
        self.write("notes.txt", b"not an image")
        self.write(".hidden/Hidden.st", make_st_image("hidden"))
        self.write("@eaDir/Thumb.st", make_st_image("thumb"))
        self.write("st/.AppleDouble.st", b"resource fork")

        summary = self.scanner.scan([self.library])

        rows = {(Path(entry.path).name, entry.member): entry for entry in self.db.entries()}
        self.assertEqual(
            sorted(rows),
            [
                ("Direct.adz", ""),
                ("Disk.adf.gz", "Disk.adf"),
                ("Packed.msa", ""),
                ("Plain.st", ""),
                ("Set.zip", "a/One.st"),
                ("Set.zip", "b.zip::Inner.st"),
            ],
        )
        self.assertEqual(summary.files_seen, 5)
        self.assertEqual(summary.images_found, 6)
        self.assertEqual(summary.new, 6)
        self.assertEqual(summary.unmatched, 6)
        self.assertEqual(summary.errors, ())
        plain = rows[("Plain.st", "")]
        self.assertEqual(plain.format, "st")
        self.assertEqual(plain.volume_label, "TESTDISK")
        self.assertIn("SONIC.PRG", plain.listing)
        self.assertEqual(plain.raw_size, len(st))
        packed = rows[("Packed.msa", "")]
        self.assertEqual(packed.format, "msa")
        self.assertNotEqual(packed.md5, packed.raw_md5)
        nested = rows[("Set.zip", "b.zip::Inner.st")]
        self.assertEqual(
            read_image_bytes(nested.path, nested.member, archives), make_st_image("inner")
        )

    def test_second_scan_reads_only_changed_files_and_removes_missing_ones(self) -> None:
        keep = self.write("Keep.st", make_st_image("keep"))
        change = self.write("Change.st", make_st_image("change"))
        gone = self.write("Gone.st", make_st_image("gone"))
        self.scanner.scan([self.library])
        self.assertEqual(len(self.inspect.names), 3)

        self.inspect.names.clear()
        summary = self.scanner.scan([self.library])
        self.assertEqual(self.inspect.names, [])
        self.assertEqual((summary.new, summary.removed, summary.images_found), (0, 0, 3))

        change.write_bytes(make_st_image("changed again"))
        os.utime(change, (1_000_000, 1_000_000))
        gone.unlink()
        summary = self.scanner.scan([self.library])
        self.assertEqual(self.inspect.names, ["Change.st"])
        self.assertEqual(summary.removed, 1)
        self.assertEqual(summary.images_found, 2)
        self.assertEqual(
            sorted(Path(e.path).name for e in self.db.entries()), ["Change.st", "Keep.st"]
        )
        self.assertTrue(keep.exists())

    def test_unavailable_folder_keeps_its_files(self) -> None:
        self.write("Disk.st", make_st_image("nas"))
        self.scanner.scan([self.library])
        moved = self.folder / "unmounted"
        self.library.rename(moved)
        summary = self.scanner.scan([self.library])
        self.assertEqual(len(self.db.entries()), 1)
        self.assertEqual(summary.removed, 0)
        self.assertTrue(any("could not be read" in error for error in summary.errors))

    def test_other_folders_are_left_alone(self) -> None:
        other = self.folder / "other"
        other.mkdir()
        (other / "Other.st").write_bytes(make_st_image("other"))
        self.write("Mine.st", make_st_image("mine"))
        self.scanner.scan([self.library, other])
        summary = self.scanner.scan([self.library])
        self.assertEqual(summary.removed, 0)
        self.assertEqual(len(self.db.entries()), 2)
        self.assertEqual(summary.images_found, 1)

    def test_corrupt_archive_is_reported_and_retried(self) -> None:
        self.write("Broken.zip", b"PK\x03\x04 this is not really a zip")
        self.write("Good.st", make_st_image("good"))
        summary = self.scanner.scan([self.library])
        self.assertEqual(len(summary.errors), 1)
        self.assertIn("Broken.zip", summary.errors[0])
        self.assertEqual(summary.images_found, 1)
        summary = self.scanner.scan([self.library])
        self.assertEqual(len(summary.errors), 1, "a file that failed is read again next time")

    def test_missing_7z_tool_is_reported(self) -> None:
        self.write("Set.7z", b"7z\xbc\xaf\x27\x1c" + b"\0" * 64)
        with mock.patch.object(archives, "find_7z", return_value=None):
            summary = self.scanner.scan([self.library])
        self.assertEqual(len(summary.errors), 1)
        self.assertIn("7-Zip", summary.errors[0])

    @unittest.skipUnless(archives.find_7z(), "7-Zip is not installed")
    def test_reads_7z_archives(self) -> None:
        source = self.folder / "src"
        source.mkdir()
        (source / "Menu 1.st").write_bytes(make_st_image("7z one"))
        (source / "Menu 2.st").write_bytes(make_st_image("7z two"))
        target = self.library / "Menus.7z"
        subprocess.run(
            [
                archives.find_7z(),
                "a",
                "-bd",
                str(target),
                str(source / "Menu 1.st"),
                str(source / "Menu 2.st"),
            ],
            check=True,
            capture_output=True,
        )
        summary = self.scanner.scan([self.library])
        self.assertEqual(summary.errors, ())
        self.assertEqual(sorted(e.member for e in self.db.entries()), ["Menu 1.st", "Menu 2.st"])

    def test_symlink_loops_end(self) -> None:
        self.write("deep/Disk.st", make_st_image("loop"))
        os.symlink(self.library, self.library / "deep" / "back")
        os.symlink(self.library / "deep", self.library / "again")
        summary = self.scanner.scan([self.library])
        self.assertEqual(summary.images_found, 1)

    @unittest.skipIf(os.geteuid() == 0, "root reads every file")
    def test_unreadable_file_is_reported(self) -> None:
        locked = self.write("Locked.st", make_st_image("locked"))
        locked.chmod(0)
        self.addCleanup(locked.chmod, 0o644)
        self.write("Open.st", make_st_image("open"))
        summary = self.scanner.scan([self.library])
        self.assertEqual(summary.images_found, 1)
        self.assertEqual(len(summary.errors), 1)
        self.assertIn("permission denied", summary.errors[0])

    def test_cancellation_stops_without_removing(self) -> None:
        for index in range(3):
            self.write(f"Disk{index}.st", make_st_image(f"cancel {index}"))
        self.scanner.scan([self.library])
        (self.library / "Disk0.st").unlink()
        cancel = Cancellation()
        cancel.cancel()
        summary = self.scanner.scan([self.library], cancel=cancel)
        self.assertTrue(summary.cancelled)
        self.assertEqual(summary.removed, 0)
        self.assertEqual(len(self.db.entries()), 3)

    def test_progress_reports_each_file(self) -> None:
        for index in range(3):
            self.write(f"Disk{index}.st", make_st_image(f"progress {index}"))
        calls: list[tuple[str, int, int]] = []
        self.scanner.scan([self.library], progress=lambda *call: calls.append(call))
        reading = [call for call in calls if call[0].startswith("Reading")]
        self.assertEqual(
            [(current, total) for _m, current, total in reading], [(1, 3), (2, 3), (3, 3)]
        )
        self.assertEqual(calls[-1][0], "Scan finished")

    def test_inspection_failure_keeps_the_file_unmatched(self) -> None:
        self.write("Odd.st", make_st_image("odd"))

        def broken(data: bytes, name: str):
            raise ValueError("unexpected header")

        scanner = Scanner(self.db, inspect_bytes=broken, archives=archives)
        summary = scanner.scan([self.library])
        self.assertEqual(summary.images_found, 1)
        entry = self.db.entries()[0]
        self.assertIn("unexpected header", entry.error)
        self.assertEqual(len(entry.md5), 32)


if __name__ == "__main__":
    unittest.main()

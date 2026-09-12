"""Listing and reading zip, gzip and 7z archives, nested one level deep."""

from __future__ import annotations

import subprocess
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from piratefinder.images import archives
from piratefinder.images.archives import ArchiveError, Member
from tests.test_images_synthetic import adf_image, gzip_bytes, msa_archive, st_image, zip_bytes


class ArchiveTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._folder = tempfile.TemporaryDirectory()
        self.folder = Path(self._folder.name)

    def tearDown(self) -> None:
        self._folder.cleanup()

    def write(self, name: str, data: bytes) -> Path:
        path = self.folder / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path


class ZipTests(ArchiveTestCase):
    def test_members_and_read(self) -> None:
        st = st_image()
        path = self.write("menu.zip", zip_bytes({"Menu 1.st": st, "info.txt": b"hello"}))
        self.assertTrue(archives.is_archive(path))
        self.assertEqual(
            archives.members(path), [Member("Menu 1.st", len(st)), Member("info.txt", 5)]
        )
        self.assertEqual(archives.disk_image_members(path), [Member("Menu 1.st", len(st))])
        self.assertEqual(archives.read_member(path, "Menu 1.st"), st)

    def test_empty_name_reads_the_first_disk_image(self) -> None:
        st = st_image()
        path = self.write("menu.zip", zip_bytes({"readme.txt": b"x", "disk/Menu.st": st}))
        self.assertEqual(archives.read_member(path, ""), st)

    def test_folders_and_backslashes_are_normalised(self) -> None:
        path = self.write("x.zip", zip_bytes({"Menus\\Disk 1.st": b"a" * 512, "dir/": b""}))
        self.assertEqual([m.name for m in archives.members(path)], ["Menus/Disk 1.st"])
        self.assertEqual(archives.read_member(path, "Menus/Disk 1.st"), b"a" * 512)

    def test_missing_member(self) -> None:
        path = self.write("x.zip", zip_bytes({"a.st": b"a"}))
        with self.assertRaisesRegex(ArchiveError, "not in the zip"):
            archives.read_member(path, "b.st")

    def test_damaged_zip(self) -> None:
        path = self.write("bad.zip", b"PK\x03\x04 this is not a zip")
        with self.assertRaises(ArchiveError):
            archives.members(path)

    def test_extensionless_zip_is_recognised_by_its_header(self) -> None:
        path = self.write("download", zip_bytes({"a.st": b"a"}))
        self.assertEqual(archives.archive_kind(path), "zip")
        self.assertFalse(archives.is_archive(self.write("disk.st", b"PK\x03\x04")))


class SafetyTests(ArchiveTestCase):
    def test_names_that_climb_out_are_not_listed_or_read(self) -> None:
        data = zip_bytes(
            {
                "../evil.st": b"e",
                "/etc/evil.st": b"e",
                "ok/../../evil.st": b"e",
                "C:/evil.st": b"e",
                "good.st": b"g",
            }
        )
        path = self.write("x.zip", data)
        self.assertEqual([m.name for m in archives.members(path)], ["good.st"])
        for name in ("../evil.st", "/etc/evil.st", "ok/../../evil.st"):
            with self.subTest(name=name), self.assertRaisesRegex(ArchiveError, "outside"):
                archives.read_member(path, name)

    def test_a_member_that_expands_too_far_is_refused(self) -> None:
        big = archives.MAX_MEMBER_SIZE + 1
        path = self.folder / "bomb.zip"
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("bomb.st", bytes(big))
        self.assertLess(path.stat().st_size, 1024 * 1024)
        with self.assertRaisesRegex(ArchiveError, "larger than"):
            archives.read_member(path, "bomb.st")
        errors: list[tuple[str, str]] = []
        found = list(archives.iter_members(path, on_error=lambda *error: errors.append(error)))
        self.assertEqual(found, [])
        self.assertEqual(errors[0][0], "bomb.st")

    def test_a_gzip_bomb_is_refused(self) -> None:
        path = self.write("bomb.st.gz", gzip_bytes(bytes(archives.MAX_MEMBER_SIZE + 1)))
        with self.assertRaises(ArchiveError):
            archives.read_member(path, "bomb.st")


class GzipTests(ArchiveTestCase):
    def test_adz_holds_an_adf_of_the_same_name(self) -> None:
        adf = adf_image()
        path = self.write("Compact 1.adz", gzip_bytes(adf))
        self.assertTrue(archives.is_archive(path))
        self.assertEqual(archives.members(path), [Member("Compact 1.adf", len(adf))])
        self.assertEqual(archives.read_member(path, "Compact 1.adf"), adf)
        self.assertEqual(archives.read_member(path, ""), adf)

    def test_gz_member_drops_the_suffix(self) -> None:
        st = st_image()
        path = self.write("Menu 5.st.gz", gzip_bytes(st))
        self.assertEqual(archives.disk_image_members(path), [Member("Menu 5.st", len(st))])
        self.assertEqual(archives.read_member(path, "Menu 5.st"), st)


class NestedTests(ArchiveTestCase):
    def test_zip_of_zips(self) -> None:
        first, second = st_image(seed=1), st_image(seed=2)
        inner_one = zip_bytes({"Menu 1.st": first})
        inner_two = zip_bytes({"Menu 2.msa": msa_archive(second, 80, 2, 10), "notes.txt": b"n"})
        path = self.write(
            "[Menus].zip", zip_bytes({"Menus/Menu 1.zip": inner_one, "Menus/Menu 2.zip": inner_two})
        )
        names = [member.name for member in archives.disk_image_members(path)]
        self.assertEqual(names, ["Menus/Menu 1.zip::Menu 1.st", "Menus/Menu 2.zip::Menu 2.msa"])
        self.assertEqual(archives.read_member(path, "Menus/Menu 1.zip::Menu 1.st"), first)
        found = dict(archives.iter_members(path))
        self.assertEqual(found[Member("Menus/Menu 1.zip::Menu 1.st", len(first))], first)
        self.assertEqual(len(found), 2)

    def test_iter_members_reads_only_the_named_members(self) -> None:
        inner = zip_bytes({"a.st": b"a" * 512, "b.st": b"b" * 512})
        path = self.write("outer.zip", zip_bytes({"in.zip": inner, "c.st": b"c" * 512}))
        found = [member.name for member, _ in archives.iter_members(path, ["in.zip::b.st"])]
        self.assertEqual(found, ["in.zip::b.st"])

    def test_only_one_level_is_followed(self) -> None:
        deepest = zip_bytes({"a.st": b"a"})
        middle = zip_bytes({"deep.zip": deepest})
        path = self.write("outer.zip", zip_bytes({"middle.zip": middle}))
        self.assertEqual([m.name for m in archives.members(path)], ["middle.zip::deep.zip"])
        with self.assertRaisesRegex(ArchiveError, "one archive inside another"):
            archives.read_member(path, "middle.zip::deep.zip::a.st")

    def test_a_damaged_inner_archive_is_listed_as_itself(self) -> None:
        path = self.write("outer.zip", zip_bytes({"broken.zip": b"not a zip", "a.st": b"a"}))
        self.assertEqual([m.name for m in archives.members(path)], ["broken.zip", "a.st"])
        errors: list[tuple[str, str]] = []
        found = [
            member.name
            for member, _ in archives.iter_members(
                path, on_error=lambda *error: errors.append(error)
            )
        ]
        self.assertEqual(found, ["a.st"])
        self.assertEqual(errors[0][0], "broken.zip")

    def test_gzip_inside_a_zip(self) -> None:
        st = st_image()
        path = self.write("outer.zip", zip_bytes({"Menu.st.gz": gzip_bytes(st)}))
        self.assertEqual(archives.read_member(path, "Menu.st.gz::Menu.st"), st)


@unittest.skipUnless(archives.find_7z(), "7-Zip is not installed")
class SevenZipTests(ArchiveTestCase):
    def make_7z(self, name: str, files: dict[str, bytes], *extra: str) -> Path:
        source = self.folder / f"src-{name}"
        for member, data in files.items():
            target = source / member
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        archive = self.folder / name
        subprocess.run(
            [archives.find_7z(), "a", "-bd", "-bso0", *extra, str(archive), "."],
            cwd=source,
            check=True,
            stdout=subprocess.DEVNULL,
        )
        return archive

    def test_7z_of_zips_as_local_collections_are_kept(self) -> None:
        disks = {f"Menu {n}.st": st_image(seed=n) for n in range(1, 4)}
        files = {
            f"[Menus]/Menu {n}.zip": zip_bytes({name: data})
            for n, (name, data) in enumerate(disks.items(), 1)
        }
        files["[Menus]/index.txt"] = b"list"
        path = self.make_7z("[Menus].7z", files)
        names = [member.name for member in archives.disk_image_members(path)]
        self.assertEqual(names, [f"[Menus]/Menu {n}.zip::Menu {n}.st" for n in range(1, 4)])
        self.assertEqual(
            archives.read_member(path, "[Menus]/Menu 2.zip::Menu 2.st"), disks["Menu 2.st"]
        )
        streamed = {member.name: data for member, data in archives.iter_members(path)}
        self.assertEqual(streamed["[Menus]/Menu 3.zip::Menu 3.st"], disks["Menu 3.st"])
        self.assertEqual(len(streamed), 3)

    def test_plain_images_in_a_solid_7z_are_streamed_once(self) -> None:
        disks = {f"Disk {n}.adf": adf_image(seed=n) for n in range(1, 4)}
        path = self.make_7z("disks.7z", disks, "-ms=on")
        found = {member.name: data for member, data in archives.iter_members(path)}
        self.assertEqual(found, disks)
        self.assertEqual(archives.read_member(path, "Disk 2.adf"), disks["Disk 2.adf"])

    def test_a_name_with_wildcard_characters_is_read_exactly(self) -> None:
        files = {"a*.st": b"1" * 512, "ab.st": b"2" * 512}
        path = self.make_7z("wild.7z", files)
        self.assertEqual(archives.read_member(path, "a*.st"), b"1" * 512)

    def test_damaged_7z(self) -> None:
        path = self.write("bad.7z", b"7z\xbc\xaf\x27\x1c" + bytes(64))
        with self.assertRaisesRegex(ArchiveError, "7-Zip could not"):
            archives.members(path)


class MissingSevenZipTests(ArchiveTestCase):
    def test_a_7z_without_7zip_explains_what_to_install(self) -> None:
        path = self.write("x.7z", b"7z\xbc\xaf\x27\x1c" + bytes(64))
        with mock.patch.object(archives, "find_7z", return_value=None):
            with self.assertRaisesRegex(ArchiveError, "needs 7-Zip"):
                archives.members(path)
            with self.assertRaisesRegex(ArchiveError, "needs 7-Zip"):
                archives.read_member(path, "a.st")


if __name__ == "__main__":
    unittest.main()

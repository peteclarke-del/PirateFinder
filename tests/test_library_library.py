from __future__ import annotations

import hashlib
import shutil
import tempfile
import unittest
from pathlib import Path

from piratefinder.images import archives
from piratefinder.library.library import Library, LinkError, display_name, match_entry
from piratefinder.library.userdb import LibraryEntry, UserDatabase
from piratefinder.models import Availability, ImageRecord, LocalFile
from tests.test_images_synthetic import zip_bytes
from tests.test_library_helpers import (
    CatalogueBuilder,
    SqlCatalogue,
    hashes,
    make_msa,
    make_st_image,
    store_module_available,
)


class RecordingCatalogue:
    """Answers match_image from a table of hash -> record and records every call."""

    def __init__(self, table: dict[tuple[str, str], ImageRecord]) -> None:
        self.table = table
        self.calls: list[dict[str, object]] = []

    def match_image(self, **hashes: object) -> ImageRecord | None:
        self.calls.append(hashes)
        for kind, value in hashes.items():
            if kind != "size" and (kind, value) in self.table:
                return self.table[(kind, value)]
        return None


class MatchOrderTests(unittest.TestCase):
    def entry(self) -> LibraryEntry:
        return LibraryEntry(
            path="/x.msa",
            size=900,
            md5="file-md5",
            sha1="file-sha1",
            crc32="filecrc",
            sha512="file-sha512",
            raw_md5="raw-md5",
            raw_sha1="raw-sha1",
            raw_crc32="rawcrc",
            raw_size=737280,
        )

    def test_order_is_raw_md5_sha1_crc_then_file_sha512_and_md5(self) -> None:
        catalogue = RecordingCatalogue({})
        self.assertIsNone(match_entry(catalogue, self.entry()))
        self.assertEqual(
            catalogue.calls,
            [
                {"md5": "raw-md5"},
                {"sha1": "raw-sha1"},
                {"crc32": "rawcrc", "size": 737280},
                {"sha512": "file-sha512"},
                {"md5": "file-md5"},
            ],
        )

    def test_first_hit_wins(self) -> None:
        record = ImageRecord(id=3, disk_id=9, name="x.st", format="st")
        catalogue = RecordingCatalogue({("sha512", "file-sha512"): record})
        self.assertEqual(match_entry(catalogue, self.entry()), record)
        self.assertEqual(len(catalogue.calls), 4)

    def test_undecoded_images_use_file_hashes_with_file_size(self) -> None:
        entry = LibraryEntry(path="/p.stx", size=500, md5="m", sha1="s", crc32="c", sha512="x")
        catalogue = RecordingCatalogue({})
        match_entry(catalogue, entry)
        self.assertEqual(
            catalogue.calls,
            [{"md5": "m"}, {"sha1": "s"}, {"crc32": "c", "size": 500}, {"sha512": "x"}],
        )


class DisplayNameTests(unittest.TestCase):
    def test_tosec_names_are_tidied(self) -> None:
        name, parsed = display_name(
            "/nas/Chaos Engine, The (1993)(Renegade)(Disk 1 of 2)[cr Cynix][a].st"
        )
        self.assertEqual(name, "The Chaos Engine (Disk 1 of 2) [cr Cynix]")
        self.assertEqual(parsed["publisher"], "Renegade")

    def test_other_names_use_the_file_name(self) -> None:
        self.assertEqual(display_name("/nas/menu_disk_12.msa"), ("menu disk 12", {}))
        self.assertEqual(display_name("/nas/set.zip", "inner.zip::Menu 3.st")[0], "Menu 3")


class LibraryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.folder = Path(tempfile.mkdtemp(prefix="pf-library-"))
        self.addCleanup(shutil.rmtree, self.folder, True)
        self.files = self.folder / "files"
        self.files.mkdir()
        self.raw_a = make_st_image("disk a", files=("ALPHA.PRG",))
        self.raw_b = make_st_image("disk b")
        self.raw_c = make_st_image("disk c")
        builder = CatalogueBuilder(self.folder / "catalogue.sqlite")
        builder.disk(1, "Crew 1", series=("crew", "Crew"), number=1, contents=["Alpha"])
        builder.disk(2, "Crew 2", series=("crew", "Crew"), number=2, contents=["Beta"])
        builder.disk(3, "Crew 3", series=("crew", "Crew"), number=3, contents=["Gamma"])
        builder.disk(4, "Crew 4", series=("crew", "Crew"), number=4)
        builder.image(10, 1, "Crew 1 (1990)(Crew).st", data=self.raw_a, hash_kinds=("md5",))
        # Atari Legend style: only the SHA-512 of the MSA file as stored.
        self.msa_b = make_msa(self.raw_b)
        builder.image(20, 2, "crew2.msa", sha512=hashlib.sha512(self.msa_b).hexdigest())
        builder.image(30, 3, "Crew 3.st", data=self.raw_c, hash_kinds=("crc32",))
        builder.location(1, 3, "provider-x", "https://example.invalid/c.st")
        builder.location(2, 4, "provider-y", "https://example.invalid/d.st")
        self.catalogue = SqlCatalogue(builder.close())
        self.addCleanup(self.catalogue.close)
        self.db = UserDatabase.open(self.folder / "user.sqlite")
        self.addCleanup(self.db.close)
        self.library = Library(self.db, self.catalogue, archives=archives)

    def test_scan_matches_raw_hashes_file_sha512_and_crc_with_size(self) -> None:
        (self.files / "a.msa").write_bytes(make_msa(self.raw_a))  # MSA of a TOSEC .st
        (self.files / "b.msa").write_bytes(self.msa_b)
        (self.files / "c.st").write_bytes(self.raw_c)
        (self.files / "unknown.st").write_bytes(make_st_image("nobody"))
        summary = self.library.scan([self.files])
        self.assertEqual((summary.matched, summary.unmatched), (3, 1))
        self.assertEqual(
            [f.path for f in self.library.files_for_disk(1)], [str(self.files / "a.msa")]
        )
        self.assertEqual(self.library.files_for_disk(2)[0].image_id, 20)
        self.assertEqual(self.library.files_for_image(30)[0].disk_id, 3)
        local = self.library.files_for_disk(1)[0]
        self.assertEqual(local.md5, hashes(self.raw_a)["md5"], "LocalFile hashes are raw")

    def test_availability(self) -> None:
        (self.files / "a.st").write_bytes(self.raw_a)
        self.library.scan([self.files])
        states = self.library.availability([1, 2, 3, 4], ["provider-x"])
        self.assertEqual(
            states,
            {
                1: Availability.LOCAL,
                2: Availability.MISSING,
                3: Availability.ONLINE,
                4: Availability.MISSING,
            },
        )
        self.assertEqual(self.library.availability([4], [])[4], Availability.MISSING)
        self.assertEqual(self.library.availability([4], ["provider-y"])[4], Availability.ONLINE)

    def test_search_unmatched_and_stats(self) -> None:
        (self.files / "a.st").write_bytes(self.raw_a)
        (self.files / "a copy.st").write_bytes(self.raw_a)
        (self.files / "Mystery.st").write_bytes(
            make_st_image("m", label="SECRET", files=("ZORK.PRG",))
        )
        self.library.scan([self.files])
        self.assertEqual(
            [f.display_name for f in self.library.search_unmatched("zork")], ["Mystery"]
        )
        self.assertEqual(
            [f.display_name for f in self.library.search_unmatched("secret")], ["Mystery"]
        )
        self.assertEqual(self.library.search_unmatched("alpha"), [])
        self.assertEqual(
            self.library.stats(),
            {
                "images": 3,
                "matched": 2,
                "unmatched": 1,
                "duplicates": 1,
                "folders": 1,
                "infected": 0,
            },
        )

    def test_add_file_indexes_at_once(self) -> None:
        path = self.files / "downloaded.st"
        path.write_bytes(self.raw_c)
        added = self.library.add_file(path)
        self.assertEqual([(f.disk_id, f.image_id) for f in added], [(3, 30)])
        self.assertEqual(self.library.availability([3], [])[3], Availability.LOCAL)
        summary = self.library.scan([self.files])
        self.assertEqual(summary.new, 0, "a later scan sees the file as unchanged")

    def test_a_download_no_checksum_could_check_stays_with_its_disc(self) -> None:
        # Crew 4 has a download and no dump, as the Vectronix disks on the CD.
        path = self.files / "crew4.msa"
        path.write_bytes(make_msa(make_st_image("disk d")))
        [added] = self.library.add_download(path, 4, "https://example.invalid/d.st")
        self.assertEqual((added.disk_id, added.image_id), (4, None))
        self.assertEqual(self.library.availability([4], [])[4], Availability.LOCAL)
        self.assertEqual(self.library.stats()["unmatched"], 0)
        [kept] = self.db.file_discs()
        self.assertEqual(
            (kept.reason, kept.disc.series_id, kept.disc.number), ("downloaded", "crew", 4)
        )
        # A later scan and a rematch keep it there.
        self.library.scan([self.files])
        self.assertEqual(self.library.rematch(), 1)
        self.assertEqual(self.library.files_for_disk(4), [added])
        # A catalogue update that numbers the disc 9 finds it again.
        builder = CatalogueBuilder(self.folder / "newer.sqlite", built_at="2026-10-01")
        builder.disk(9, "Crew 4", series=("crew", "Crew"), number=4)
        newer = SqlCatalogue(builder.close())
        self.addCleanup(newer.close)
        self.library.set_catalogue(newer)
        self.assertEqual(self.library.rematch(), 1)
        self.assertEqual([local.disk_id for local in self.library.files_for_disk(9)], [9])

    def test_a_download_that_changes_is_let_go(self) -> None:
        path = self.files / "crew4.st"
        path.write_bytes(make_st_image("disk d"))
        self.library.add_download(path, 4)
        path.write_bytes(make_st_image("something else"))
        self.library.scan([self.files])
        self.assertEqual(self.library.files_for_disk(4), [])

    def test_a_download_that_matches_a_dump_is_matched_as_usual(self) -> None:
        path = self.files / "crew3.st"
        path.write_bytes(self.raw_c)
        [added] = self.library.add_download(path, 3)
        self.assertEqual((added.disk_id, added.image_id), (3, 30))
        self.assertEqual(self.db.file_discs(), [])

    def test_an_unmatched_file_linked_to_a_disc_stays_with_it(self) -> None:
        # A menu disk downloaded by hand: no dump of Crew 4 is known.
        path = self.files / "crew4.msa"
        path.write_bytes(make_msa(make_st_image("by hand")))
        self.library.scan([self.files])
        [local] = self.library.search_unmatched("")
        linked = self.library.link_file(local, 4)
        self.assertEqual((linked.disk_id, linked.image_id), (4, None))
        self.assertEqual(self.library.search_unmatched(""), [])
        self.assertEqual(self.library.availability([4], [])[4], Availability.LOCAL)
        [kept] = self.db.file_discs()
        self.assertEqual((kept.reason, kept.disc.number, kept.member), ("linked", 4, ""))
        # A scan and a catalogue update that numbers the disc 9 keep it there.
        self.library.scan([self.files])
        builder = CatalogueBuilder(self.folder / "newer.sqlite", built_at="2026-10-01")
        builder.disk(9, "Crew 4", series=("crew", "Crew"), number=4)
        newer = SqlCatalogue(builder.close())
        self.addCleanup(newer.close)
        self.library.set_catalogue(newer)
        self.assertEqual(self.library.rematch(), 1)
        self.assertEqual([f.disk_id for f in self.library.files_for_disk(9)], [9])

    def test_an_image_inside_an_archive_can_be_linked(self) -> None:
        inner = make_msa(make_st_image("zipped by hand"))
        path = self.files / "crew4.zip"
        path.write_bytes(zip_bytes({"MENU4.MSA": inner, "readme.txt": b"from the forum"}))
        self.library.scan([self.files])
        [local] = self.library.search_unmatched("")
        self.assertEqual(local.member, "MENU4.MSA")
        self.assertEqual(self.library.link_file(local, 4).disk_id, 4)
        self.library.scan([self.files])
        self.assertEqual(self.library.rematch(), 1)
        self.assertEqual(
            [(f.path, f.member) for f in self.library.files_for_disk(4)],
            [(str(path), "MENU4.MSA")],
        )

    def test_unlinking_makes_the_file_unmatched_again(self) -> None:
        path = self.files / "crew4.st"
        path.write_bytes(make_st_image("by hand"))
        self.library.scan([self.files])
        [local] = self.library.search_unmatched("")
        linked = self.library.link_file(local, 4)
        unlinked = self.library.unlink_file(linked)
        self.assertEqual(unlinked.disk_id, None)
        self.assertEqual(self.db.file_discs(), [])
        self.assertEqual(self.library.rematch(), 0)
        self.assertEqual(len(self.library.search_unmatched("")), 1)

    def test_a_file_that_matches_a_dump_is_not_linked(self) -> None:
        path = self.files / "c.st"
        path.write_bytes(self.raw_c)
        [local] = self.library.add_file(path)
        with self.assertRaisesRegex(LinkError, "matches a dump in the catalogue"):
            self.library.link_file(local, 4)
        with self.assertRaisesRegex(LinkError, "not in the catalogue"):
            self.library.link_file(
                self.library.add_file(self._write("x.st", make_st_image("x")))[0], 99
            )
        with self.assertRaisesRegex(LinkError, "not in the library index"):
            self.library.link_file(LocalFile(path=str(self.files / "gone.st")), 4)

    def _write(self, name: str, data: bytes) -> Path:
        path = self.files / name
        path.write_bytes(data)
        return path

    def test_rematch_after_a_catalogue_update(self) -> None:
        mystery = make_st_image("new in the update")
        (self.files / "later.st").write_bytes(mystery)
        self.library.scan([self.files])
        self.assertEqual(self.library.stats()["matched"], 0)
        builder = CatalogueBuilder(self.folder / "newer.sqlite", built_at="2026-10-01")
        builder.disk(7, "Crew 7")
        builder.image(70, 7, "Crew 7.st", data=mystery, hash_kinds=("sha1",))
        newer = SqlCatalogue(builder.close())
        self.addCleanup(newer.close)
        self.library.set_catalogue(newer)
        self.assertEqual(self.library.rematch(), 1)
        self.assertEqual(self.library.files_for_disk(7)[0].image_id, 70)

    def test_last_scan(self) -> None:
        self.assertEqual(self.library.last_scan(), "")
        (self.files / "a.st").write_bytes(self.raw_a)
        self.library.scan([self.files])
        self.assertRegex(self.library.last_scan(), r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}$")

    def test_forget_folder(self) -> None:
        (self.files / "a.st").write_bytes(self.raw_a)
        self.library.scan([self.files])
        self.assertEqual(self.library.forget_folder(self.files), 1)
        self.assertEqual(self.library.stats()["images"], 0)

    def test_forget_outside_keeps_listed_folders_only(self) -> None:
        kept = self.folder / "kept"
        # A sibling whose name starts with the kept folder's must not survive.
        removed = self.folder / "kept-old"
        kept.mkdir()
        removed.mkdir()
        (kept / "a.st").write_bytes(self.raw_a)
        (removed / "c.st").write_bytes(self.raw_c)
        self.library.scan([kept, removed])
        self.assertEqual(self.library.forget_outside([kept]), 1)
        self.assertEqual([f.path for f in self.library.files_for_disk(1)], [str(kept / "a.st")])
        self.assertEqual(self.library.files_for_disk(3), [])

    def test_read_bytes_reads_archive_members(self) -> None:
        import zipfile

        with zipfile.ZipFile(self.files / "set.zip", "w") as archive:
            archive.writestr("Crew 1.st", self.raw_a)
        self.library.scan([self.files])
        local = self.library.files_for_disk(1)[0]
        self.assertEqual(local.member, "Crew 1.st")
        self.assertEqual(self.library.read_bytes(local), self.raw_a)


@unittest.skipUnless(store_module_available(), "catalogue.store is not written yet")
class RealCatalogueTests(unittest.TestCase):
    def test_scan_matches_with_the_real_store(self) -> None:
        from piratefinder.catalogue.store import Catalogue

        folder = Path(tempfile.mkdtemp(prefix="pf-library-store-"))
        self.addCleanup(shutil.rmtree, folder, True)
        raw = make_st_image("real store")
        builder = CatalogueBuilder(folder / "catalogue.sqlite")
        builder.disk(1, "Crew 1", contents=["Alpha"])
        builder.image(10, 1, "Crew 1.st", data=raw, hash_kinds=("md5", "sha1", "crc32"))
        catalogue = Catalogue.open(builder.close())
        files = folder / "files"
        files.mkdir()
        (files / "one.msa").write_bytes(make_msa(raw))
        db = UserDatabase.open(folder / "user.sqlite")
        self.addCleanup(db.close)
        library = Library(db, catalogue)
        summary = library.scan([files])
        self.assertEqual(summary.errors, ())
        self.assertEqual(summary.matched, 1)
        self.assertEqual(library.files_for_disk(1)[0].image_id, 10)


if __name__ == "__main__":
    unittest.main()

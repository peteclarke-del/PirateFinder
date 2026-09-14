"""Boot block viruses in the library: found on scan, removed from files on request.

Every image is synthetic. The Amiga "virus" is a seeded boot block named by
the test brainfile; the ST one a seeded body with a signature written by the
test (see ``test_images_virus``).
"""

from __future__ import annotations

import gzip
import io
import os
import shutil
import sqlite3
import tempfile
import unittest
import zipfile
from dataclasses import replace
from pathlib import Path
from unittest import mock

from piratefinder.images import virus
from piratefinder.images.vendor.msa import parse_msa, st_to_msa
from piratefinder.jobs.cancellation import Cancellation
from piratefinder.library import library as library_module
from piratefinder.library.library import Library
from piratefinder.library.userdb import MIGRATIONS, SCHEMA_VERSION, UserDatabase
from piratefinder.models import BootRecheck, LocalFile, Platform
from piratefinder.ui import formatting as fmt
from tests.test_images_synthetic import dms_archive
from tests.test_images_virus import (
    VirusEnvironment,
    amiga_disk,
    crc_virus,
    st_body,
    st_infected,
    st_signature_toml,
)
from tests.test_library_helpers import CatalogueBuilder, SqlCatalogue, make_st_image


class CleanFileTests(VirusEnvironment, unittest.TestCase):
    def setUp(self) -> None:
        self.folder = self.use_environment(brainfile=True)
        self.body = st_body()
        self.use_st_signatures(st_signature_toml("Synthetic Ghost", self.body))
        self.files = self.folder / "files"
        self.files.mkdir()
        self.infected = amiga_disk(crc_virus())
        self.cleaned = virus.clean(self.infected, Platform.AMIGA)
        self.st_infected = st_infected(make_st_image("clean file test"), self.body)
        builder = CatalogueBuilder(self.folder / "catalogue.sqlite")
        builder.disk(1, "Crew 1", platform=Platform.AMIGA, series=("crew", "Crew"), number=1)
        builder.disk(2, "Crew 2", platform=Platform.AMIGA, series=("crew", "Crew"), number=2)
        builder.disk(3, "Menu 3", series=("menu", "Menu"), number=3)
        # Disk 1: an infected dump and the clean dump it was made from.
        builder.image(10, 1, "Crew 1 [v Synthetic CRC].adf", data=self.infected, rank=1)
        builder.image(11, 1, "Crew 1.adf", data=self.cleaned, rank=0)
        # Disk 2: only the infected dump is catalogued.
        self.other = amiga_disk(crc_virus(), seed=8)
        builder.image(20, 2, "Crew 2.adf", data=self.other)
        builder.image(30, 3, "Menu 3.st", data=self.st_infected)
        self.catalogue = SqlCatalogue(builder.close())
        self.addCleanup(self.catalogue.close)
        self.db = UserDatabase.open(self.folder / "user.sqlite")
        self.addCleanup(self.db.close)
        self.library = Library(self.db, self.catalogue)

    def scan(self) -> None:
        summary = self.library.scan([self.files])
        self.assertEqual(summary.errors, ())

    def only(self, disk_id: int):
        files = self.library.files_for_disk(disk_id)
        self.assertEqual(len(files), 1, files)
        return files[0]

    def test_a_scan_records_the_virus(self) -> None:
        (self.files / "crew1.adf").write_bytes(self.infected)
        (self.files / "clean.st").write_bytes(make_st_image("plain"))
        self.scan()
        local = self.only(1)
        self.assertEqual(local.virus, "Synthetic CRC")
        self.assertEqual(self.db.entries(disk_id=1)[0].boot_status, "virus")
        plain = self.library.search_unmatched("")
        self.assertEqual(
            [(f.virus, self.db.entries(path=f.path)[0].boot_status) for f in plain], [("", "clean")]
        )
        self.assertEqual(self.library.stats()["infected"], 1)
        self.assertEqual(self.library.infected_files(), [local])

    def test_a_plain_adf_is_rewritten_with_a_backup(self) -> None:
        path = self.files / "crew1.adf"
        path.write_bytes(self.infected)
        path.chmod(0o640)
        self.scan()
        cleaned = self.library.clean_file(self.only(1))
        self.assertEqual(cleaned.path, str(path))
        self.assertEqual(path.read_bytes(), self.cleaned)
        self.assertEqual((self.files / "crew1.adf.bak").read_bytes(), self.infected)
        self.assertEqual(oct(path.stat().st_mode & 0o777), oct(0o640))
        # The cleaned file is the clean dump of the same disk.
        self.assertEqual((cleaned.disk_id, cleaned.image_id, cleaned.virus), (1, 11, ""))
        self.assertEqual(self.library.files_for_disk(1), [cleaned])
        self.scan()
        self.assertEqual(self.library.files_for_disk(1), [cleaned], "backups are not indexed")

    def test_an_existing_backup_is_never_replaced(self) -> None:
        path = self.files / "crew1.adf"
        path.write_bytes(self.infected)
        (self.files / "crew1.adf.bak").write_bytes(b"an older backup")
        (self.files / "crew1.adf.2.bak").write_bytes(b"another")
        self.scan()
        self.library.clean_file(self.only(1))
        self.assertEqual((self.files / "crew1.adf.bak").read_bytes(), b"an older backup")
        self.assertEqual((self.files / "crew1.adf.2.bak").read_bytes(), b"another")
        self.assertEqual((self.files / "crew1.adf.3.bak").read_bytes(), self.infected)

    def test_a_cleaned_file_stays_with_its_disk(self) -> None:
        path = self.files / "crew2.adf"
        path.write_bytes(self.other)
        self.scan()
        cleaned = self.library.clean_file(self.only(2))
        self.assertEqual((cleaned.disk_id, cleaned.image_id), (2, None))
        self.assertEqual(self.library.rematch(), 1)
        self.assertEqual(self.only(2).path, str(path))
        # Another image saved under the same name is not taken for the cleaned copy.
        path.write_bytes(amiga_disk(virus.standard_boot_block(0), seed=99))
        os.utime(path, (1, 1))
        self.scan()
        self.assertEqual(self.library.files_for_disk(2), [])
        self.assertEqual(self.library.rematch(), 0)

    def test_a_cleaned_file_stays_with_its_disk_in_a_catalogue_update(self) -> None:
        # A new build numbers its discs afresh: Crew 2 is disc 7 in it.
        (self.files / "crew2.adf").write_bytes(self.other)
        self.scan()
        cleaned = self.library.clean_file(self.only(2))
        [kept] = self.db.file_discs()
        self.assertEqual(
            (kept.reason, kept.disc.series_id, kept.disc.number), ("cleaned", "crew", 2)
        )
        builder = CatalogueBuilder(self.folder / "newer.sqlite", built_at="2026-10-01")
        builder.disk(7, "Crew 2", platform=Platform.AMIGA, series=("crew", "Crew"), number=2)
        builder.disk(8, "Crew 1", platform=Platform.AMIGA, series=("crew", "Crew"), number=1)
        newer = SqlCatalogue(builder.close())
        self.addCleanup(newer.close)
        self.library.set_catalogue(newer)
        self.assertEqual(self.library.rematch(), 1)
        self.assertEqual(self.library.files_for_disk(7), [replace(cleaned, disk_id=7)])
        self.assertEqual(self.library.files_for_disk(2), [])

    def test_a_forgotten_file_forgets_its_origin(self) -> None:
        (self.files / "crew2.adf").write_bytes(self.other)
        self.scan()
        self.library.clean_file(self.only(2))
        self.assertEqual(len(self.db.file_discs()), 1)
        self.library.forget_folder(self.files)
        self.assertEqual(self.db.file_discs(), [])

    def test_an_msa_is_packed_again(self) -> None:
        path = self.files / "menu3.msa"
        path.write_bytes(st_to_msa(self.st_infected))
        self.scan()
        local = self.only(3)
        self.assertEqual(local.virus, "Synthetic Ghost")
        cleaned = self.library.clean_file(local)
        self.assertEqual(cleaned.path, str(path))
        expected = virus.clean(self.st_infected, Platform.ATARI_ST)
        self.assertEqual(parse_msa(path.read_bytes()).sectors(), expected)
        self.assertEqual(
            parse_msa((self.files / "menu3.msa.bak").read_bytes()).sectors(), self.st_infected
        )
        self.assertEqual((cleaned.format, cleaned.disk_id, cleaned.virus), ("msa", 3, ""))

    def test_dms_and_adz_get_a_cleaned_adf_beside_them(self) -> None:
        dms = self.files / "crew1.dms"
        dms.write_bytes(dms_archive(self.infected))
        adz = self.files / "copy.adz"
        adz.write_bytes(gzip.compress(self.infected))
        before = {path.name: path.read_bytes() for path in (dms, adz)}
        self.scan()
        for local in self.library.files_for_disk(1):
            self.library.clean_file(local)
        self.assertEqual((self.files / "crew1 (cleaned).adf").read_bytes(), self.cleaned)
        self.assertEqual((self.files / "copy (cleaned).adf").read_bytes(), self.cleaned)
        self.assertEqual({path.name: path.read_bytes() for path in (dms, adz)}, before)
        self.assertEqual(sorted(p.name for p in self.files.glob("*.bak")), [])
        clean = [f for f in self.library.files_for_disk(1) if not f.virus]
        self.assertEqual(len(clean), 2)

    def test_a_name_that_is_taken_gets_a_number(self) -> None:
        (self.files / "crew1.dms").write_bytes(dms_archive(self.infected))
        (self.files / "crew1 (cleaned).adf").write_bytes(b"someone else's file")
        self.scan()
        infected = next(f for f in self.library.files_for_disk(1) if f.virus)
        cleaned = self.library.clean_file(infected)
        self.assertEqual(Path(cleaned.path).name, "crew1 (cleaned) (2).adf")
        self.assertEqual((self.files / "crew1 (cleaned).adf").read_bytes(), b"someone else's file")

    def test_an_archive_member_is_saved_into_the_download_folder(self) -> None:
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("set/crew2.adf", self.other)
        zipped = self.files / "set.zip"
        zipped.write_bytes(buffer.getvalue())
        self.scan()
        local = self.only(2)
        self.assertEqual(local.member, "set/crew2.adf")
        with self.assertRaises(virus.VirusError):
            self.library.clean_file(local)
        downloads = self.folder / "downloads"
        cleaned = self.library.clean_file(
            local, download_folder=downloads, folders=("Games", "Crew")
        )
        target = downloads / "Amiga" / "Games" / "Crew" / "crew2 (cleaned).adf"
        self.assertEqual(cleaned.path, str(target))
        self.assertEqual(target.read_bytes(), virus.clean(self.other, Platform.AMIGA))
        self.assertEqual(zipped.read_bytes(), buffer.getvalue())
        self.assertEqual(
            {f.path for f in self.library.files_for_disk(2)}, {str(zipped), str(target)}
        )

    def test_nothing_is_written_without_a_removable_virus(self) -> None:
        clean = amiga_disk(virus.standard_boot_block(0), seed=3)
        (self.files / "clean.adf").write_bytes(clean)
        (self.files / "noroot.adf").write_bytes(amiga_disk(crc_virus(), root=False, seed=4))
        self.scan()
        for local in self.library.search_unmatched(""):
            with self.subTest(path=local.path), self.assertRaises(virus.VirusError):
                self.library.clean_file(local)
        self.assertEqual(sorted(p.name for p in self.files.iterdir()), ["clean.adf", "noroot.adf"])

    def test_the_confirmation_names_the_files_cleaning_writes(self) -> None:
        (self.files / "crew1.adf").write_bytes(self.infected)
        (self.files / "crew1.dms").write_bytes(dms_archive(self.infected))
        self.scan()
        gzipped = self.files / "menu3.st.gz"
        gzipped.write_bytes(gzip.compress(self.st_infected))
        files = [
            *self.library.files_for_disk(1),
            LocalFile(path=str(gzipped), format="gz", disk_id=3),
        ]
        expected = {"crew1.adf": "crew1.adf.bak", "crew1.dms": "crew1 (cleaned).adf"}
        expected["menu3.st.gz"] = "menu3 (cleaned).st"
        platforms = (Platform.AMIGA, Platform.AMIGA, Platform.ATARI_ST)
        for local, platform in zip(files, platforms, strict=True):
            with self.subTest(path=local.path):
                text = fmt.clean_explanation(local, "Synthetic", platform)
                before = set(self.files.iterdir())
                self.library.clean_file(local)
                written = {path.name for path in set(self.files.iterdir()) - before}
                self.assertEqual(written, {expected[Path(local.path).name]})
                self.assertIn(f" {written.pop()}", text)

    def test_one_list_says_which_formats_are_rewritten_in_place(self) -> None:
        path = self.files / "crew1.adf"
        path.write_bytes(self.infected)
        self.scan()
        local = self.only(1)
        with mock.patch.object(library_module, "REWRITTEN_FORMATS", frozenset({"st", "msa"})):
            text = fmt.clean_explanation(local, "Synthetic CRC")
            cleaned = self.library.clean_file(local)
        self.assertEqual(Path(cleaned.path).name, "crew1 (cleaned).adf")
        self.assertIn("beside crew1.adf as crew1 (cleaned).adf", text)
        self.assertEqual(path.read_bytes(), self.infected)
        self.assertEqual(list(self.files.glob("*.bak")), [])


class BootRecheckTests(VirusEnvironment, unittest.TestCase):
    """Boot blocks checked again when the virus data changes (``Library.recheck_boot_blocks``)."""

    def setUp(self) -> None:
        self.folder = self.use_environment()
        self.files = self.folder / "files"
        self.files.mkdir()
        self.body = st_body()
        infected = st_infected(make_st_image("recheck plain"), self.body)
        (self.files / "plain.st").write_bytes(infected)
        packed = parse_msa(st_to_msa(st_infected(make_st_image("recheck msa"), self.body)))
        (self.files / "packed.msa").write_bytes(st_to_msa(packed.sectors()))
        with zipfile.ZipFile(self.files / "archive.zip", "w") as archive:
            archive.writestr("inner.st", st_infected(make_st_image("recheck zip"), self.body))
        (self.files / "clean.st").write_bytes(make_st_image("recheck clean"))
        (self.files / "crew1.adf").write_bytes(amiga_disk(crc_virus()))
        self.db = UserDatabase.open(self.folder / "user.sqlite")
        self.addCleanup(self.db.close)
        self.library = Library(self.db, None)
        self.library.scan([self.files])

    def statuses(self) -> dict[str, tuple[str, str]]:
        return {
            Path(entry.path).name + (f":{entry.member}" if entry.member else ""): (
                entry.boot_status,
                entry.boot_name,
            )
            for entry in self.db.entries()
        }

    def test_new_built_in_signatures_find_viruses_in_files_scanned_before(self) -> None:
        before = self.statuses()
        self.assertEqual(before["plain.st"], ("unknown-boot", ""))
        self.assertIsNone(self.library.recheck_boot_blocks(), "the scan checked with this data")
        self.use_st_signatures(st_signature_toml("Synthetic Ghost", self.body))
        progress: list[tuple[str, int, int]] = []
        result = self.library.recheck_boot_blocks(lambda *step: progress.append(step))
        self.assertEqual(result, BootRecheck(checked=5, changed=3))
        after = self.statuses()
        for name in ("plain.st", "packed.msa", "archive.zip:inner.st"):
            self.assertEqual(after[name], ("virus", "Synthetic Ghost"), name)
        self.assertEqual(after["clean.st"], before["clean.st"])
        self.assertEqual(after["crew1.adf"], before["crew1.adf"])
        self.assertEqual([step[1:] for step in progress], [(n, 5) for n in range(1, 6)])
        self.assertEqual(self.library.infected_files()[0].virus, "Synthetic Ghost")
        self.assertIsNone(self.library.recheck_boot_blocks(), "the new fingerprint is kept")

    def test_only_the_boot_block_is_read_and_nothing_is_hashed_again(self) -> None:
        hashes = {entry.path + entry.member: entry.raw_sha1 for entry in self.db.entries()}
        self.use_st_signatures(st_signature_toml("Synthetic Ghost", self.body))
        real_open = open
        sizes: list[int] = []

        class Counting:
            def __init__(self, handle) -> None:
                self.handle = handle

            def __enter__(self):
                return self

            def __exit__(self, *details) -> None:
                self.handle.close()

            def read(self, size: int = -1) -> bytes:
                sizes.append(size)
                return self.handle.read(size)

        def counting_open(path, mode="r", *arguments, **keywords):
            return Counting(real_open(path, mode, *arguments, **keywords))

        reads = mock.Mock(wraps=library_module.read_image_bytes)
        with (
            mock.patch("builtins.open", counting_open),
            mock.patch.object(library_module, "read_image_bytes", reads),
            mock.patch("piratefinder.images.inspect.Hashes.of", side_effect=AssertionError),
        ):
            result = self.library.recheck_boot_blocks()
        self.assertEqual(result.changed, 3)
        # The plain ADF and ST files are read as their first kilobyte; the MSA
        # and the archive member are unpacked, as a scan would unpack them.
        self.assertEqual(sizes, [1024, 1024, 1024])
        self.assertEqual(
            sorted(Path(call.args[0]).name for call in reads.call_args_list),
            ["archive.zip", "packed.msa"],
        )
        self.assertEqual(
            {entry.path + entry.member: entry.raw_sha1 for entry in self.db.entries()}, hashes
        )

    def test_installing_the_brainfile_checks_the_boot_blocks_again(self) -> None:
        self.assertEqual(self.statuses()["crew1.adf"], ("unknown-boot", ""))
        from tests.test_images_virus import install_fixture_brainfile

        install_fixture_brainfile()
        result = self.library.recheck_boot_blocks()
        self.assertEqual((result.checked, result.changed), (5, 1))
        self.assertEqual(self.statuses()["crew1.adf"], ("virus", "Synthetic CRC"))

    def test_a_scan_checks_unchanged_files_again_first(self) -> None:
        self.use_st_signatures(st_signature_toml("Synthetic Ghost", self.body))
        summary = self.library.scan([self.files])
        self.assertEqual(summary.new, 0, "no file changed, so the scan read none")
        self.assertEqual(self.statuses()["plain.st"], ("virus", "Synthetic Ghost"))

    def test_a_file_that_cannot_be_read_is_left_to_the_next_scan(self) -> None:
        self.use_st_signatures(st_signature_toml("Synthetic Ghost", self.body))
        gone = self.files / "plain.st"
        moved = self.folder / "plain.st"
        gone.rename(moved)
        result = self.library.recheck_boot_blocks()
        self.assertEqual((result.checked, result.changed, result.unreadable), (4, 2, 1))
        self.assertEqual(self.statuses()["plain.st"], ("unknown-boot", ""))
        self.assertEqual(self.db.scanned_files()[str(gone)][1], -1, "read in full next time")
        self.assertIsNone(self.library.recheck_boot_blocks())
        moved.rename(gone)
        self.library.scan([self.files])
        self.assertEqual(self.statuses()["plain.st"], ("virus", "Synthetic Ghost"))

    def test_a_cancelled_check_is_done_again_next_time(self) -> None:
        self.use_st_signatures(st_signature_toml("Synthetic Ghost", self.body))
        cancel = Cancellation()
        cancel.cancel()
        self.assertEqual(
            self.library.recheck_boot_blocks(cancel=cancel), BootRecheck(cancelled=True)
        )
        self.assertEqual(self.library.recheck_boot_blocks().changed, 3)


class MigrationTests(unittest.TestCase):
    def test_a_version_4_database_has_its_boot_blocks_checked_again(self) -> None:
        folder = Path(tempfile.mkdtemp(prefix="pf-migrate-"))
        self.addCleanup(shutil.rmtree, folder, True)
        path = folder / "user.sqlite"
        connection = sqlite3.connect(path)
        for script in MIGRATIONS[:4]:
            connection.executescript(script)
        connection.execute("PRAGMA user_version = 4")
        connection.commit()
        connection.close()
        db = UserDatabase.open(path)
        self.addCleanup(db.close)
        self.assertEqual(db.meta(library_module.BOOT_FINGERPRINT), "")
        self.assertIsNotNone(Library(db, None).recheck_boot_blocks())
        self.assertEqual(db.meta(library_module.BOOT_FINGERPRINT), virus.fingerprint())

    def test_cleaned_copies_recorded_by_disk_id_take_the_disc_of_the_catalogue_in_use(
        self,
    ) -> None:
        folder = Path(tempfile.mkdtemp(prefix="pf-migrate-"))
        self.addCleanup(shutil.rmtree, folder, True)
        path = folder / "user.sqlite"
        connection = sqlite3.connect(path)
        for script in MIGRATIONS[:5]:
            connection.executescript(script)
        connection.execute("PRAGMA user_version = 5")
        connection.execute(
            "INSERT INTO cleaned_files(path, sha1, disk_id, source_path, virus) "
            "VALUES ('/nas/crew1 (cleaned).adf', 'ab12', 1, '/nas/crew1.adf', 'Lamer')"
        )
        connection.commit()
        connection.close()
        db = UserDatabase.open(path)
        self.addCleanup(db.close)
        [kept] = db.file_discs()
        self.assertEqual(
            (kept.path, kept.sha1, kept.reason), ("/nas/crew1 (cleaned).adf", "ab12", "cleaned")
        )
        self.assertEqual(
            (kept.disc.disk_id, kept.disc.catalogue, kept.disc.series_id), (1, "", None)
        )
        builder = CatalogueBuilder(folder / "catalogue.sqlite")
        builder.disk(1, "Crew 1", platform=Platform.AMIGA, series=("crew", "Crew"), number=1)
        catalogue = SqlCatalogue(builder.close())
        self.addCleanup(catalogue.close)
        stamp = Library(db, catalogue).relink_files(catalogue)
        [kept] = db.file_discs()
        self.assertEqual(
            (kept.disc.series_id, kept.disc.number, kept.disc.catalogue), ("crew", 1, stamp)
        )
        self.assertEqual(db.file_disc_origins(stamp), {"/nas/crew1 (cleaned).adf": ("ab12", 1)})

    def test_an_old_database_gains_boot_columns_and_is_scanned_again(self) -> None:
        folder = Path(tempfile.mkdtemp(prefix="pf-migrate-"))
        self.addCleanup(shutil.rmtree, folder, True)
        path = folder / "user.sqlite"
        connection = sqlite3.connect(path)
        connection.executescript(MIGRATIONS[0])
        connection.execute("PRAGMA user_version = 1")
        connection.execute(
            "INSERT INTO scanned_files(path, size, mtime) VALUES ('/nas/a.adf', 901120, 1234.5)"
        )
        connection.execute(
            "INSERT INTO library_files(path, size, format) VALUES ('/nas/a.adf', 901120, 'adf')"
        )
        connection.commit()
        connection.close()
        db = UserDatabase.open(path)
        self.addCleanup(db.close)
        version = db.connection().execute("PRAGMA user_version").fetchone()[0]
        self.assertEqual(version, SCHEMA_VERSION)
        self.assertEqual(db.scanned_files()["/nas/a.adf"][1], -1)
        entry = db.entries()[0]
        self.assertEqual((entry.boot_status, entry.boot_name), ("", ""))
        self.assertEqual(entry.to_local().virus, "")


if __name__ == "__main__":
    unittest.main()

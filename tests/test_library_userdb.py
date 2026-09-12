from __future__ import annotations

import shutil
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path

from piratefinder.library.userdb import (
    SCHEMA_VERSION,
    LibraryEntry,
    UserDatabase,
    UserDatabaseError,
    fts_query,
)


def entry(path: str, member: str = "", **values: object) -> LibraryEntry:
    return LibraryEntry(path=path, member=member, **values)  # type: ignore[arg-type]


class UserDatabaseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.folder = Path(tempfile.mkdtemp(prefix="pf-userdb-"))
        self.addCleanup(shutil.rmtree, self.folder, True)
        self.path = self.folder / "nested" / "user.sqlite"
        self.db = UserDatabase.open(self.path)
        self.addCleanup(self.db.close)

    def test_open_creates_folders_schema_and_wal(self) -> None:
        self.assertTrue(self.path.exists())
        connection = self.db.connection()
        self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], SCHEMA_VERSION)
        self.assertEqual(connection.execute("PRAGMA journal_mode").fetchone()[0], "wal")
        tables = {
            name
            for (name,) in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        for table in ("library_files", "scanned_files", "sessions", "session_items", "corrections"):
            self.assertIn(table, tables)

    def test_reopening_does_not_migrate_again(self) -> None:
        self.db.store_file("/a/x.st", 10, 1.0, [entry("/a/x.st", display_name="X")])
        self.db.close()
        again = UserDatabase.open(self.path)
        self.addCleanup(again.close)
        self.assertEqual(len(again.entries()), 1)

    def test_newer_database_is_refused(self) -> None:
        self.db.close()
        connection = sqlite3.connect(self.path)
        connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")
        connection.close()
        with self.assertRaises(UserDatabaseError):
            UserDatabase.open(self.path)

    def test_store_file_replaces_entries_and_counts_new_ones(self) -> None:
        first = [entry("/a/set.zip", "one.st"), entry("/a/set.zip", "two.st")]
        self.assertEqual(self.db.store_file("/a/set.zip", 100, 5.0, first), 2)
        second = [entry("/a/set.zip", "two.st"), entry("/a/set.zip", "three.st")]
        self.assertEqual(self.db.store_file("/a/set.zip", 120, 6.0, second), 1)
        members = [item.member for item in self.db.entries(path="/a/set.zip")]
        self.assertEqual(sorted(members), ["three.st", "two.st"])
        self.assertEqual(self.db.scanned_files()["/a/set.zip"], (120, 6.0, ""))

    def test_listing_and_parsed_round_trip(self) -> None:
        self.db.store_file(
            "/a/x.st",
            1,
            1.0,
            [entry("/a/x.st", listing=("A.PRG", "B/"), parsed={"title": "X"}, raw_size=737280)],
        )
        stored = self.db.entries()[0]
        self.assertEqual(stored.listing, ("A.PRG", "B/"))
        self.assertEqual(stored.parsed, {"title": "X"})
        self.assertEqual(stored.to_local().size, 737280)

    def test_unmatched_search_uses_name_label_and_listing(self) -> None:
        self.db.store_file(
            "/a/one.st",
            1,
            1.0,
            [entry("/a/one.st", display_name="Mystery Disk", volume_label="MENU12")],
        )
        self.db.store_file(
            "/a/two.st", 1, 1.0, [entry("/a/two.st", display_name="Other", listing=("SONIC.PRG",))]
        )
        self.db.store_file(
            "/a/three.st", 1, 1.0, [entry("/a/three.st", display_name="Mystery Match", disk_id=4)]
        )
        self.assertEqual([e.path for e in self.db.search_unmatched("myst")], ["/a/one.st"])
        self.assertEqual([e.path for e in self.db.search_unmatched("menu12")], ["/a/one.st"])
        self.assertEqual([e.path for e in self.db.search_unmatched("sonic")], ["/a/two.st"])
        self.assertEqual(len(self.db.search_unmatched("")), 2)
        self.assertEqual(self.db.search_unmatched('" AND OR ('), [])

    def test_fts_index_follows_updates_and_deletes(self) -> None:
        self.db.store_file("/a/one.st", 1, 1.0, [entry("/a/one.st", display_name="Zebra")])
        stored = self.db.entries()[0]
        self.db.set_matches([(stored.id, 1, 1)])
        self.assertEqual(self.db.search_unmatched("zebra"), [])
        self.db.set_matches([(stored.id, None, None)])
        self.assertEqual(len(self.db.search_unmatched("zebra")), 1)
        self.db.remove_files(["/a/one.st"])
        self.assertEqual(self.db.search_unmatched("zebra"), [])
        self.assertEqual(self.db.scanned_files(), {})

    def test_fts_query_quotes_every_word(self) -> None:
        self.assertEqual(fts_query("Pompey 51!"), '"pompey"* AND "51"*')
        self.assertEqual(fts_query("  "), "")

    def test_counts(self) -> None:
        self.db.store_file(
            "/a/x.st", 1, 1.0, [entry("/a/x.st", raw_md5="aa", md5="aa", disk_id=1, image_id=1)]
        )
        self.db.store_file(
            "/b/y.msa", 1, 1.0, [entry("/b/y.msa", raw_md5="aa", md5="bb", disk_id=1, image_id=1)]
        )
        self.db.store_file("/b/z.st", 1, 1.0, [entry("/b/z.st", raw_md5="cc", md5="cc")])
        self.assertEqual(
            self.db.library_counts(),
            {"images": 3, "matched": 2, "unmatched": 1, "duplicates": 1, "folders": 2},
        )
        self.assertEqual(self.db.counts_under("/b"), (2, 1))
        self.assertEqual(self.db.disks_present([1, 2]), {1})

    def test_corrections(self) -> None:
        self.db.set_correction(5, "label", "Automation 250 (fixed)")
        self.db.set_correction(5, "label", "Automation 250 B")
        self.db.set_correction(6, "notes", "Side B is blank")
        self.assertEqual(
            self.db.corrections([5, 6, 7]),
            {5: {"label": "Automation 250 B"}, 6: {"notes": "Side B is blank"}},
        )
        self.db.remove_correction(5, "label")
        self.assertEqual(self.db.corrections([5]), {})

    def test_threads_get_their_own_connections(self) -> None:
        errors: list[BaseException] = []
        connections: list[sqlite3.Connection] = []
        together = threading.Barrier(4)

        def writer(number: int) -> None:
            try:
                together.wait()
                connections.append(self.db.connection())
                for index in range(20):
                    path = f"/t{number}/f{index}.st"
                    self.db.store_file(path, index, 1.0, [entry(path, display_name=f"n{index}")])
                    self.db.library_counts()
                together.wait()
            except BaseException as error:  # collected for the assertion below
                errors.append(error)

        threads = [threading.Thread(target=writer, args=(number,)) for number in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])
        self.assertEqual(len({id(connection) for connection in connections}), 4)
        self.assertEqual(self.db.library_counts()["images"], 80)

    def test_closed_database_refuses_use(self) -> None:
        self.db.close()
        with self.assertRaises(UserDatabaseError):
            self.db.connection()


if __name__ == "__main__":
    unittest.main()

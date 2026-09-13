from __future__ import annotations

import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path

from piratefinder.jobs.history import History, report_text
from piratefinder.library.userdb import MIGRATIONS, UserDatabase
from piratefinder.models import SessionSummary, WriteOutcome, WriteStatus


def summary(started: str = "2026-09-12T14:03:10") -> SessionSummary:
    return SessionSummary(
        started=started,
        finished="2026-09-12T14:30:00",
        drive="A",
        items=(
            (
                "Crew 1",
                WriteOutcome(
                    WriteStatus.VERIFIED,
                    "The disk was written and verified.",
                    seconds=61.5,
                    notes=(
                        "Unpacked the MSA archive to a plain sector image.",
                        "No checksum is known for any dump of this disc, so the download was not checked.",
                    ),
                ),
                "/nas/Crew 1.st",
            ),
            (
                "Crew 2",
                WriteOutcome(
                    WriteStatus.FAILED,
                    "Track 40.1 could not be verified.",
                    diagnostic="gw output",
                    retries=3,
                    failed_tracks=("40.1", "41.0"),
                ),
                "Fast Host (fast.invalid)",
            ),
            (
                "Crew 3",
                WriteOutcome(WriteStatus.WRITTEN, "Flux images are not verified."),
                "/nas/c.scp",
            ),
            ("Crew 4", WriteOutcome(WriteStatus.SKIPPED, "Skipped at the insert-disk prompt."), ""),
            ("Crew 5", WriteOutcome(WriteStatus.UNAVAILABLE, "No image was found."), ""),
        ),
    )


class HistoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.folder = Path(tempfile.mkdtemp(prefix="pf-history-"))
        self.addCleanup(shutil.rmtree, self.folder, True)
        self.db = UserDatabase.open(self.folder / "user.sqlite")
        self.addCleanup(self.db.close)
        self.history = History(self.db)

    def test_round_trip_newest_first(self) -> None:
        older = summary("2026-09-10T10:00:00")
        newer = summary()
        self.history.record(older)
        self.history.record(newer)
        sessions = self.history.sessions()
        self.assertEqual(sessions, [newer, older])
        self.assertEqual(self.history.sessions(limit=1), [newer])

    def test_notes_are_kept(self) -> None:
        self.history.record(summary())
        (_label, outcome, _source), *_rest = self.history.sessions()[0].items
        self.assertEqual(len(outcome.notes), 2)
        self.assertIn("was not checked", outcome.notes[1])

    def test_clear(self) -> None:
        self.history.record(summary())
        self.history.clear()
        self.assertEqual(self.history.sessions(), [])

    def test_report_text(self) -> None:
        text = report_text(summary())
        self.assertIn("Started: 2026-09-12 14:03", text)
        self.assertIn("Drive: A", text)
        self.assertIn("1. Crew 1: written and verified", text)
        self.assertIn("   Source: /nas/Crew 1.st", text)
        self.assertIn("   Note: Unpacked the MSA archive to a plain sector image.", text)
        self.assertIn(
            "   Note: No checksum is known for any dump of this disc, so the download was not checked.",
            text,
        )
        self.assertIn("2. Crew 2: failed", text)
        self.assertIn("   Retries: 3", text)
        self.assertIn("   Failed tracks: 40.1, 41.0", text)
        self.assertIn("3. Crew 3: written, not verified", text)
        self.assertIn(
            "Disks: 5. Verified: 1. Written without verification: 1. Failed: 1. "
            "Skipped or cancelled: 1. No usable image: 1.",
            text,
        )
        self.assertTrue(text.isascii())


class HistoryMigrationTests(unittest.TestCase):
    def test_sessions_recorded_before_notes_were_kept_read_back_without_notes(self) -> None:
        folder = Path(tempfile.mkdtemp(prefix="pf-history-old-"))
        self.addCleanup(shutil.rmtree, folder, True)
        path = folder / "user.sqlite"
        connection = sqlite3.connect(path)
        for script in MIGRATIONS[:2]:
            connection.executescript(script)
        connection.execute("PRAGMA user_version = 2")
        connection.execute(
            "INSERT INTO sessions(id, started, finished, drive) "
            "VALUES (1, '2026-09-01T10:00:00', '2026-09-01T10:05:00', 'A')"
        )
        connection.execute(
            "INSERT INTO session_items(session_id, position, label, status, summary) "
            "VALUES (1, 0, 'Crew 1', 'verified', 'Written and verified.')"
        )
        connection.commit()
        connection.close()
        db = UserDatabase.open(path)
        self.addCleanup(db.close)
        (session,) = History(db).sessions()
        (label, outcome, _source) = session.items[0]
        self.assertEqual(
            (label, outcome.status, outcome.notes), ("Crew 1", WriteStatus.VERIFIED, ())
        )
        History(db).record(summary())
        self.assertEqual(len(History(db).sessions()[0].items[0][1].notes), 2)


if __name__ == "__main__":
    unittest.main()

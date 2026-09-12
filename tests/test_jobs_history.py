from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

from piratefinder.jobs.history import History, report_text
from piratefinder.library.userdb import UserDatabase
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
                    WriteStatus.VERIFIED, "The disk was written and verified.", seconds=61.5
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


if __name__ == "__main__":
    unittest.main()

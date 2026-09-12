"""Past write sessions and their plain-text reports."""

from __future__ import annotations

import json
from datetime import datetime

from ..library.userdb import UserDatabase
from ..models import SessionSummary, WriteOutcome, WriteStatus

_RESULT_WORDS = {
    WriteStatus.VERIFIED: "written and verified",
    WriteStatus.WRITTEN: "written, not verified",
    WriteStatus.FAILED: "failed",
    WriteStatus.WRITE_PROTECTED: "failed, the disk is write-protected",
    WriteStatus.NO_DISK: "failed, no disk in the drive",
    WriteStatus.CANCELLED: "cancelled",
    WriteStatus.SKIPPED: "skipped",
    WriteStatus.UNAVAILABLE: "no usable image",
}


class History:
    """Write sessions stored in the user database."""

    def __init__(self, userdb: UserDatabase) -> None:
        self.userdb = userdb

    def record(self, summary: SessionSummary) -> int:
        """Store a finished session and return its id."""
        return self.userdb.add_session(
            summary.started,
            summary.finished,
            summary.drive,
            [
                (
                    label,
                    str(outcome.status),
                    outcome.summary,
                    outcome.diagnostic,
                    outcome.retries,
                    outcome.failed_tracks,
                    outcome.seconds,
                    source,
                )
                for label, outcome, source in summary.items
            ],
        )

    def sessions(self, limit: int = 50) -> list[SessionSummary]:
        """Recent sessions, newest first."""
        result = []
        for _id, started, finished, drive, rows in self.userdb.session_rows(limit):
            items = []
            for label, status, text, diagnostic, retries, failed, seconds, source in rows:
                try:
                    tracks = tuple(str(track) for track in json.loads(failed or "[]"))
                except ValueError:
                    tracks = ()
                try:
                    state = WriteStatus(status)
                except ValueError:
                    state = WriteStatus.FAILED
                outcome = WriteOutcome(
                    status=state,
                    summary=text,
                    diagnostic=diagnostic,
                    retries=int(retries or 0),
                    failed_tracks=tracks,
                    seconds=float(seconds or 0.0),
                )
                items.append((label, outcome, source))
            result.append(SessionSummary(started, finished, drive, tuple(items)))
        return result

    def clear(self) -> None:
        """Forget every recorded session."""
        self.userdb.delete_sessions()


def _when(text: str) -> str:
    try:
        return datetime.fromisoformat(text).strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return text


def report_text(summary: SessionSummary) -> str:
    """A plain-text report of a session, fit to paste into a message or print."""
    lines = [
        "PirateFinder write session",
        f"Started: {_when(summary.started)}",
        f"Finished: {_when(summary.finished)}",
        f"Drive: {summary.drive}",
        "",
    ]
    for number, (label, outcome, source) in enumerate(summary.items, start=1):
        lines.append(f"{number}. {label}: {_RESULT_WORDS.get(outcome.status, outcome.status)}")
        if outcome.summary:
            lines.append(f"   {outcome.summary}")
        if outcome.retries:
            lines.append(f"   Retries: {outcome.retries}")
        if outcome.failed_tracks:
            lines.append(f"   Failed tracks: {', '.join(outcome.failed_tracks)}")
        if source:
            lines.append(f"   Source: {source}")
    failed = summary.count(WriteStatus.FAILED, WriteStatus.WRITE_PROTECTED, WriteStatus.NO_DISK)
    skipped = summary.count(WriteStatus.SKIPPED, WriteStatus.CANCELLED)
    lines += [
        "",
        f"Disks: {len(summary.items)}. Verified: {summary.count(WriteStatus.VERIFIED)}. "
        f"Written without verification: {summary.count(WriteStatus.WRITTEN)}. "
        f"Failed: {failed}. Skipped or cancelled: {skipped}. "
        f"No usable image: {summary.count(WriteStatus.UNAVAILABLE)}.",
    ]
    return "\n".join(lines) + "\n"

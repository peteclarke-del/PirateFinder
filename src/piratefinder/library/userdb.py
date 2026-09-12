"""The user database: library index, write history and user corrections.

It lives at ``~/.local/share/piratefinder/user.sqlite`` and is separate from
the read-only catalogue, so a catalogue update never loses anything the user
did. The scanner writes from a worker thread while the interface reads, so
every thread gets its own connection, the database runs in WAL mode, and
writes are serialised with a lock.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import sqlite3
import threading
import time
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..models import LocalFile


class UserDatabaseError(RuntimeError):
    """The user database cannot be opened or upgraded."""


# Each entry upgrades the database by one version. PRAGMA user_version holds
# the number of scripts applied.
MIGRATIONS: tuple[str, ...] = (
    """
    CREATE TABLE scanned_files (
        path TEXT PRIMARY KEY,
        size INTEGER NOT NULL,
        mtime REAL NOT NULL,
        last_seen REAL NOT NULL DEFAULT 0,
        error TEXT NOT NULL DEFAULT ''
    );

    CREATE TABLE library_files (
        id INTEGER PRIMARY KEY,
        path TEXT NOT NULL,
        member TEXT NOT NULL DEFAULT '',
        size INTEGER NOT NULL DEFAULT 0,
        mtime REAL NOT NULL DEFAULT 0,
        format TEXT NOT NULL DEFAULT '',
        crc32 TEXT NOT NULL DEFAULT '',
        md5 TEXT NOT NULL DEFAULT '',
        sha1 TEXT NOT NULL DEFAULT '',
        sha512 TEXT NOT NULL DEFAULT '',
        raw_crc32 TEXT NOT NULL DEFAULT '',
        raw_md5 TEXT NOT NULL DEFAULT '',
        raw_sha1 TEXT NOT NULL DEFAULT '',
        raw_size INTEGER,
        image_id INTEGER,
        disk_id INTEGER,
        volume_label TEXT NOT NULL DEFAULT '',
        listing TEXT NOT NULL DEFAULT '[]',
        display_name TEXT NOT NULL DEFAULT '',
        parsed TEXT NOT NULL DEFAULT '{}',
        last_seen REAL NOT NULL DEFAULT 0,
        error TEXT NOT NULL DEFAULT '',
        UNIQUE (path, member)
    );
    CREATE INDEX library_disk ON library_files(disk_id);
    CREATE INDEX library_image ON library_files(image_id);

    CREATE VIRTUAL TABLE library_fts USING fts5(
        display_name, volume_label, listing,
        content='library_files', content_rowid='id',
        tokenize="unicode61 remove_diacritics 2",
        prefix='2 3'
    );
    CREATE TRIGGER library_files_insert AFTER INSERT ON library_files BEGIN
        INSERT INTO library_fts(rowid, display_name, volume_label, listing)
        VALUES (new.id, new.display_name, new.volume_label, new.listing);
    END;
    CREATE TRIGGER library_files_delete AFTER DELETE ON library_files BEGIN
        INSERT INTO library_fts(library_fts, rowid, display_name, volume_label, listing)
        VALUES ('delete', old.id, old.display_name, old.volume_label, old.listing);
    END;
    CREATE TRIGGER library_files_update AFTER UPDATE ON library_files BEGIN
        INSERT INTO library_fts(library_fts, rowid, display_name, volume_label, listing)
        VALUES ('delete', old.id, old.display_name, old.volume_label, old.listing);
        INSERT INTO library_fts(rowid, display_name, volume_label, listing)
        VALUES (new.id, new.display_name, new.volume_label, new.listing);
    END;

    CREATE TABLE sessions (
        id INTEGER PRIMARY KEY,
        started TEXT NOT NULL,
        finished TEXT NOT NULL,
        drive TEXT NOT NULL DEFAULT ''
    );
    CREATE TABLE session_items (
        session_id INTEGER NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
        position INTEGER NOT NULL,
        label TEXT NOT NULL,
        status TEXT NOT NULL,
        summary TEXT NOT NULL DEFAULT '',
        diagnostic TEXT NOT NULL DEFAULT '',
        retries INTEGER NOT NULL DEFAULT 0,
        failed_tracks TEXT NOT NULL DEFAULT '[]',
        seconds REAL NOT NULL DEFAULT 0,
        source TEXT NOT NULL DEFAULT '',
        PRIMARY KEY (session_id, position)
    );

    CREATE TABLE corrections (
        disk_id INTEGER NOT NULL,
        field TEXT NOT NULL,
        value TEXT NOT NULL,
        PRIMARY KEY (disk_id, field)
    );
    """,
)

SCHEMA_VERSION = len(MIGRATIONS)

_ENTRY_COLUMNS = (
    "id",
    "path",
    "member",
    "size",
    "mtime",
    "format",
    "crc32",
    "md5",
    "sha1",
    "sha512",
    "raw_crc32",
    "raw_md5",
    "raw_sha1",
    "raw_size",
    "image_id",
    "disk_id",
    "volume_label",
    "listing",
    "display_name",
    "parsed",
    "error",
)
_SELECT_ENTRIES = f"SELECT {', '.join(_ENTRY_COLUMNS)} FROM library_files"


@dataclass(frozen=True, slots=True)
class LibraryEntry:
    """One indexed image: a plain file or one member of an archive.

    ``crc32``, ``md5``, ``sha1`` and ``sha512`` are hashes of the image as it
    is stored; the ``raw_`` hashes are of the decoded sector image.
    """

    path: str
    member: str = ""
    size: int = 0
    mtime: float = 0.0
    format: str = ""
    crc32: str = ""
    md5: str = ""
    sha1: str = ""
    sha512: str = ""
    raw_crc32: str = ""
    raw_md5: str = ""
    raw_sha1: str = ""
    raw_size: int | None = None
    image_id: int | None = None
    disk_id: int | None = None
    volume_label: str = ""
    listing: tuple[str, ...] = ()
    display_name: str = ""
    parsed: dict[str, Any] = field(default_factory=dict)
    error: str = ""
    id: int | None = None

    def to_local(self) -> LocalFile:
        """The entry as the shared ``LocalFile`` value.

        CRC32, MD5 and SHA-1 are of the raw sector image when it could be
        decoded, as in TOSEC; SHA-512 is of the file as stored, as in Atari
        Legend.
        """
        return LocalFile(
            path=self.path,
            member=self.member,
            format=self.format,
            size=self.raw_size if self.raw_size is not None else self.size,
            crc32=self.raw_crc32 or self.crc32,
            md5=self.raw_md5 or self.md5,
            sha1=self.raw_sha1 or self.sha1,
            sha512=self.sha512,
            image_id=self.image_id,
            disk_id=self.disk_id,
            volume_label=self.volume_label,
            listing=self.listing,
            display_name=self.display_name,
        )


def _entry_from_row(row: Sequence[Any]) -> LibraryEntry:
    values = dict(zip(_ENTRY_COLUMNS, row, strict=True))
    try:
        listing = tuple(str(name) for name in json.loads(values["listing"] or "[]"))
    except (ValueError, TypeError):
        listing = ()
    try:
        parsed = json.loads(values["parsed"] or "{}")
        if not isinstance(parsed, dict):
            parsed = {}
    except (ValueError, TypeError):
        parsed = {}
    values["listing"] = listing
    values["parsed"] = parsed
    return LibraryEntry(**values)


def fts_query(text: str) -> str:
    """An FTS5 query matching every word of ``text`` as a prefix."""
    words = re.findall(r"\w+", text.casefold())
    return " AND ".join(f'"{word}"*' for word in words)


class UserDatabase:
    """Thread-safe access to ``user.sqlite``."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._registry_lock = threading.Lock()
        self._write_lock = threading.RLock()
        self._connections: dict[int, sqlite3.Connection] = {}
        self._closed = False

    @classmethod
    def open(cls, path: Path) -> UserDatabase:
        """Open or create the database at ``path`` and bring its schema up to date."""
        database = cls(Path(path))
        database.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            database._migrate()
        except sqlite3.DatabaseError as error:
            database.close()
            raise UserDatabaseError(
                f"The user database {path} could not be opened: {error}"
            ) from error
        return database

    # Connections -----------------------------------------------------------

    def connection(self) -> sqlite3.Connection:
        """The calling thread's connection, created on first use."""
        ident = threading.get_ident()
        with self._registry_lock:
            if self._closed:
                raise UserDatabaseError("The user database has been closed.")
            connection = self._connections.get(ident)
            if connection is None:
                self._prune_dead_threads()
                connection = sqlite3.connect(
                    self.path, timeout=30, isolation_level=None, check_same_thread=False
                )
                connection.execute("PRAGMA foreign_keys = ON")
                connection.execute("PRAGMA synchronous = NORMAL")
                connection.execute("PRAGMA busy_timeout = 30000")
                self._connections[ident] = connection
            return connection

    @contextlib.contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """A write transaction, serialised with every other writer in the process."""
        connection = self.connection()
        with self._write_lock:
            connection.execute("BEGIN IMMEDIATE")
            try:
                yield connection
            except BaseException:
                connection.execute("ROLLBACK")
                raise
            connection.execute("COMMIT")

    def close(self) -> None:
        """Close every connection. The object cannot be used afterwards."""
        with self._registry_lock:
            self._closed = True
            for connection in self._connections.values():
                with contextlib.suppress(sqlite3.Error):
                    connection.close()
            self._connections.clear()

    def _prune_dead_threads(self) -> None:
        alive = {thread.ident for thread in threading.enumerate()}
        for ident in [ident for ident in self._connections if ident not in alive]:
            with contextlib.suppress(sqlite3.Error):
                self._connections.pop(ident).close()

    def _migrate(self) -> None:
        connection = self.connection()
        connection.execute("PRAGMA journal_mode = WAL")
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        if version > SCHEMA_VERSION:
            raise UserDatabaseError(
                f"The user database {self.path} was written by a newer version of PirateFinder."
            )
        for number in range(version, SCHEMA_VERSION):
            with self.transaction() as db:
                for statement in _split_script(MIGRATIONS[number]):
                    db.execute(statement)
                db.execute(f"PRAGMA user_version = {number + 1}")

    # Library index ---------------------------------------------------------

    def scanned_files(self) -> dict[str, tuple[int, float, str]]:
        """Every scanned file: path -> (size, mtime, error)."""
        rows = self.connection().execute("SELECT path, size, mtime, error FROM scanned_files")
        return {path: (size, mtime, error) for path, size, mtime, error in rows}

    def store_file(
        self,
        path: str,
        size: int,
        mtime: float,
        entries: Iterable[LibraryEntry],
        error: str = "",
    ) -> int:
        """Replace everything indexed for one file; return how many entries are new."""
        now = time.time()
        entries = list(entries)
        with self.transaction() as db:
            before = {
                member
                for (member,) in db.execute(
                    "SELECT member FROM library_files WHERE path = ?", (path,)
                )
            }
            db.execute("DELETE FROM library_files WHERE path = ?", (path,))
            db.executemany(
                """INSERT INTO library_files(path, member, size, mtime, format, crc32, md5,
                       sha1, sha512, raw_crc32, raw_md5, raw_sha1, raw_size, image_id, disk_id,
                       volume_label, listing, display_name, parsed, last_seen, error)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                [
                    (
                        path,
                        entry.member,
                        entry.size,
                        mtime,
                        entry.format,
                        entry.crc32,
                        entry.md5,
                        entry.sha1,
                        entry.sha512,
                        entry.raw_crc32,
                        entry.raw_md5,
                        entry.raw_sha1,
                        entry.raw_size,
                        entry.image_id,
                        entry.disk_id,
                        entry.volume_label,
                        json.dumps(list(entry.listing)),
                        entry.display_name,
                        json.dumps(entry.parsed, sort_keys=True),
                        now,
                        entry.error,
                    )
                    for entry in entries
                ],
            )
            db.execute(
                """INSERT INTO scanned_files(path, size, mtime, last_seen, error)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(path) DO UPDATE SET size = excluded.size,
                       mtime = excluded.mtime, last_seen = excluded.last_seen,
                       error = excluded.error""",
                (path, size, mtime, now, error),
            )
        return sum(1 for entry in entries if entry.member not in before)

    def mark_seen(self, paths: Iterable[str]) -> None:
        """Record that unchanged files were still present."""
        now = time.time()
        with self.transaction() as db:
            db.executemany(
                "UPDATE scanned_files SET last_seen = ? WHERE path = ?",
                [(now, path) for path in paths],
            )

    def remove_files(self, paths: Iterable[str]) -> int:
        """Forget files that have gone; return how many image entries were removed."""
        removed = 0
        with self.transaction() as db:
            for path in paths:
                removed += db.execute("DELETE FROM library_files WHERE path = ?", (path,)).rowcount
                db.execute("DELETE FROM scanned_files WHERE path = ?", (path,))
        return removed

    def entries(
        self,
        *,
        disk_id: int | None = None,
        image_id: int | None = None,
        path: str | None = None,
        unmatched: bool = False,
    ) -> list[LibraryEntry]:
        """Index entries filtered by disk, image, file path or unmatched state."""
        clauses: list[str] = []
        values: list[Any] = []
        if disk_id is not None:
            clauses.append("disk_id = ?")
            values.append(disk_id)
        if image_id is not None:
            clauses.append("image_id = ?")
            values.append(image_id)
        if path is not None:
            clauses.append("path = ?")
            values.append(path)
        if unmatched:
            clauses.append("disk_id IS NULL")
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self.connection().execute(
            f"{_SELECT_ENTRIES}{where} ORDER BY display_name, path, member", values
        )
        return [_entry_from_row(row) for row in rows]

    def counts_under(self, folder: str) -> tuple[int, int]:
        """(images, matched images) indexed below ``folder``."""
        prefix = folder.rstrip(os.sep) + os.sep
        images, matched = (
            self.connection()
            .execute(
                "SELECT COUNT(*), COUNT(disk_id) FROM library_files WHERE substr(path, 1, ?) = ?",
                (len(prefix), prefix),
            )
            .fetchone()
        )
        return images, matched

    def last_scan(self) -> float | None:
        """When a scan last saw a file, as a Unix time, or None before the first scan."""
        row = self.connection().execute("SELECT MAX(last_seen) FROM scanned_files").fetchone()
        return float(row[0]) if row and row[0] else None

    def set_matches(self, matches: Iterable[tuple[int, int | None, int | None]]) -> None:
        """Store (entry id, image id, disk id) after matching against a catalogue."""
        with self.transaction() as db:
            db.executemany(
                "UPDATE library_files SET image_id = ?, disk_id = ? WHERE id = ?",
                [(image_id, disk_id, entry_id) for entry_id, image_id, disk_id in matches],
            )

    def disks_present(self, disk_ids: Iterable[int]) -> set[int]:
        """The disk ids among ``disk_ids`` that have at least one local image."""
        ids = list(dict.fromkeys(disk_ids))
        found: set[int] = set()
        connection = self.connection()
        for start in range(0, len(ids), 500):
            chunk = ids[start : start + 500]
            marks = ", ".join("?" * len(chunk))
            found.update(
                disk_id
                for (disk_id,) in connection.execute(
                    f"SELECT DISTINCT disk_id FROM library_files WHERE disk_id IN ({marks})",
                    chunk,
                )
            )
        return found

    def search_unmatched(self, text: str, limit: int = 200) -> list[LibraryEntry]:
        """Unmatched entries whose name, volume label or file listing match ``text``."""
        query = fts_query(text)
        connection = self.connection()
        if not query:
            rows = connection.execute(
                f"{_SELECT_ENTRIES} WHERE disk_id IS NULL ORDER BY display_name, path, member "
                "LIMIT ?",
                (limit,),
            )
            return [_entry_from_row(row) for row in rows]
        columns = ", ".join(f"library_files.{column}" for column in _ENTRY_COLUMNS)
        try:
            rows = connection.execute(
                f"""SELECT {columns} FROM library_fts
                    JOIN library_files ON library_files.id = library_fts.rowid
                    WHERE library_fts MATCH ? AND library_files.disk_id IS NULL
                    ORDER BY bm25(library_fts), library_files.display_name
                    LIMIT ?""",
                (query, limit),
            ).fetchall()
        except sqlite3.OperationalError:
            return []
        return [_entry_from_row(row) for row in rows]

    def library_counts(self) -> dict[str, int]:
        """Counts for the Library page: images, matched, unmatched, duplicates, folders."""
        connection = self.connection()
        images, matched = connection.execute(
            "SELECT COUNT(*), COUNT(disk_id) FROM library_files"
        ).fetchone()
        distinct = connection.execute(
            """SELECT COUNT(DISTINCT CASE WHEN raw_md5 != '' THEN raw_md5 ELSE md5 END)
               FROM library_files"""
        ).fetchone()[0]
        folders = {
            os.path.dirname(path)
            for (path,) in connection.execute("SELECT DISTINCT path FROM library_files")
        }
        return {
            "images": images,
            "matched": matched,
            "unmatched": images - matched,
            "duplicates": images - distinct,
            "folders": len(folders),
        }

    # Corrections -----------------------------------------------------------

    def set_correction(self, disk_id: int, field_name: str, value: str) -> None:
        """Override one field of a catalogue disk with the user's own value."""
        with self.transaction() as db:
            db.execute(
                """INSERT INTO corrections(disk_id, field, value) VALUES (?, ?, ?)
                   ON CONFLICT(disk_id, field) DO UPDATE SET value = excluded.value""",
                (disk_id, field_name, value),
            )

    def remove_correction(self, disk_id: int, field_name: str) -> None:
        """Drop a user correction so the catalogue value shows again."""
        with self.transaction() as db:
            db.execute(
                "DELETE FROM corrections WHERE disk_id = ? AND field = ?", (disk_id, field_name)
            )

    def corrections(self, disk_ids: Iterable[int]) -> dict[int, dict[str, str]]:
        """User corrections for the given disks: disk id -> field -> value."""
        ids = list(dict.fromkeys(disk_ids))
        result: dict[int, dict[str, str]] = {}
        connection = self.connection()
        for start in range(0, len(ids), 500):
            chunk = ids[start : start + 500]
            marks = ", ".join("?" * len(chunk))
            for disk_id, field_name, value in connection.execute(
                f"SELECT disk_id, field, value FROM corrections WHERE disk_id IN ({marks})",
                chunk,
            ):
                result.setdefault(disk_id, {})[field_name] = value
        return result

    # History ---------------------------------------------------------------

    def add_session(
        self, started: str, finished: str, drive: str, items: Iterable[Sequence[Any]]
    ) -> int:
        """Store one write session.

        Each item is (label, status, summary, diagnostic, retries,
        failed_tracks, seconds, source).
        """
        with self.transaction() as db:
            cursor = db.execute(
                "INSERT INTO sessions(started, finished, drive) VALUES (?, ?, ?)",
                (started, finished, drive),
            )
            session_id = int(cursor.lastrowid or 0)
            db.executemany(
                """INSERT INTO session_items(session_id, position, label, status, summary,
                       diagnostic, retries, failed_tracks, seconds, source)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                [
                    (
                        session_id,
                        position,
                        label,
                        status,
                        summary,
                        diagnostic,
                        retries,
                        json.dumps(list(failed_tracks)),
                        seconds,
                        source,
                    )
                    for position, (
                        label,
                        status,
                        summary,
                        diagnostic,
                        retries,
                        failed_tracks,
                        seconds,
                        source,
                    ) in enumerate(items)
                ],
            )
        return session_id

    def session_rows(self, limit: int = 50) -> list[tuple[int, str, str, str, list[tuple]]]:
        """Recent sessions, newest first: (id, started, finished, drive, item rows)."""
        connection = self.connection()
        sessions = connection.execute(
            "SELECT id, started, finished, drive FROM sessions ORDER BY started DESC, id DESC "
            "LIMIT ?",
            (limit,),
        ).fetchall()
        result = []
        for session_id, started, finished, drive in sessions:
            items = connection.execute(
                """SELECT label, status, summary, diagnostic, retries, failed_tracks, seconds,
                          source
                   FROM session_items WHERE session_id = ? ORDER BY position""",
                (session_id,),
            ).fetchall()
            result.append((session_id, started, finished, drive, items))
        return result

    def delete_sessions(self) -> None:
        """Forget every recorded write session."""
        with self.transaction() as db:
            db.execute("DELETE FROM session_items")
            db.execute("DELETE FROM sessions")


def _split_script(script: str) -> list[str]:
    """Split a migration into statements, keeping trigger bodies whole."""
    statements: list[str] = []
    current = ""
    for line in script.splitlines(keepends=True):
        current += line
        if sqlite3.complete_statement(current):
            if current.strip():
                statements.append(current.strip())
            current = ""
    if current.strip():
        statements.append(current.strip())
    return statements

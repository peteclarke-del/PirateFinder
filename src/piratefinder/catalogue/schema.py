"""The catalogue database layout.

The catalogue is a read-only SQLite file produced by ``catalogue_builder`` and
shipped with the application. Nothing the user does is stored in it: the
library index, corrections and write history live in a separate user database,
so replacing the catalogue with a newer snapshot never loses anything.
"""

from __future__ import annotations

import sqlite3

SCHEMA_VERSION = 1

DDL = """
CREATE TABLE meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE sources (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    url TEXT NOT NULL,
    licence TEXT NOT NULL DEFAULT '',
    retrieved TEXT NOT NULL DEFAULT '',
    records INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE series (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    platform TEXT NOT NULL,
    kind TEXT NOT NULL,
    group_name TEXT NOT NULL DEFAULT '',
    description TEXT NOT NULL DEFAULT ''
);

CREATE TABLE series_alias (
    alias TEXT NOT NULL,
    series_id TEXT NOT NULL REFERENCES series(id),
    PRIMARY KEY (alias, series_id)
);

CREATE TABLE disks (
    id INTEGER PRIMARY KEY,
    series_id TEXT REFERENCES series(id),
    number INTEGER,
    part TEXT NOT NULL DEFAULT '',
    version TEXT NOT NULL DEFAULT '',
    label TEXT NOT NULL,
    title TEXT NOT NULL DEFAULT '',
    date TEXT NOT NULL DEFAULT '',
    platform TEXT NOT NULL,
    kind TEXT NOT NULL,
    publisher TEXT NOT NULL DEFAULT '',
    cracker TEXT NOT NULL DEFAULT '',
    condition TEXT NOT NULL DEFAULT '',
    notes TEXT NOT NULL DEFAULT '',
    credits TEXT NOT NULL DEFAULT '',
    menu_text TEXT NOT NULL DEFAULT ''
);
CREATE INDEX disks_series ON disks(series_id, number, part, version);
CREATE INDEX disks_platform_kind ON disks(platform, kind);

CREATE TABLE contents (
    id INTEGER PRIMARY KEY,
    disk_id INTEGER NOT NULL REFERENCES disks(id),
    position INTEGER NOT NULL DEFAULT 0,
    title TEXT NOT NULL,
    kind TEXT NOT NULL,
    publisher TEXT NOT NULL DEFAULT '',
    cracker TEXT NOT NULL DEFAULT '',
    version TEXT NOT NULL DEFAULT '',
    extra TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL DEFAULT ''
);
CREATE INDEX contents_disk ON contents(disk_id, position);

CREATE TABLE images (
    id INTEGER PRIMARY KEY,
    disk_id INTEGER NOT NULL REFERENCES disks(id),
    name TEXT NOT NULL,
    format TEXT NOT NULL,
    flags TEXT NOT NULL DEFAULT '',
    size INTEGER,
    crc32 TEXT NOT NULL DEFAULT '',
    md5 TEXT NOT NULL DEFAULT '',
    sha1 TEXT NOT NULL DEFAULT '',
    sha512 TEXT NOT NULL DEFAULT '',
    bad INTEGER NOT NULL DEFAULT 0,
    rank INTEGER NOT NULL DEFAULT 0,
    source TEXT NOT NULL DEFAULT ''
);
CREATE INDEX images_disk ON images(disk_id, rank);
CREATE INDEX images_md5 ON images(md5);
CREATE INDEX images_sha1 ON images(sha1);
CREATE INDEX images_sha512 ON images(sha512);
CREATE INDEX images_crc ON images(crc32, size);

CREATE TABLE locations (
    id INTEGER PRIMARY KEY,
    disk_id INTEGER NOT NULL REFERENCES disks(id),
    image_id INTEGER REFERENCES images(id),
    provider TEXT NOT NULL,
    url TEXT NOT NULL,
    container TEXT NOT NULL DEFAULT '',
    member TEXT NOT NULL DEFAULT '',
    size INTEGER,
    hash_kind TEXT NOT NULL DEFAULT '',
    hash_value TEXT NOT NULL DEFAULT '',
    page_url TEXT NOT NULL DEFAULT '',
    priority INTEGER NOT NULL DEFAULT 100
);
CREATE INDEX locations_disk ON locations(disk_id, priority);

CREATE TABLE links (
    disk_id INTEGER NOT NULL REFERENCES disks(id),
    label TEXT NOT NULL,
    url TEXT NOT NULL
);
CREATE INDEX links_disk ON links(disk_id);

-- Full-text index, one row per disk, rowid = disks.id. Contentless: the
-- columns are only searched and ranked, the values are read from the tables.
CREATE VIRTUAL TABLE disk_fts USING fts5(
    label, series, contents, people, notes,
    content='',
    tokenize="unicode61 remove_diacritics 2",
    prefix='2 3'
);

-- Substring index for partial words and near misses, rowid = disks.id.
CREATE VIRTUAL TABLE disk_trigram USING fts5(
    text,
    content='',
    tokenize='trigram'
);
"""


def create(connection: sqlite3.Connection) -> None:
    """Create an empty catalogue in a fresh database."""
    connection.executescript(DDL)
    connection.execute(
        "INSERT INTO meta(key, value) VALUES ('schema_version', ?)",
        (str(SCHEMA_VERSION),),
    )


def schema_version(connection: sqlite3.Connection) -> int:
    try:
        row = connection.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
    except sqlite3.DatabaseError:
        return 0
    return int(row[0]) if row else 0

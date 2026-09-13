"""The catalogue database layout.

The catalogue is a read-only SQLite file produced by ``catalogue_builder`` and
shipped with the application. Nothing the user does is stored in it: the
library index, corrections and write history live in a separate user database,
so replacing the catalogue with a newer snapshot never loses anything.
"""

from __future__ import annotations

import sqlite3

SCHEMA_VERSION = 3

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
    menu_text TEXT NOT NULL DEFAULT '',
    -- Derived at build time with piratefinder.archive_layout, so filters,
    -- sorting and the download folder layout agree.
    category TEXT NOT NULL DEFAULT '',
    crew TEXT NOT NULL DEFAULT '',
    -- The history of the crew that made this disk; NULL when no source
    -- describes that crew. Crew names are not unique, so never join by name.
    crew_id INTEGER REFERENCES crews(id),
    year INTEGER,
    month INTEGER,
    day INTEGER,
    sort_title TEXT NOT NULL DEFAULT ''
);
CREATE INDEX disks_series ON disks(series_id, number, part, version);
CREATE INDEX disks_platform_kind ON disks(platform, kind);
CREATE INDEX disks_crew ON disks(crew);
CREATE INDEX disks_year ON disks(year, month);
CREATE INDEX disks_category ON disks(category);
CREATE INDEX disks_sort_title ON disks(sort_title);

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
    source TEXT NOT NULL DEFAULT '',
    virus TEXT NOT NULL DEFAULT '',          -- TOSEC [v Name]
    virus_damage INTEGER NOT NULL DEFAULT 0, -- TOSEC [b virus damage]
    antivirus TEXT NOT NULL DEFAULT ''       -- TOSEC [m ... antivirus] boot code
);
CREATE INDEX images_disk ON images(disk_id, rank);
CREATE INDEX images_md5 ON images(md5);
CREATE INDEX images_sha1 ON images(sha1);
CREATE INDEX images_sha512 ON images(sha512);
CREATE INDEX images_crc ON images(crc32, size);

-- Download locations. Addresses are stored as a shared beginning from
-- address_prefix and the rest; Catalogue.locations puts them together.
CREATE TABLE locations (
    id INTEGER PRIMARY KEY,
    disk_id INTEGER NOT NULL REFERENCES disks(id),
    image_id INTEGER REFERENCES images(id),
    provider TEXT NOT NULL,
    url_prefix INTEGER NOT NULL REFERENCES address_prefix(id),
    url TEXT NOT NULL,             -- the address after its prefix
    container TEXT NOT NULL DEFAULT '',
    member TEXT NOT NULL DEFAULT '',
    size INTEGER,
    hash_kind TEXT NOT NULL DEFAULT '',
    hash_value TEXT NOT NULL DEFAULT '',
    page_prefix INTEGER REFERENCES address_prefix(id),  -- NULL when there is no page
    page_url TEXT NOT NULL DEFAULT '',  -- after its prefix
    priority INTEGER NOT NULL DEFAULT 100
);
CREATE INDEX locations_disk ON locations(disk_id, priority);
-- The disks an enabled provider hosts, for the "available only" filter.
CREATE INDEX locations_provider ON locations(provider, disk_id);

CREATE TABLE links (
    disk_id INTEGER NOT NULL REFERENCES disks(id),
    label TEXT NOT NULL,
    url TEXT NOT NULL
);
CREATE INDEX links_disk ON links(disk_id);

-- Full-text index, one row per disk, rowid = disks.id. Contentless: the
-- columns are only searched and ranked, the values are read from the tables.
CREATE VIRTUAL TABLE disk_fts USING fts5(
    label, series, contents, people, notes, crew, facets, files,
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

-- One row per title on a disc, and one per disc that lists no titles, so the
-- Find screen can list titles. content_id is NULL for the disc rows.
CREATE TABLE entries (
    id INTEGER PRIMARY KEY,
    disk_id INTEGER NOT NULL REFERENCES disks(id),
    content_id INTEGER REFERENCES contents(id),
    title TEXT NOT NULL,
    sort_title TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT ''
);
CREATE INDEX entries_disk ON entries(disk_id);
CREATE INDEX entries_sort_title ON entries(sort_title);

-- Full-text index for title rows, rowid = entries.id. "facets" holds the
-- words for platform, type, disc kind and year, so free text finds them too.
CREATE VIRTUAL TABLE entry_fts USING fts5(
    title, disk, crew, people, facets, files, notes,
    content='',
    tokenize="unicode61 remove_diacritics 2",
    prefix='2 3'
);

-- Crews: history and members for the details pane, one row per crew name
-- and platform as the disks carry them (and, where a source has several
-- crews of that name there, per crew the disks' sources credit). Disks point
-- at their row with crew_id; the name alone may belong to another crew.
CREATE TABLE crews (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,              -- as in disks.crew
    platform TEXT NOT NULL,          -- as in disks.platform
    notes TEXT NOT NULL DEFAULT '',  -- plain text
    members TEXT NOT NULL DEFAULT '',-- comma separated
    founded TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL DEFAULT '',
    url TEXT NOT NULL DEFAULT '',
    wikipedia TEXT NOT NULL DEFAULT ''  -- English Wikipedia article title
);

-- Screenshots and other pictures, fetched by the application on demand.
-- Addresses are stored as for locations, and the source and credit line of a
-- row come from media_credit. Catalogue.media puts them back together.
CREATE TABLE media (
    id INTEGER PRIMARY KEY,
    disk_id INTEGER NOT NULL REFERENCES disks(id),
    content_id INTEGER REFERENCES contents(id),  -- NULL for the disc itself
    kind TEXT NOT NULL,          -- 'menu', 'intro', 'snap', 'title', 'boxart', 'demo'
    url_prefix INTEGER NOT NULL REFERENCES address_prefix(id),
    url TEXT NOT NULL,           -- the address after its prefix
    thumb_prefix INTEGER REFERENCES address_prefix(id),  -- NULL when there is no thumbnail
    thumb_url TEXT,              -- after its prefix; NULL when the same as url
    width INTEGER,
    height INTEGER,
    credit_id INTEGER NOT NULL REFERENCES media_credit(id),
    page_prefix INTEGER REFERENCES address_prefix(id),  -- NULL when there is no page
    page_url TEXT NOT NULL DEFAULT '',  -- after its prefix
    rank INTEGER NOT NULL DEFAULT 100  -- lower is shown first
);
CREATE INDEX media_disk ON media(disk_id, rank);
CREATE INDEX media_content ON media(content_id, rank);

-- The source and credit line of media rows, each pair stored once. The
-- credit is a str.format template: "{page}" stands for the row's page
-- address without its scheme ("demozoo.org/productions/100/"), and a brace
-- of the credit itself is doubled.
CREATE TABLE media_credit (
    id INTEGER PRIMARY KEY,
    source TEXT NOT NULL,        -- sources.id
    credit TEXT NOT NULL
);

-- Beginnings that many addresses of locations and media share, such as a
-- folder of pictures or an archive of disk images, each stored once. The
-- prefix with id 0 is empty.
CREATE TABLE address_prefix (
    id INTEGER PRIMARY KEY,
    text TEXT NOT NULL
);

-- Facts, notes and pointers to reference articles for the details pane.
CREATE TABLE trivia (
    id INTEGER PRIMARY KEY,
    disk_id INTEGER REFERENCES disks(id),
    content_id INTEGER REFERENCES contents(id),
    kind TEXT NOT NULL,          -- 'fact', 'note', 'wikipedia' (text holds the article title)
    text TEXT NOT NULL,
    source TEXT NOT NULL,
    url TEXT NOT NULL DEFAULT '',
    licence TEXT NOT NULL DEFAULT ''
);
CREATE INDEX trivia_disk ON trivia(disk_id);
CREATE INDEX trivia_content ON trivia(content_id);
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

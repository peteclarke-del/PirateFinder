"""Read-only access to catalogue.sqlite.

The catalogue is opened with SQLite's read-only URI mode, so nothing the
application does can change it, and every query returns the value types from
``piratefinder.models``. One ``Catalogue`` may be shared by the interface and
worker threads; queries are serialised with a lock.
"""

from __future__ import annotations

import hashlib
import os
import sqlite3
import threading
import urllib.parse
from array import array
from collections.abc import Iterable, Iterator, Mapping
from pathlib import Path

from .. import paths
from ..models import (
    Content,
    ContentKind,
    CrewInfo,
    Disk,
    DiskKind,
    ImageRecord,
    Link,
    Location,
    MediaItem,
    Platform,
    TriviaItem,
)
from ..models import Series as SeriesModel
from . import schema
from .naming import normalise

_CHUNK = 500  # ids per IN (...) list, well below SQLite's variable limit
DISK_SETS = 8  # temporary tables (disk id sets and others) kept for reuse

_DISK_COLUMNS = (
    "d.id, d.label, d.platform, d.kind, d.series_id, COALESCE(s.name, ''), d.number, d.part, "
    "d.version, d.title, d.date, d.publisher, d.cracker, d.condition, d.notes, d.credits, "
    "d.menu_text, d.category, d.crew, d.year, d.month, d.day"
)
_IMAGE_COLUMNS = (
    "id, disk_id, name, format, flags, size, crc32, md5, sha1, sha512, bad, rank, source, virus"
)
_CONTENT_COLUMNS = "disk_id, title, kind, position, publisher, cracker, version, extra, source, id"
# A media row with its addresses and credit put back together (schema.py).
_MEDIA_QUERY = (
    "SELECT m.kind, u.text || m.url, c.source, c.credit, p.text || m.page_url, "
    "t.text || COALESCE(m.thumb_url, m.url), m.width, m.height, m.content_id "
    "FROM media m JOIN address_prefix u ON u.id = m.url_prefix "
    "JOIN media_credit c ON c.id = m.credit_id "
    "LEFT JOIN address_prefix p ON p.id = m.page_prefix "
    "LEFT JOIN address_prefix t ON t.id = m.thumb_prefix "
)
WIKIPEDIA = "wikipedia"  # trivia kind whose text is an English Wikipedia article title
# A location row with its addresses put back together (schema.py).
_LOCATION_QUERY = (
    "SELECT l.id, l.disk_id, l.provider, u.text || l.url, l.image_id, l.container, l.member, "
    "l.size, l.hash_kind, l.hash_value, COALESCE(p.text, '') || l.page_url, l.priority "
    "FROM locations l JOIN address_prefix u ON u.id = l.url_prefix "
    "LEFT JOIN address_prefix p ON p.id = l.page_prefix "
)


class CatalogueError(RuntimeError):
    """The file is missing, is not a catalogue, or has an unsupported layout."""


def layout_problem(version: int) -> str:
    """Why a catalogue of layout ``version`` cannot be read, or "" when it can.

    The words follow the catalogue's path or name: "<path> is not a
    PirateFinder catalogue".
    """
    if version == schema.SCHEMA_VERSION:
        return ""
    if version <= 0:
        return "is not a PirateFinder catalogue"
    return (
        f"was made for a different PirateFinder version (layout {version}; this version "
        f"reads layout {schema.SCHEMA_VERSION})"
    )


def _platform(value: str) -> Platform:
    try:
        return Platform(value)
    except ValueError:
        return Platform.ATARI_ST


def _disk_kind(value: str) -> DiskKind:
    try:
        return DiskKind(value)
    except ValueError:
        return DiskKind.MENU


def _content_kind(value: str) -> ContentKind:
    try:
        return ContentKind(value)
    except ValueError:
        return ContentKind.OTHER


def _disk(row: tuple) -> Disk:
    return Disk(
        id=row[0],
        label=row[1],
        platform=_platform(row[2]),
        kind=_disk_kind(row[3]),
        series_id=row[4],
        series_name=row[5],
        number=row[6],
        part=row[7],
        version=row[8],
        title=row[9],
        date=row[10],
        publisher=row[11],
        cracker=row[12],
        condition=row[13],
        notes=row[14],
        credits=row[15],
        menu_text=row[16],
        category=row[17],
        crew=row[18],
        year=row[19],
        month=row[20],
        day=row[21],
    )


def _content(row: tuple) -> Content:
    return Content(
        disk_id=row[0],
        title=row[1],
        kind=_content_kind(row[2]),
        position=row[3],
        publisher=row[4],
        cracker=row[5],
        version=row[6],
        extra=row[7],
        source=row[8],
        id=row[9],
    )


def _image(row: tuple) -> ImageRecord:
    return ImageRecord(
        id=row[0],
        disk_id=row[1],
        name=row[2],
        format=row[3],
        flags=row[4],
        size=row[5],
        crc32=row[6],
        md5=row[7],
        sha1=row[8],
        sha512=row[9],
        bad=bool(row[10]),
        rank=row[11],
        source=row[12],
        virus=row[13],
    )


def _location(row: tuple) -> Location:
    return Location(
        id=row[0],
        disk_id=row[1],
        provider=row[2],
        url=row[3],
        image_id=row[4],
        container=row[5],
        member=row[6],
        size=row[7],
        hash_kind=row[8],
        hash_value=row[9],
        page_url=row[10],
        priority=row[11],
    )


def media_credit(template: str, page_url: str) -> str:
    """A credit line from its stored template: "{page}" is the page address
    without its scheme, and doubled braces are single ones."""
    try:
        return template.format(page=page_url.split("://", 1)[-1])
    except (IndexError, KeyError, ValueError):
        return template


def _chunks(values: Iterable[int]) -> Iterator[list[int]]:
    batch: list[int] = []
    for value in values:
        batch.append(value)
        if len(batch) == _CHUNK:
            yield batch
            batch = []
    if batch:
        yield batch


class Catalogue:
    """An open catalogue. Use ``Catalogue.open(path)``."""

    def __init__(self, connection: sqlite3.Connection, path: Path, built_at: str) -> None:
        self._connection = connection
        self._lock = threading.RLock()
        self._aliases: dict[str, tuple[str, ...]] | None = None
        self._hosted: dict[tuple[str, ...], frozenset[int]] = {}
        self._disk_sets: dict[str, None] = {}
        self._available: dict[tuple, tuple[str, int]] = {}
        self.path = path
        self.built_at = built_at

    @classmethod
    def open(cls, path: Path) -> Catalogue:
        """Open ``path`` read-only; raise ``CatalogueError`` if it is not usable."""
        path = Path(path)
        if not path.is_file():
            raise CatalogueError(f"{path} does not exist")
        uri = "file:" + urllib.parse.quote(str(path.resolve())) + "?mode=ro"
        try:
            connection = sqlite3.connect(uri, uri=True, check_same_thread=False)
            version = schema.schema_version(connection)
        except sqlite3.Error as error:
            raise CatalogueError(f"{path} cannot be read: {error}") from error
        problem = layout_problem(version)
        if problem:
            connection.close()
            raise CatalogueError(f"{path} {problem}.")
        row = connection.execute("SELECT value FROM meta WHERE key = 'built_at'").fetchone()
        return cls(connection, path, row[0] if row else "")

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def __enter__(self) -> Catalogue:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def query(
        self, sql: str, parameters: Iterable[object] | Mapping[str, object] = ()
    ) -> list[tuple]:
        """Run a read-only query under the catalogue lock. Used by search.

        ``parameters`` is a sequence for ``?`` placeholders or a mapping for
        ``:name`` ones.
        """
        values = parameters if isinstance(parameters, Mapping) else tuple(parameters)
        with self._lock:
            return self._connection.execute(sql, values).fetchall()

    # -- series ------------------------------------------------------------

    def series(self) -> list[SeriesModel]:
        aliases: dict[str, list[str]] = {}
        for alias, series_id in self.query("SELECT alias, series_id FROM series_alias"):
            aliases.setdefault(series_id, []).append(alias)
        rows = self.query(
            "SELECT s.id, s.name, s.platform, s.kind, s.group_name, COUNT(d.id) "
            "FROM series s LEFT JOIN disks d ON d.series_id = s.id "
            "GROUP BY s.id ORDER BY s.name COLLATE NOCASE"
        )
        return [
            SeriesModel(
                id=row[0],
                name=row[1],
                platform=_platform(row[2]),
                kind=_disk_kind(row[3]),
                group=row[4],
                aliases=tuple(sorted(aliases.get(row[0], ()))),
                disk_count=row[5],
            )
            for row in rows
        ]

    def aliases(self) -> dict[str, tuple[str, ...]]:
        """Normalised alias or series name -> ids of the series it names."""
        if self._aliases is None:
            found: dict[str, dict[str, None]] = {}
            for alias, series_id in self.query("SELECT alias, series_id FROM series_alias"):
                found.setdefault(normalise(alias), {})[series_id] = None
            for series_id, name in self.query("SELECT id, name FROM series"):
                found.setdefault(normalise(name), {})[series_id] = None
            self._aliases = {alias: tuple(ids) for alias, ids in found.items() if alias}
        return self._aliases

    # -- disks -------------------------------------------------------------

    def disk(self, disk_id: int) -> Disk | None:
        return self.disks([disk_id]).get(disk_id)

    def disks(self, ids: Iterable[int]) -> dict[int, Disk]:
        found: dict[int, Disk] = {}
        for batch in _chunks(dict.fromkeys(ids)):
            marks = ",".join("?" * len(batch))
            for row in self.query(
                f"SELECT {_DISK_COLUMNS} FROM disks d LEFT JOIN series s ON s.id = d.series_id "
                f"WHERE d.id IN ({marks})",
                batch,
            ):
                found[row[0]] = _disk(row)
        return found

    def contents(self, disk_id: int) -> list[Content]:
        rows = self.query(
            f"SELECT {_CONTENT_COLUMNS} FROM contents WHERE disk_id = ? ORDER BY position, id",
            (disk_id,),
        )
        return [_content(row) for row in rows]

    def content(self, content_id: int) -> Content | None:
        """One title on a disk, by ``Content.id``."""
        rows = self.query(f"SELECT {_CONTENT_COLUMNS} FROM contents WHERE id = ?", (content_id,))
        return _content(rows[0]) if rows else None

    def images(self, disk_id: int) -> list[ImageRecord]:
        rows = self.query(
            f"SELECT {_IMAGE_COLUMNS} FROM images WHERE disk_id = ? ORDER BY rank, id",
            (disk_id,),
        )
        return [_image(row) for row in rows]

    def image(self, image_id: int) -> ImageRecord | None:
        rows = self.query(f"SELECT {_IMAGE_COLUMNS} FROM images WHERE id = ?", (image_id,))
        return _image(rows[0]) if rows else None

    def locations(self, disk_id: int) -> list[Location]:
        rows = self.query(
            f"{_LOCATION_QUERY} WHERE l.disk_id = ? ORDER BY l.priority, l.id", (disk_id,)
        )
        return [_location(row) for row in rows]

    def links(self, disk_id: int) -> list[Link]:
        rows = self.query(
            "SELECT disk_id, label, url FROM links WHERE disk_id = ? ORDER BY rowid", (disk_id,)
        )
        return [Link(disk_id=row[0], label=row[1], url=row[2]) for row in rows]

    # -- details pane ----------------------------------------------------------

    def picture_addresses(self, platforms: Iterable[str] = ()) -> list[tuple[str, str]]:
        """Every picture address the catalogue lists, once, with its source.

        Only discs of ``platforms`` (Platform values) count, or every disc
        when none is given. The order is the discs' order in the catalogue, so
        a download that stops part way has covered whole discs.
        """
        wanted = [getattr(platform, "value", platform) for platform in platforms]
        where = f"WHERE d.platform IN ({', '.join('?' * len(wanted))}) " if wanted else ""
        rows = self.query(
            "SELECT u.text || m.url, MIN(c.source) FROM media m "
            "JOIN address_prefix u ON u.id = m.url_prefix "
            "JOIN media_credit c ON c.id = m.credit_id "
            f"JOIN disks d ON d.id = m.disk_id {where}"
            "GROUP BY u.text || m.url ORDER BY MIN(m.disk_id), MIN(m.id)",
            tuple(wanted),
        )
        return [(url, source) for url, source in rows]

    def media(self, disk_id: int) -> list[MediaItem]:
        """Pictures of a disk and of the titles on it, lowest rank first.

        A picture filed both for the disk and for its only title is returned
        once, as a picture of the disk. The addresses and the credit are
        stored compactly (schema.py) and come back whole.
        """
        rows = self.query(
            f"{_MEDIA_QUERY} WHERE m.disk_id = ? ORDER BY m.rank, m.content_id IS NOT NULL, m.id",
            (disk_id,),
        )
        found: dict[str, MediaItem] = {}
        for kind, url, source, credit, page_url, thumb_url, width, height, content_id in rows:
            page_url = page_url or ""
            found.setdefault(
                url,
                MediaItem(
                    kind=kind,
                    url=url,
                    source=source,
                    credit=media_credit(credit, page_url),
                    page_url=page_url,
                    thumb_url=thumb_url or "",
                    width=width,
                    height=height,
                    content_id=content_id,
                ),
            )
        return list(found.values())

    def trivia(self, disk_id: int) -> list[TriviaItem]:
        """Facts, notes and Wikipedia article titles for a disk and its titles.

        Rows about the disk come first, then rows about its titles in menu
        order. A "wikipedia" row carries the article title in ``text`` and
        ``title``; the application fetches the summary itself.
        """
        rows = self.query(
            "SELECT t.kind, t.text, t.source, t.url, t.licence, t.content_id FROM trivia t "
            "LEFT JOIN contents c ON c.id = t.content_id WHERE t.disk_id = ? "
            "ORDER BY t.content_id IS NOT NULL, c.position, t.id",
            (disk_id,),
        )
        return [
            TriviaItem(
                kind=row[0],
                text=row[1],
                source=row[2],
                url=row[3],
                licence=row[4],
                title=row[1] if row[0] == WIKIPEDIA else "",
                content_id=row[5],
            )
            for row in rows
        ]

    def crew_for_disk(self, disk_id: int) -> CrewInfo | None:
        """History and members of the crew that made a disk.

        The disk points at its crew row (``disks.crew_id``). Crew names are
        not unique across platforms or sources, so a crew is never looked up
        by name.
        """
        rows = self.query(
            "SELECT c.name, c.notes, c.members, c.founded, c.source, c.url, c.wikipedia "
            "FROM disks d JOIN crews c ON c.id = d.crew_id WHERE d.id = ?",
            (disk_id,),
        )
        if not rows:
            return None
        row = rows[0]
        members = tuple(member.strip() for member in row[2].split(",") if member.strip())
        return CrewInfo(
            name=row[0],
            notes=row[1],
            members=members,
            founded=row[3],
            source=row[4],
            url=row[5],
            wikipedia=row[6],
        )

    # -- matching ------------------------------------------------------------

    def match_image(
        self,
        *,
        md5: str = "",
        sha1: str = "",
        sha512: str = "",
        crc32: str = "",
        size: int | None = None,
    ) -> ImageRecord | None:
        """The catalogue image with one of these hashes, trying MD5, SHA-1,
        SHA-512 and then CRC32 with size. Good dumps win over bad ones, then
        the lowest rank."""
        attempts: list[tuple[str, tuple[object, ...]]] = []
        if md5:
            attempts.append(("md5 = ?", (md5.lower(),)))
        if sha1:
            attempts.append(("sha1 = ?", (sha1.lower(),)))
        if sha512:
            attempts.append(("sha512 = ?", (sha512.lower(),)))
        if crc32 and size is not None:
            attempts.append(("crc32 = ? AND size = ?", (crc32.lower(), size)))
        for condition, parameters in attempts:
            rows = self.query(
                f"SELECT {_IMAGE_COLUMNS} FROM images WHERE {condition} "
                "ORDER BY bad, rank, id LIMIT 1",
                parameters,
            )
            if rows:
                return _image(rows[0])
        return None

    def disks_with_locations(self, providers: Iterable[str]) -> set[int]:
        """Disks that one of ``providers`` hosts; worked out once per set of providers."""
        wanted = tuple(sorted(dict.fromkeys(providers)))
        if not wanted:
            return set()
        if wanted not in self._hosted:
            marks = ",".join("?" * len(wanted))
            rows = self.query(
                f"SELECT DISTINCT disk_id FROM locations WHERE provider IN ({marks})", wanted
            )
            self._hosted[wanted] = frozenset(row[0] for row in rows)
        return set(self._hosted[wanted])

    def batch(self) -> threading.RLock:
        """The catalogue lock, to hold across several queries that belong together."""
        return self._lock

    def available_set(
        self, local_disks: Iterable[int], providers: Iterable[str]
    ) -> tuple[str, int]:
        """The ``disk_set`` of disks with a local image or hosted by one of
        ``providers``, and how many disks it holds; ("", 0) when none.

        The last answer is remembered, so paging through "available only"
        results does not build the set again.
        """
        key = (frozenset(int(disk) for disk in local_disks), tuple(sorted(set(providers))))
        with self._lock:
            found = self._available.get(key)
            if found is not None and (not found[0] or self._touch(found[0])):
                return found
            ids = set(key[0]) | self.disks_with_locations(key[1])
            found = (self.disk_set(ids), len(ids)) if ids else ("", 0)
            self._available = {key: found}
            return found

    def _touch(self, table: str) -> bool:
        """Mark a disk set as just used; False when it has been dropped."""
        name = table.removeprefix("temp.")
        if name not in self._disk_sets:
            return False
        self._disk_sets[name] = self._disk_sets.pop(name)
        return True

    def disk_set(self, ids: Iterable[int]) -> str:
        """The name of a temporary table of ``ids`` (column ``id``), for SQL
        such as ``d.id IN <name>``; see ``temp_table``."""
        values = sorted({int(value) for value in ids})
        digest = hashlib.blake2b(array("q", values).tobytes(), digest_size=8).hexdigest()
        return self._temp_table(
            f"disk_set_{digest}", "id INTEGER PRIMARY KEY", [(value,) for value in values]
        )

    def temp_table(self, columns: str, rows: Iterable[tuple]) -> str:
        """The name of a temporary table holding ``rows``, for SQL such as a join.

        ``columns`` are the SQL column definitions, such as "id INTEGER
        PRIMARY KEY, crew TEXT". The table lives in this connection's
        temporary database, never in the catalogue file. The same rows get
        the same table, so rows used page after page are written once; the
        last ``DISK_SETS`` tables are kept. Hold ``batch()`` while using the
        name.
        """
        rows = list(rows)
        digest = hashlib.blake2b(repr((columns, rows)).encode(), digest_size=8).hexdigest()
        return self._temp_table(f"rows_{digest}", columns, rows)

    def _temp_table(self, name: str, columns: str, rows: list[tuple]) -> str:
        with self._lock:
            if self._touch(name):
                return f"temp.{name}"
            self._connection.execute(f"CREATE TEMP TABLE {name}({columns})")
            if rows:
                marks = ", ".join("?" * len(rows[0]))
                self._connection.executemany(f"INSERT INTO temp.{name} VALUES ({marks})", rows)
            self._disk_sets[name] = None
            while len(self._disk_sets) > DISK_SETS:
                oldest = next(iter(self._disk_sets))
                del self._disk_sets[oldest]
                self._connection.execute(f"DROP TABLE temp.{oldest}")
            self._connection.commit()
        return f"temp.{name}"

    def providers(self) -> list[str]:
        """Every provider that has at least one location, sorted."""
        return [row[0] for row in self.query("SELECT DISTINCT provider FROM locations ORDER BY 1")]

    # -- provenance ------------------------------------------------------------

    def stats(self) -> dict[str, int]:
        """Totals, and disk counts per platform, per kind and per both
        ("platform:amiga", "kind:menu", "amiga:menu")."""
        counts: dict[str, int] = {}
        for table in (
            "disks",
            "series",
            "contents",
            "images",
            "locations",
            "links",
            "entries",
            "media",
            "trivia",
            "crews",
        ):
            counts[table] = self.query(f"SELECT COUNT(*) FROM {table}")[0][0]
        for platform, kind, count in self.query(
            "SELECT platform, kind, COUNT(*) FROM disks GROUP BY platform, kind"
        ):
            counts[f"{platform}:{kind}"] = count
            counts[f"platform:{platform}"] = counts.get(f"platform:{platform}", 0) + count
            counts[f"kind:{kind}"] = counts.get(f"kind:{kind}", 0) + count
        counts["disks with contents"] = self.query("SELECT COUNT(DISTINCT disk_id) FROM contents")[
            0
        ][0]
        counts["disks with locations"] = self.query(
            "SELECT COUNT(DISTINCT disk_id) FROM locations"
        )[0][0]
        return counts

    def sources(self) -> list[dict[str, str]]:
        rows = self.query(
            "SELECT id, name, url, licence, retrieved, records FROM sources ORDER BY name"
        )
        keys = ("id", "name", "url", "licence", "retrieved", "records")
        return [{key: str(value) for key, value in zip(keys, row, strict=True)} for row in rows]

    def meta(self) -> dict[str, str]:
        return dict(self.query("SELECT key, value FROM meta"))


def locate_catalogue() -> Path | None:
    """The catalogue to open.

    ``PIRATEFINDER_CATALOGUE`` wins when set. Otherwise, of the candidates in
    ``paths.catalogue_candidates()`` that exist and open, the one built most
    recently; on a tie the earlier candidate.
    """
    explicit = os.environ.get("PIRATEFINDER_CATALOGUE", "")
    if explicit:
        return Path(explicit)
    best: tuple[str, Path] | None = None
    for candidate in paths.catalogue_candidates():
        try:
            with Catalogue.open(candidate) as catalogue:
                built_at = catalogue.built_at
        except (CatalogueError, sqlite3.Error):
            continue
        if best is None or built_at > best[0]:
            best = (built_at, candidate)
    return best[1] if best else None

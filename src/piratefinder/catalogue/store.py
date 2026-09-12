"""Read-only access to catalogue.sqlite.

The catalogue is opened with SQLite's read-only URI mode, so nothing the
application does can change it, and every query returns the value types from
``piratefinder.models``. One ``Catalogue`` may be shared by the interface and
worker threads; queries are serialised with a lock.
"""

from __future__ import annotations

import os
import sqlite3
import threading
import urllib.parse
from collections.abc import Iterable, Iterator
from pathlib import Path

from .. import paths
from ..models import Content, ContentKind, Disk, DiskKind, ImageRecord, Link, Location, Platform
from ..models import Series as SeriesModel
from . import schema
from .naming import normalise

_CHUNK = 500  # ids per IN (...) list, well below SQLite's variable limit

_DISK_COLUMNS = (
    "d.id, d.label, d.platform, d.kind, d.series_id, COALESCE(s.name, ''), d.number, d.part, "
    "d.version, d.title, d.date, d.publisher, d.cracker, d.condition, d.notes, d.credits, "
    "d.menu_text"
)
_IMAGE_COLUMNS = (
    "id, disk_id, name, format, flags, size, crc32, md5, sha1, sha512, bad, rank, source"
)
_LOCATION_COLUMNS = (
    "id, disk_id, provider, url, image_id, container, member, size, hash_kind, hash_value, "
    "page_url, priority"
)


class CatalogueError(RuntimeError):
    """The file is missing, is not a catalogue, or has an unsupported layout."""


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
        if version != schema.SCHEMA_VERSION:
            connection.close()
            raise CatalogueError(
                f"{path} has catalogue layout {version}; this version of PirateFinder "
                f"reads layout {schema.SCHEMA_VERSION}"
            )
        row = connection.execute("SELECT value FROM meta WHERE key = 'built_at'").fetchone()
        return cls(connection, path, row[0] if row else "")

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def __enter__(self) -> Catalogue:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def query(self, sql: str, parameters: Iterable[object] = ()) -> list[tuple]:
        """Run a read-only query under the catalogue lock. Used by search."""
        with self._lock:
            return self._connection.execute(sql, tuple(parameters)).fetchall()

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
            "SELECT disk_id, title, kind, position, publisher, cracker, version, extra, source "
            "FROM contents WHERE disk_id = ? ORDER BY position, id",
            (disk_id,),
        )
        return [
            Content(
                disk_id=row[0],
                title=row[1],
                kind=_content_kind(row[2]),
                position=row[3],
                publisher=row[4],
                cracker=row[5],
                version=row[6],
                extra=row[7],
                source=row[8],
            )
            for row in rows
        ]

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
            f"SELECT {_LOCATION_COLUMNS} FROM locations WHERE disk_id = ? ORDER BY priority, id",
            (disk_id,),
        )
        return [_location(row) for row in rows]

    def links(self, disk_id: int) -> list[Link]:
        rows = self.query(
            "SELECT disk_id, label, url FROM links WHERE disk_id = ? ORDER BY rowid", (disk_id,)
        )
        return [Link(disk_id=row[0], label=row[1], url=row[2]) for row in rows]

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
        wanted = list(dict.fromkeys(providers))
        if not wanted:
            return set()
        marks = ",".join("?" * len(wanted))
        rows = self.query(
            f"SELECT DISTINCT disk_id FROM locations WHERE provider IN ({marks})", wanted
        )
        return {row[0] for row in rows}

    def providers(self) -> list[str]:
        """Every provider that has at least one location, sorted."""
        return [row[0] for row in self.query("SELECT DISTINCT provider FROM locations ORDER BY 1")]

    # -- provenance ------------------------------------------------------------

    def stats(self) -> dict[str, int]:
        """Totals, and disk counts per platform, per kind and per both
        ("platform:amiga", "kind:menu", "amiga:menu")."""
        counts: dict[str, int] = {}
        for table in ("disks", "series", "contents", "images", "locations", "links"):
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

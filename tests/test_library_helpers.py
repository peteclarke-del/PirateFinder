"""Builders shared by the library, online, finder and job tests.

Everything here is synthetic: disk images are made from a seed at run time,
and the catalogue is a small database created with ``catalogue.schema``.
``SqlCatalogue`` is a minimal stand-in for ``catalogue.store.Catalogue`` that
reads that database, so these tests do not depend on the store module.
"""

from __future__ import annotations

import contextlib
import hashlib
import http.server
import sqlite3
import struct
import threading
import zlib
from collections.abc import Iterable
from pathlib import Path

from piratefinder.catalogue import schema
from piratefinder.catalogue.naming import normalise
from piratefinder.models import (
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

SECTOR = 512


def make_st_image(
    seed: str,
    *,
    label: str = "TESTDISK",
    files: Iterable[str] = ("GAME.PRG",),
    cylinders: int = 80,
    sides: int = 2,
    sectors: int = 9,
) -> bytes:
    """A FAT12 Atari ST sector image with a volume label and a few files."""
    total = cylinders * sides * sectors
    image = bytearray(total * SECTOR)
    boot = bytearray(SECTOR)
    boot[0:2] = b"\x60\x38"  # BRA.S, as TOS writes
    boot[8:11] = hashlib.md5(seed.encode()).digest()[:3]  # serial number
    struct.pack_into("<HBHBHHBHHH", boot, 11, SECTOR, 2, 1, 2, 112, total, 0xF9, 3, sectors, sides)
    image[0:SECTOR] = boot
    fat = bytes([0xF9, 0xFF, 0xFF, 0xFF, 0x0F, 0x00])
    for copy in range(2):
        start = (1 + copy * 3) * SECTOR
        image[start : start + len(fat)] = fat
    root = (1 + 2 * 3) * SECTOR
    entries = [_dir_entry(label.ljust(11)[:11], 0x08, 0, 0)]
    payload = f"PirateFinder test data {seed}".encode()
    for name in files:
        stem, _dot, extension = name.partition(".")
        entries.append(
            _dir_entry(stem.ljust(8)[:8] + extension.ljust(3)[:3], 0x00, 2, len(payload))
        )
    for index, entry in enumerate(entries):
        image[root + index * 32 : root + (index + 1) * 32] = entry
    data_start = root + 7 * SECTOR
    image[data_start : data_start + len(payload)] = payload
    # Make the rest of the disk depend on the seed so every image hashes differently.
    marker = hashlib.sha256(seed.encode()).digest()
    image[-len(marker) :] = marker
    return bytes(image)


def _dir_entry(name: str, attributes: int, cluster: int, size: int) -> bytes:
    entry = bytearray(32)
    entry[0:11] = name.encode("ascii")
    entry[11] = attributes
    struct.pack_into("<HI", entry, 26, cluster, size)
    return bytes(entry)


def make_msa(raw: bytes, *, sectors: int = 9, sides: int = 2) -> bytes:
    """An uncompressed MSA file holding ``raw``."""
    track = sectors * SECTOR
    tracks = len(raw) // (track * sides)
    out = bytearray(struct.pack(">HHHHH", 0x0E0F, sectors, sides - 1, 0, tracks - 1))
    for offset in range(0, len(raw), track):
        out += struct.pack(">H", track) + raw[offset : offset + track]
    return bytes(out)


def make_adf(seed: str) -> bytes:
    """A double-density AmigaDOS image that hashes differently per seed."""
    image = bytearray(80 * 2 * 11 * SECTOR)
    image[0:4] = b"DOS\x00"
    marker = hashlib.sha256(seed.encode()).digest()
    image[-len(marker) :] = marker
    return bytes(image)


def hashes(data: bytes) -> dict[str, str]:
    """CRC32, MD5, SHA-1 and SHA-512 of ``data``."""
    return {
        "crc32": f"{zlib.crc32(data) & 0xFFFFFFFF:08x}",
        "md5": hashlib.md5(data).hexdigest(),
        "sha1": hashlib.sha1(data).hexdigest(),
        "sha512": hashlib.sha512(data).hexdigest(),
    }


class CatalogueBuilder:
    """Writes a small catalogue database with the real schema."""

    def __init__(self, path: Path, built_at: str = "2026-09-01") -> None:
        self.path = Path(path)
        self.connection = sqlite3.connect(self.path)
        self.connection.execute("PRAGMA synchronous = OFF")  # a test file needs no fsync
        self.connection.execute("PRAGMA journal_mode = MEMORY")
        schema.create(self.connection)
        self.connection.execute("INSERT INTO meta(key, value) VALUES ('built_at', ?)", (built_at,))
        self._series: set[str] = set()

    def disk(
        self,
        disk_id: int,
        label: str,
        *,
        platform: Platform = Platform.ATARI_ST,
        kind: DiskKind = DiskKind.MENU,
        series: tuple[str, str] | None = None,
        number: int | None = None,
        contents: Iterable[str] = (),
        crew: str = "",
    ) -> None:
        """Add a disk with its contents in menu order."""
        series_id = None
        if series is not None:
            series_id, name = series
            if series_id not in self._series:
                self._series.add(series_id)
                self.connection.execute(
                    "INSERT INTO series(id, name, platform, kind) VALUES (?, ?, ?, ?)",
                    (series_id, name, str(platform), str(kind)),
                )
        self.connection.execute(
            """INSERT INTO disks(id, series_id, number, label, title, platform, kind, crew)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (disk_id, series_id, number, label, label, str(platform), str(kind), crew),
        )
        contents = list(contents)
        for position, title in enumerate(contents):
            self.connection.execute(
                "INSERT INTO contents(disk_id, position, title, kind) VALUES (?, ?, ?, ?)",
                (disk_id, position, title, "game"),
            )
        # The search indexes, filled the way the catalogue builder fills them.
        series_name = series[1] if series is not None else ""
        self.connection.execute(
            "INSERT INTO disk_fts(rowid, label, series, contents, people, notes) "
            "VALUES (?, ?, ?, ?, ?, '')",
            (disk_id, normalise(label), normalise(series_name), normalise(" ".join(contents)), ""),
        )
        self.connection.execute(
            "INSERT INTO disk_trigram(rowid, text) VALUES (?, ?)",
            (disk_id, normalise(" ".join([label, series_name, *contents]))),
        )

    def image(
        self,
        image_id: int,
        disk_id: int,
        name: str,
        *,
        data: bytes | None = None,
        hash_kinds: Iterable[str] = ("crc32", "md5", "sha1"),
        rank: int = 0,
        bad: bool = False,
        sha512: str = "",
        virus: str = "",
    ) -> None:
        """Add a dump; the chosen hashes are those of ``data`` (the raw sectors)."""
        values = hashes(data) if data is not None else {}
        kinds = set(hash_kinds)
        self.connection.execute(
            """INSERT INTO images(id, disk_id, name, format, size, crc32, md5, sha1, sha512,
                   bad, rank, virus) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                image_id,
                disk_id,
                name,
                Path(name).suffix.lstrip(".").lower(),
                len(data) if data is not None else None,
                values.get("crc32", "") if "crc32" in kinds else "",
                values.get("md5", "") if "md5" in kinds else "",
                values.get("sha1", "") if "sha1" in kinds else "",
                sha512,
                int(bad),
                rank,
                virus,
            ),
        )

    def location(
        self,
        location_id: int,
        disk_id: int,
        provider: str,
        url: str,
        *,
        image_id: int | None = None,
        container: str = "",
        member: str = "",
        hash_kind: str = "",
        hash_value: str = "",
        priority: int = 100,
    ) -> None:
        """Add a download location (the whole address after the empty prefix, see
        schema.py)."""
        self.connection.execute("INSERT OR IGNORE INTO address_prefix(id, text) VALUES (0, '')")
        self.connection.execute(
            """INSERT INTO locations(id, disk_id, image_id, provider, url_prefix, url, container,
                   member, hash_kind, hash_value, priority)
               VALUES (?, ?, ?, ?, 0, ?, ?, ?, ?, ?, ?)""",
            (
                location_id,
                disk_id,
                image_id,
                provider,
                url,
                container,
                member,
                hash_kind,
                hash_value,
                priority,
            ),
        )

    def media(
        self,
        disk_id: int,
        url: str,
        *,
        kind: str = "menu",
        source: str = "test",
        content_id: int | None = None,
        rank: int = 100,
    ) -> None:
        """Add a picture of a disk, or of one title on it (the whole address
        after the empty prefix, see schema.py)."""
        self.connection.execute("INSERT OR IGNORE INTO address_prefix(id, text) VALUES (0, '')")
        credit_id = self.connection.execute(
            "INSERT INTO media_credit(source, credit) VALUES (?, '')", (source,)
        ).lastrowid
        self.connection.execute(
            """INSERT INTO media(disk_id, content_id, kind, url_prefix, url, credit_id, rank)
               VALUES (?, ?, ?, 0, ?, ?, ?)""",
            (disk_id, content_id, kind, url, credit_id, rank),
        )

    def trivia(self, disk_id: int, kind: str, text: str, *, content_id: int | None = None) -> None:
        """Add a fact, a note or a Wikipedia article title."""
        self.connection.execute(
            "INSERT INTO trivia(disk_id, content_id, kind, text, source) VALUES (?, ?, ?, ?, ?)",
            (disk_id, content_id, kind, text, "test"),
        )

    def crew(
        self,
        name: str,
        *,
        disks: Iterable[int] = (),
        platform: Platform = Platform.ATARI_ST,
        notes: str = "",
        wikipedia: str = "",
    ) -> None:
        """Add a crew's history on one platform, as the history of ``disks``."""
        crew_id = self.connection.execute(
            "INSERT INTO crews(name, platform, notes, source, wikipedia) VALUES (?, ?, ?, ?, ?)",
            (name, str(platform), notes, "test", wikipedia),
        ).lastrowid
        self.connection.executemany(
            "UPDATE disks SET crew_id = ? WHERE id = ?", [(crew_id, disk) for disk in disks]
        )

    def content_id(self, disk_id: int, position: int) -> int:
        """The contents.id of one title."""
        return self.connection.execute(
            "SELECT id FROM contents WHERE disk_id = ? AND position = ?", (disk_id, position)
        ).fetchone()[0]

    def source(self, source_id: str, name: str) -> None:
        """Add a provenance row, which also names a provider."""
        self.connection.execute(
            "INSERT INTO sources(id, name, url) VALUES (?, ?, ?)", (source_id, name, "")
        )

    def close(self) -> Path:
        """Commit and close; return the catalogue path."""
        self.connection.commit()
        self.connection.close()
        return self.path


class SqlCatalogue:
    """The parts of ``catalogue.store.Catalogue`` the library and finder use."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._connection = sqlite3.connect(
            f"{self.path.resolve().as_uri()}?mode=ro", uri=True, check_same_thread=False
        )
        self._lock = threading.Lock()
        self.match_calls: list[dict[str, object]] = []
        row = self._query("SELECT value FROM meta WHERE key = 'built_at'")
        self.built_at = row[0][0] if row else ""

    def _query(self, sql: str, values: Iterable[object] = ()) -> list[tuple]:
        with self._lock:
            return self._connection.execute(sql, tuple(values)).fetchall()

    def query(self, sql: str, parameters: Iterable[object] = ()) -> list[tuple]:
        """A read-only query, as ``Catalogue.query`` runs it."""
        return self._query(sql, parameters)

    def close(self) -> None:
        self._connection.close()

    def disk(self, disk_id: int) -> Disk | None:
        return self.disks([disk_id]).get(disk_id)

    def disks(self, ids: Iterable[int]) -> dict[int, Disk]:
        result = {}
        for disk_id in ids:
            rows = self._query(
                """SELECT d.id, d.label, d.platform, d.kind, d.series_id,
                          COALESCE(s.name, ''), d.number, d.title, d.crew
                   FROM disks d LEFT JOIN series s ON s.id = d.series_id WHERE d.id = ?""",
                (disk_id,),
            )
            for row in rows:
                result[row[0]] = Disk(
                    id=row[0],
                    label=row[1],
                    platform=Platform(row[2]),
                    kind=DiskKind(row[3]),
                    series_id=row[4],
                    series_name=row[5],
                    number=row[6],
                    title=row[7],
                    crew=row[8],
                )
        return result

    def contents(self, disk_id: int) -> list[Content]:
        rows = self._query(
            "SELECT title, kind, position, id FROM contents WHERE disk_id = ? ORDER BY position",
            (disk_id,),
        )
        return [
            Content(disk_id, title, ContentKind(kind), position, id=content_id)
            for title, kind, position, content_id in rows
        ]

    def _image_rows(self, where: str, values: Iterable[object]) -> list[ImageRecord]:
        rows = self._query(
            f"""SELECT id, disk_id, name, format, flags, size, crc32, md5, sha1, sha512, bad,
                       rank, source, virus FROM images WHERE {where} ORDER BY rank, id""",
            values,
        )
        return [
            ImageRecord(
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
            for row in rows
        ]

    def images(self, disk_id: int) -> list[ImageRecord]:
        return self._image_rows("disk_id = ?", (disk_id,))

    def image(self, image_id: int) -> ImageRecord | None:
        found = self._image_rows("id = ?", (image_id,))
        return found[0] if found else None

    def locations(self, disk_id: int) -> list[Location]:
        rows = self._query(
            """SELECT l.id, l.disk_id, l.provider, p.text || l.url, l.image_id, l.container,
                      l.member, l.size, l.hash_kind, l.hash_value, l.page_url, l.priority
               FROM locations l JOIN address_prefix p ON p.id = l.url_prefix
               WHERE l.disk_id = ? ORDER BY l.priority, l.id""",
            (disk_id,),
        )
        return [Location(*row) for row in rows]

    def links(self, disk_id: int) -> list[Link]:
        rows = self._query("SELECT disk_id, label, url FROM links WHERE disk_id = ?", (disk_id,))
        return [Link(*row) for row in rows]

    def media(self, disk_id: int) -> list[MediaItem]:
        rows = self._query(
            """SELECT m.kind, p.text || m.url, c.source, m.content_id FROM media m
               JOIN address_prefix p ON p.id = m.url_prefix
               JOIN media_credit c ON c.id = m.credit_id WHERE m.disk_id = ?
               ORDER BY m.rank, m.id""",
            (disk_id,),
        )
        return [MediaItem(kind, url, source, content_id=cid) for kind, url, source, cid in rows]

    def trivia(self, disk_id: int) -> list[TriviaItem]:
        rows = self._query(
            "SELECT kind, text, source, content_id FROM trivia WHERE disk_id = ? ORDER BY id",
            (disk_id,),
        )
        return [TriviaItem(kind, text, source, content_id=cid) for kind, text, source, cid in rows]

    def crew_for_disk(self, disk_id: int) -> CrewInfo | None:
        rows = self._query(
            "SELECT c.name, c.notes, c.wikipedia FROM disks d JOIN crews c ON c.id = d.crew_id "
            "WHERE d.id = ?",
            (disk_id,),
        )
        return CrewInfo(rows[0][0], notes=rows[0][1], wikipedia=rows[0][2]) if rows else None

    def match_image(self, *, md5="", sha1="", sha512="", crc32="", size=None):
        self.match_calls.append(
            {
                key: value
                for key, value in {
                    "md5": md5,
                    "sha1": sha1,
                    "sha512": sha512,
                    "crc32": crc32,
                    "size": size,
                }.items()
                if value
            }
        )
        for column, value in (("md5", md5), ("sha1", sha1), ("sha512", sha512)):
            if value:
                found = self._image_rows(f"{column} = ?", (value.lower(),))
                if found:
                    return found[0]
        if crc32 and size:
            found = self._image_rows("crc32 = ? AND size = ?", (crc32.lower(), size))
            if found:
                return found[0]
        return None

    def disks_with_locations(self, providers: Iterable[str]) -> set[int]:
        providers = list(providers)
        if not providers:
            return set()
        marks = ", ".join("?" * len(providers))
        rows = self._query(
            f"SELECT DISTINCT disk_id FROM locations WHERE provider IN ({marks})", providers
        )
        return {disk_id for (disk_id,) in rows}

    def stats(self) -> dict[str, int]:
        return {
            "disks": self._query("SELECT COUNT(*) FROM disks")[0][0],
            "images": self._query("SELECT COUNT(*) FROM images")[0][0],
        }

    def sources(self) -> list[dict[str, str]]:
        rows = self._query("SELECT id, name, url FROM sources ORDER BY id")
        return [{"id": row[0], "name": row[1], "url": row[2]} for row in rows]


class QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        return None


@contextlib.contextmanager
def serve(handler: type[http.server.BaseHTTPRequestHandler]):
    """Run ``handler`` on a local port in a thread; yield the base URL."""
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, args=(0.05,), daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def store_module_available() -> bool:
    """Whether engineer B1's catalogue store exists yet."""
    try:
        from piratefinder.catalogue import store  # noqa: F401
    except ImportError:
        return False
    return True

"""Demo, music and intro packs from the Demozoo daily database export.

Demozoo publishes its whole database every day as a PostgreSQL dump
(``demozoo-export.sql.gz``, about 200 MB). Productions of the type "Pack" list
their members in order, which is exactly what is on a demo pack disk. This
importer reads the dump as a stream, keeps only the tables it needs from the
``COPY ... FROM stdin`` blocks, and emits every Amiga and Atari ST pack that
has members.

A pack is matched to a series with the rules in
``data/series/match-demozoo.toml`` against "<group> <title>", for example
"Skid Row Compact 130"; failing that, a title ending in a number is tried
against the names and aliases of the declared series of the same group. Packs
no series claims are kept as pack disks titled "<title> (<group>)".

The download is large, so the importer is off by default. A local copy can be
given with ``--input demozoo=<file>``.
"""

from __future__ import annotations

import gzip
import io
import re
from array import array
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import Path

from ..context import BuildContext
from ..records import ContentRecord, DiskRecord, SourceInfo, normalise_version
from ..series import SeriesMatch, SeriesRegistry, normalise

INFO = SourceInfo(
    id="demozoo",
    name="Demozoo",
    url="https://demozoo.org/",
    licence="",
)
CONTENT_PRIORITY = 35
DEFAULT_ENABLED = False

DUMP = "https://data.demozoo.org/demozoo-export.sql.gz"
PRODUCTION_URL = "https://demozoo.org/productions/{id}/"
MAX_AGE_DAYS = 30.0
# Demozoo platform names -> PirateFinder platforms.
PLATFORMS = {"Amiga OCS/ECS": "amiga", "Amiga AGA": "amiga", "Atari ST/E": "atari-st"}
PACK_TYPE = "pack"  # internal_name of the Pack production type
# Top-level Demozoo production types -> ContentKind; anything else is "other".
MEMBER_KINDS = {
    "demo": "demo",
    "intro": "intro",
    "invitation": "intro",
    "music": "music",
    "musicdisk": "music",
    "chip music pack": "music",
    "game": "game",
    "tool": "utility",
    "slideshow": "demo",
}
# A cracktro on a game compact stands for the cracked game that follows it, so
# on a disk of a "menu" series it is listed as a game, elsewhere as an intro.
CRACKTRO = "cracktro"
TABLES = {
    "platforms_platform": ("id", "name"),
    "productions_productiontype": ("id", "name", "path", "internal_name"),
    "productions_production": ("id", "title", "release_date_date", "release_date_precision"),
    "productions_production_platforms": ("production_id", "platform_id"),
    "productions_production_types": ("production_id", "productiontype_id"),
    "productions_packmember": ("pack_id", "member_id", "position"),
    "productions_production_author_nicks": ("production_id", "nick_id"),
    "demoscene_nick": ("id", "name"),
}

_COPY = re.compile(r"^COPY (?:\w+\.)?\"?(?P<table>\w+)\"? \((?P<columns>[^)]*)\) FROM stdin;")
_ESCAPE = re.compile(r"\\(?:(?P<octal>[0-7]{1,3})|x(?P<hex>[0-9A-Fa-f]{1,2})|(?P<char>.))")
_SIMPLE_ESCAPES = {"b": "\b", "f": "\f", "n": "\n", "r": "\r", "t": "\t", "v": "\v"}
_NUMBERED = re.compile(r"^(?P<base>.*?[A-Za-z!)])[\s_#.:-]*(?:no\.?\s*|vol\.?\s*)?(?P<number>\d+)$")


def _unescape(value: str) -> str | None:
    """A field of PostgreSQL's COPY text format; ``\\N`` is NULL."""
    if value == "\\N":
        return None
    if "\\" not in value:
        return value

    def replace(found: re.Match[str]) -> str:
        if found.group("octal"):
            return chr(int(found.group("octal"), 8))
        if found.group("hex"):
            return chr(int(found.group("hex"), 16))
        char = found.group("char")
        return _SIMPLE_ESCAPES.get(char, char)

    return _ESCAPE.sub(replace, value)


def copy_rows(lines: Iterable[str], wanted: dict[str, tuple[str, ...]]) -> Iterator[tuple]:
    """(table, row) for the wanted columns of every COPY block of the wanted tables."""
    table = None
    indexes: list[int] = []
    for line in lines:
        if table is None:
            found = _COPY.match(line)
            if found and found.group("table") in wanted:
                table = found.group("table")
                columns = [name.strip().strip('"') for name in found.group("columns").split(",")]
                indexes = [columns.index(name) for name in wanted[table]]
            continue
        line = line.rstrip("\n")
        if line == "\\.":
            table = None
            continue
        fields = line.split("\t")
        yield table, tuple(_unescape(fields[index]) for index in indexes)


@dataclass(slots=True)
class Dump:
    """The parts of the export this importer uses, kept compact."""

    platforms: dict[int, str] = field(default_factory=dict)
    types: dict[int, tuple[str, str, str]] = field(default_factory=dict)  # name, path, internal
    titles: dict[int, tuple[str, str]] = field(default_factory=dict)  # title, date
    production_platforms: array = field(default_factory=lambda: array("l"))
    production_types: array = field(default_factory=lambda: array("l"))
    pack_members: array = field(default_factory=lambda: array("l"))
    authors: array = field(default_factory=lambda: array("l"))
    nicks: dict[int, str] = field(default_factory=dict)


def _date(date: str | None, precision: str | None) -> str:
    if not date:
        return ""
    return {"y": date[:4], "m": date[:7]}.get(precision or "d", date)


def read_dump(lines: Iterable[str]) -> Dump:
    dump = Dump()
    for table, row in copy_rows(lines, TABLES):
        if table == "productions_production":
            dump.titles[int(row[0])] = (" ".join((row[1] or "").split()), _date(row[2], row[3]))
        elif table == "productions_production_platforms":
            dump.production_platforms.extend((int(row[0]), int(row[1])))
        elif table == "productions_production_types":
            dump.production_types.extend((int(row[0]), int(row[1])))
        elif table == "productions_packmember":
            dump.pack_members.extend((int(row[0]), int(row[1]), int(row[2] or 0)))
        elif table == "productions_production_author_nicks":
            dump.authors.extend((int(row[0]), int(row[1])))
        elif table == "demoscene_nick":
            dump.nicks[int(row[0])] = row[1] or ""
        elif table == "platforms_platform":
            dump.platforms[int(row[0])] = row[1] or ""
        elif table == "productions_productiontype":
            dump.types[int(row[0])] = (row[1] or "", row[2] or "", row[3] or "")
    return dump


def _pairs(values: array, width: int = 2) -> Iterator[tuple[int, ...]]:
    for index in range(0, len(values), width):
        yield tuple(values[index : index + width])


@dataclass(slots=True)
class Pack:
    id: int
    title: str
    date: str
    platform: str
    group: str
    members: list[ContentRecord]


def packs(dump: Dump) -> Iterator[Pack]:
    """Every pack with members on a wanted platform, members in pack order."""
    wanted_platforms = {
        pid: PLATFORMS[name] for pid, name in dump.platforms.items() if name in PLATFORMS
    }
    top_names = {path: name for name, path, _internal in dump.types.values()}
    type_kind: dict[int, str] = {}
    pack_types = set()
    for type_id, (name, path, internal) in dump.types.items():
        if internal == PACK_TYPE:
            pack_types.add(type_id)
        top = top_names.get(path[:4], name).lower()
        type_kind[type_id] = (
            CRACKTRO if name.lower() == CRACKTRO else MEMBER_KINDS.get(top, "other")
        )
    platform_of: dict[int, str] = {}
    for production, platform in _pairs(dump.production_platforms):
        if platform in wanted_platforms:
            platform_of.setdefault(production, wanted_platforms[platform])
    kind_of: dict[int, str] = {}
    is_pack: set[int] = set()
    for production, type_id in _pairs(dump.production_types):
        if type_id in pack_types:
            is_pack.add(production)
        kind_of.setdefault(production, type_kind.get(type_id, "other"))
    members: dict[int, list[tuple[int, int]]] = {}
    for pack, member, position in _pairs(dump.pack_members, 3):
        if pack in is_pack and pack in platform_of:
            members.setdefault(pack, []).append((position, member))
    authors: dict[int, list[str]] = {}
    for production, nick in _pairs(dump.authors):
        if production in members and nick in dump.nicks:
            authors.setdefault(production, []).append(dump.nicks[nick])
    for pack_id, listed in sorted(members.items()):
        title, date = dump.titles.get(pack_id, ("", ""))
        contents = []
        for _position, member in sorted(listed):
            member_title = dump.titles.get(member, ("", ""))[0]
            if member_title:
                contents.append(
                    ContentRecord(title=member_title, kind=kind_of.get(member, "other"))
                )
        if title and contents:
            group = " & ".join(authors.get(pack_id, []))
            yield Pack(pack_id, title, date, platform_of[pack_id], group, contents)


def identify(series: SeriesRegistry, pack: Pack) -> SeriesMatch | None:
    text = f"{pack.group} {pack.title}".strip()
    found = series.match(INFO.id, text, pack.platform)
    if found is not None:
        return found if found.number is not None else None
    numbered = _NUMBERED.match(pack.title.strip())
    if numbered is None:
        return None
    base = numbered.group("base").strip()
    candidate = series.by_name(f"{pack.group} {base}", pack.platform)
    if candidate is None:
        candidate = series.by_name(base, pack.platform)
        groups = {normalise(name) for name in pack.group.split(" & ")}
        if candidate is None or normalise(candidate.group) not in groups:
            return None
    return SeriesMatch(candidate.id, int(numbered.group("number")), "", "")


def _version(version: str) -> str:
    """One spelling per version, as the TOSEC importer keeps it: "V2.0" -> "v2"."""
    return re.sub(r"(?:\.0+)+$", "", normalise_version(version)) if version else ""


def records_from_dump(ctx: BuildContext, dump: Dump) -> Iterator[DiskRecord]:
    keyed = loose = 0
    for pack in packs(dump):
        found = identify(ctx.series, pack)
        definition = ctx.series.get(found.series_id) if found else None
        if found:
            keyed += 1
        else:
            loose += 1
        title = f"{pack.title} ({pack.group})" if pack.group else pack.title
        kind = definition.kind if definition else "pack"
        for content in pack.members:
            if content.kind == CRACKTRO:
                content.kind = "game" if kind == "menu" else "intro"
        yield DiskRecord(
            source=INFO.id,
            platform=pack.platform,
            kind=kind,
            series_key=found.series_id if found else "",
            number=found.number if found else None,
            part=found.part if found else "",
            version=_version(found.version) if found else "",
            title="" if found else title,
            date=pack.date,
            publisher=pack.group,
            contents=pack.members,
            links=[("Demozoo", PRODUCTION_URL.format(id=pack.id))],
        )
    ctx.log(f"demozoo: {keyed} packs in a series, {loose} other packs")


def _lines(path: Path) -> Iterator[str]:
    opener = gzip.open if path.suffix == ".gz" else open
    with (
        opener(path, "rb") as raw,
        io.TextIOWrapper(raw, encoding="utf-8", errors="replace") as text,
    ):
        yield from text


def collect(ctx: BuildContext) -> Iterator[DiskRecord]:
    path = ctx.input(INFO.id) or ctx.fetch(DUMP, max_age_days=MAX_AGE_DAYS)
    yield from records_from_dump(ctx, read_dump(_lines(path)))

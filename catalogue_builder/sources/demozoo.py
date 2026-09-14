"""Demo, music and intro packs from the Demozoo daily database export.

Demozoo publishes its whole database every day as a PostgreSQL dump
(``demozoo-export.sql.gz``, about 200 MB). Productions of the type "Pack" list
their members in order, which is exactly what is on a demo pack disk. This
importer reads the dump as a stream, keeps only the tables it needs from the
``COPY ... FROM stdin`` blocks, and emits every Amiga and Atari ST pack that
has members.

A pack is matched to a series with the rules in
``data/series/match-demozoo.toml`` against "<group> <title>", for example
"Skid Row Compact 130"; failing that, a title ending in a number (and
perhaps a part letter and a version) is tried against the names and aliases
of the declared series of the same group. Packs no series claims are kept as
pack disks titled "<title> (<group>)".

Demozoo also lists the menus of many crews as intros ("Automation CD #198
intro", "Pompey Pirates Menu #001 V2 Intro"). An intro whose title, without
the trailing "intro", names a numbered disk of a menu series in the same way
gives that disk its screenshot, release date, notes and Demozoo link; such
a record lists no contents.

For the details pane every record carries the production's release date
with Demozoo's precision, its notes as plain text, and its screenshots (the
pack's or menu's own as disc pictures, one of each member of a pack as a
picture of that title), and the Demozoo ids of the groups credited as its
authors (``crew_ids``). ``collect_crews`` gives the notes, members and
English Wikipedia article of every group with Amiga or Atari ST
productions, one record per Demozoo group with its id and the number of its
productions on each platform, named as the catalogue names the crew when a
series group or a crew in ``data/groups.toml`` has the same name. Demozoo has
many groups of one name ("Awesome" on the Amiga is not "Awesome" on the ST),
so the records are never merged by name here.

Demozoo also names the file of many packs and menus on the scene archives
(its download links). A pack or menu carries these as download locations:
the amigascne archive through its scene.org mirror, the main scene.org
archive, and Fujiology for the Atari ST, never ftp.untergrund.net, whose
robots.txt forbids robots. Only disk images and zips are taken. A Fujiology
zip smaller than a disk image holds only the intro program, not the disk, so
the builder reads Fujiology's listing of each folder for the file sizes and
leaves such zips out. A pack that lists no members but has a download is
kept as well, as a disk named by its title. The merge joins a record to the
disk that already has its download (``merge.Merger.disk_for_url``), so a
pack the amigascne importer found under another name is one disk.

The download is large, so the importer is off by default. A local copy can be
given with ``--input demozoo=<file>``.
"""

from __future__ import annotations

import gzip
import html
import io
import json
import re
import urllib.parse
from array import array
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import Path

from piratefinder.catalogue.naming import normalise

from ..context import BuildContext, OfflineError
from ..records import (
    FUJIOLOGY,
    SCENE_ORG,
    ContentRecord,
    CrewRecord,
    DiskRecord,
    LocationRecord,
    MediaRecordIn,
    SourceInfo,
    TriviaRecordIn,
    normalise_version,
)
from ..series import GroupRegistry, SeriesDef, SeriesMatch, SeriesRegistry, crew_key
from . import amigascne

INFO = SourceInfo(
    id="demozoo",
    name="Demozoo",
    url="https://demozoo.org/",
    licence="No licence stated (daily public dump)",
)
CONTENT_PRIORITY = 35
DEFAULT_ENABLED = False

DUMP = "https://data.demozoo.org/demozoo-export.sql.gz"
PRODUCTION_URL = "https://demozoo.org/productions/{id}/"
GROUP_URL = "https://demozoo.org/groups/{id}/"
CREDIT = "Demozoo contributors, demozoo.org/productions/{id}/"
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
# Media kind of a screenshot of a pack member, by the member's content kind.
MEMBER_MEDIA_KINDS = {
    CRACKTRO: "intro",
    "intro": "intro",
    "game": "snap",
    "utility": "snap",
}
MEMBER_MEDIA_DEFAULT = "demo"
MEDIA_RANK = 15
SCREENSHOTS_PER_PRODUCTION = 3
# An intro that is the menu of a numbered disk: its title ends in one of these.
_MENU_INTRO = re.compile(r"^(?P<title>.*?\S)\s+(?:intro|cracktro)$", re.IGNORECASE)
WIKIPEDIA_LINK = "WikipediaPage"
ENGLISH_WIKIPEDIA = re.compile(r"^https?://en\.(?:m\.)?wikipedia\.org/wiki/(?P<title>[^?#]+)")
TABLES = {
    "platforms_platform": ("id", "name"),
    "productions_productiontype": ("id", "name", "path", "internal_name"),
    "productions_production": (
        "id",
        "title",
        "release_date_date",
        "release_date_precision",
        "notes",
    ),
    "productions_production_platforms": ("production_id", "platform_id"),
    "productions_production_types": ("production_id", "productiontype_id"),
    "productions_packmember": ("pack_id", "member_id", "position"),
    "productions_production_author_nicks": ("production_id", "nick_id"),
    "productions_screenshot": (
        "production_id",
        "original_url",
        "original_width",
        "original_height",
        "standard_url",
        "standard_width",
        "standard_height",
        "thumbnail_url",
    ),
    "demoscene_nick": ("id", "name", "releaser_id"),
    "demoscene_releaser": ("id", "name", "is_group", "notes"),
    "demoscene_membership": ("member_id", "group_id", "is_current"),
    "demoscene_releaserexternallink": ("link_class", "parameter", "releaser_id"),
    "productions_productionlink": ("link_class", "parameter", "production_id", "is_download_link"),
}

# The hosts of the download links a pack or menu may carry: Demozoo's link
# class -> (provider id, address the link's path is added to).
DOWNLOAD_HOSTS = {
    "AmigascneFile": ("amigascne", amigascne.BASE),
    "SceneOrgFile": (SCENE_ORG.id, "https://ftp.scene.org/pub/"),
    "FujiologyFile": (FUJIOLOGY.id, "https://fujiology.org/"),
}
# File types a download may be: a disk image the application writes, or a zip of one.
DOWNLOAD_CONTAINERS = {".adf": "", ".adz": "", ".dms": "", ".st": "", ".msa": "", ".zip": "zip"}
# A Fujiology zip smaller than this holds an intro program, not a disk image.
DISK_SIZE_MINIMUM = 300_000
DOWNLOAD_PRIORITY = 60  # after the archives the other importers read
LISTING_MAX_AGE = 30.0

_COPY = re.compile(r"^COPY (?:\w+\.)?\"?(?P<table>\w+)\"? \((?P<columns>[^)]*)\) FROM stdin;")
_ESCAPE = re.compile(r"\\(?:(?P<octal>[0-7]{1,3})|x(?P<hex>[0-9A-Fa-f]{1,2})|(?P<char>.))")
_SIMPLE_ESCAPES = {"b": "\b", "f": "\f", "n": "\n", "r": "\r", "t": "\t", "v": "\v"}
_NUMBERED = re.compile(
    r"^(?P<base>.*?[A-Za-z!)])[\s_#.:-]*(?i:no\.?\s*|vol\.?\s*)?(?P<number>\d+)"
    r"(?:\s*(?i:part\s*)?(?P<part>[A-Za-z]))??(?:\s+[vV](?P<version>\d+(?:\.\d+)?))?$"
)


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


# --- plain text ------------------------------------------------------------

_HTML_LINK = re.compile(r"<a\s[^>]*>(?P<text>.*?)</a>", re.IGNORECASE | re.DOTALL)
_HTML_BREAK = re.compile(r"<br\s*/?>|</p>\s*<p[^>]*>", re.IGNORECASE)
_HTML_TAG = re.compile(r"</?[A-Za-z][^>]*>")
_MD_IMAGE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_MD_LINK = re.compile(r"\[(?P<text>[^\]]*)\]\((?P<url>[^)\s]*)(?:\s+\"[^\"]*\")?\)")
_MD_FOOTNOTE = re.compile(r"\[\^(?P<label>[^\]]+)\]")
_MD_EMPHASIS = re.compile(r"(\*\*|__)(?P<text>\S(?:.*?\S)?)\1|(?<!\w)\*(?P<one>\S(?:.*?\S)?)\*")
_MD_HEADING = re.compile(r"^#{1,6}\s+", re.MULTILINE)
_BLANK_LINES = re.compile(r"\n{3,}")


def plain_text(text: str | None) -> str:
    """Demozoo notes, written in Markdown with some HTML, as plain text.

    Links keep their text, footnote marks become "[1]", emphasis and
    heading marks go, line breaks are kept and runs of blank lines shortened.
    """
    if not text:
        return ""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = _HTML_LINK.sub(lambda found: found.group("text"), text)
    text = _HTML_TAG.sub("", _HTML_BREAK.sub("\n", text))
    text = _MD_LINK.sub(
        lambda found: found.group("text") or found.group("url"), _MD_IMAGE.sub("", text)
    )
    text = _MD_FOOTNOTE.sub(lambda found: f"[{found.group('label')}]", text)
    text = _MD_EMPHASIS.sub(lambda found: found.group("text") or found.group("one"), text)
    text = html.unescape(_MD_HEADING.sub("", text)).replace("\u00a0", " ")
    lines = [line.rstrip() for line in text.split("\n")]
    return _BLANK_LINES.sub("\n\n", "\n".join(lines)).strip()


# --- reading the dump ------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Screenshot:
    url: str
    thumb_url: str
    width: int | None
    height: int | None


@dataclass(slots=True)
class Dump:
    """The parts of the export this importer uses, kept compact."""

    platforms: dict[int, str] = field(default_factory=dict)
    types: dict[int, tuple[str, str, str]] = field(default_factory=dict)  # name, path, internal
    titles: dict[int, tuple[str, str]] = field(default_factory=dict)  # title, date
    notes: dict[int, str] = field(default_factory=dict)  # production -> notes as written
    production_platforms: array = field(default_factory=lambda: array("l"))
    production_types: array = field(default_factory=lambda: array("l"))
    pack_members: array = field(default_factory=lambda: array("l"))
    authors: array = field(default_factory=lambda: array("l"))
    nicks: dict[int, str] = field(default_factory=dict)
    nick_releasers: dict[int, int] = field(default_factory=dict)
    screenshots: dict[int, list[Screenshot]] = field(default_factory=dict)
    releasers: dict[int, str] = field(default_factory=dict)
    groups: set[int] = field(default_factory=set)
    group_notes: dict[int, str] = field(default_factory=dict)
    memberships: array = field(default_factory=lambda: array("l"))  # member, group, current
    wikipedia: dict[int, str] = field(default_factory=dict)  # releaser -> article title
    downloads: dict[int, list[tuple[str, str]]] = field(default_factory=dict)  # link class, path


def _date(date: str | None, precision: str | None) -> str:
    if not date:
        return ""
    return {"y": date[:4], "m": date[:7]}.get(precision or "d", date)


def _size(value: str | None) -> int | None:
    return int(value) if value and value.isdigit() else None


def _wanted_platform_ids(dump: Dump) -> set[int]:
    return {pid for pid, name in dump.platforms.items() if name in PLATFORMS}


def read_dump(lines: Iterable[str]) -> Dump:
    dump = Dump()
    # Screenshots are kept only for productions on a wanted platform. The
    # export lists platforms before screenshots, so they are known by then;
    # a dump in another order keeps every screenshot instead.
    on_platform: set[int] | None = None
    for table, row in copy_rows(lines, TABLES):
        if table == "productions_production":
            dump.titles[int(row[0])] = (" ".join((row[1] or "").split()), _date(row[2], row[3]))
            if row[4] and row[4].strip():
                dump.notes[int(row[0])] = row[4]
        elif table == "productions_production_platforms":
            dump.production_platforms.extend((int(row[0]), int(row[1])))
        elif table == "productions_screenshot":
            production = int(row[0])
            if on_platform is None and dump.production_platforms:
                wanted = _wanted_platform_ids(dump)
                on_platform = {
                    production
                    for production, platform in _pairs(dump.production_platforms)
                    if platform in wanted
                }
            if on_platform is not None and production not in on_platform:
                continue
            shots = dump.screenshots.setdefault(production, [])
            url = row[4] or row[1] or ""
            if url and len(shots) < SCREENSHOTS_PER_PRODUCTION:
                standard = bool(row[4])
                shots.append(
                    Screenshot(
                        url=url,
                        thumb_url=row[7] or "",
                        width=_size(row[5] if standard else row[2]),
                        height=_size(row[6] if standard else row[3]),
                    )
                )
        elif table == "productions_production_types":
            dump.production_types.extend((int(row[0]), int(row[1])))
        elif table == "productions_packmember":
            dump.pack_members.extend((int(row[0]), int(row[1]), int(row[2] or 0)))
        elif table == "productions_production_author_nicks":
            dump.authors.extend((int(row[0]), int(row[1])))
        elif table == "demoscene_nick":
            dump.nicks[int(row[0])] = row[1] or ""
            if row[2]:
                dump.nick_releasers[int(row[0])] = int(row[2])
        elif table == "demoscene_releaser":
            dump.releasers[int(row[0])] = row[1] or ""
            if row[2] == "t":
                dump.groups.add(int(row[0]))
                if row[3] and row[3].strip():
                    dump.group_notes[int(row[0])] = row[3]
        elif table == "demoscene_membership":
            dump.memberships.extend((int(row[0]), int(row[1]), 1 if row[2] == "t" else 0))
        elif table == "demoscene_releaserexternallink":
            found = ENGLISH_WIKIPEDIA.match(row[1] or "") if row[0] == WIKIPEDIA_LINK else None
            if found and row[2]:
                title = urllib.parse.unquote(found.group("title")).replace("_", " ").strip()
                dump.wikipedia.setdefault(int(row[2]), title)
        elif table == "productions_productionlink":
            if row[3] == "t" and row[0] in DOWNLOAD_HOSTS and row[1] and row[2]:
                dump.downloads.setdefault(int(row[2]), []).append((row[0], row[1]))
        elif table == "platforms_platform":
            dump.platforms[int(row[0])] = row[1] or ""
        elif table == "productions_productiontype":
            dump.types[int(row[0])] = (row[1] or "", row[2] or "", row[3] or "")
    return dump


def _pairs(values: array, width: int = 2) -> Iterator[tuple[int, ...]]:
    for index in range(0, len(values), width):
        yield tuple(values[index : index + width])


# --- packs and menu intros -------------------------------------------------


@dataclass(slots=True)
class Pack:
    id: int
    title: str
    date: str
    platform: str
    group: str
    members: list[ContentRecord]
    member_ids: list[int] = field(default_factory=list)


class _Index:
    """Platforms, kinds and authors of every production, worked out once."""

    def __init__(self, dump: Dump) -> None:
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
        self.platform_of: dict[int, str] = {}  # the first wanted platform listed
        self.platforms_of: dict[int, set[str]] = {}  # every wanted platform
        for production, platform in _pairs(dump.production_platforms):
            if platform in wanted_platforms:
                self.platform_of.setdefault(production, wanted_platforms[platform])
                self.platforms_of.setdefault(production, set()).add(wanted_platforms[platform])
        self.kind_of: dict[int, str] = {}
        self.is_pack: set[int] = set()
        for production, type_id in _pairs(dump.production_types):
            if type_id in pack_types:
                self.is_pack.add(production)
            self.kind_of.setdefault(production, type_kind.get(type_id, "other"))
        self.authors: dict[int, list[str]] = {}
        self.author_groups: dict[int, list[int]] = {}
        for production, nick in _pairs(dump.authors):
            if production in self.platform_of and nick in dump.nicks:
                self.authors.setdefault(production, []).append(dump.nicks[nick])
                releaser = dump.nick_releasers.get(nick)
                if releaser is not None and releaser in dump.groups:
                    self.author_groups.setdefault(production, []).append(releaser)

    def crew_ids(self, production: int) -> list[str]:
        """The Demozoo ids of the groups credited as authors of a production."""
        return [str(group) for group in dict.fromkeys(self.author_groups.get(production, []))]


def packs(dump: Dump, index: _Index | None = None) -> Iterator[Pack]:
    """Every pack with members on a wanted platform, members in pack order."""
    index = index or _Index(dump)
    members: dict[int, list[tuple[int, int]]] = {}
    for pack, member, position in _pairs(dump.pack_members, 3):
        if pack in index.is_pack and pack in index.platform_of:
            members.setdefault(pack, []).append((position, member))
    # A pack that lists no members is a disk all the same when it can be downloaded.
    for pack in dump.downloads:
        if pack in index.is_pack and pack in index.platform_of:
            members.setdefault(pack, [])
    for pack_id, listed in sorted(members.items()):
        title, date = dump.titles.get(pack_id, ("", ""))
        contents = []
        member_ids = []
        for _position, member in sorted(listed):
            member_title = dump.titles.get(member, ("", ""))[0]
            if member_title:
                contents.append(
                    ContentRecord(title=member_title, kind=index.kind_of.get(member, "other"))
                )
                member_ids.append(member)
        if title and (contents or pack_id in dump.downloads):
            group = " & ".join(index.authors.get(pack_id, []))
            yield Pack(
                pack_id, title, date, index.platform_of[pack_id], group, contents, member_ids
            )


def menu_intros(dump: Dump, index: _Index) -> Iterator[Pack]:
    """Intros and cracktros whose title, without "intro", may name a menu disk."""
    for production, platform in sorted(index.platform_of.items()):
        if production in index.is_pack or index.kind_of.get(production) not in ("intro", CRACKTRO):
            continue
        title, date = dump.titles.get(production, ("", ""))
        found = _MENU_INTRO.match(title)
        if found is None:
            continue
        group = " & ".join(index.authors.get(production, []))
        yield Pack(production, found.group("title"), date, platform, group, [])


# Words that call a numbered release a menu disk of the crew that made it:
# "CD 001 intro" by The World's Picture Collection is that crew's menu 1.
MENU_WORDS = frozenset(
    {"cd", "menu", "menu disk", "menu disc", "compact", "compact disk", "compact disc", "disk"}
)


def _crew_menus(series: SeriesRegistry, pack: Pack) -> SeriesDef | None:
    """The menu series named after a crew credited with ``pack``, or None."""
    for group in pack.group.split(" & "):
        for name in dict.fromkeys((group.strip(), re.sub(r"(?i)^the\s+", "", group.strip()))):
            candidate = series.by_name(name, pack.platform)
            if candidate is not None and candidate.kind == "menu":
                return candidate
    return None


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
    if candidate is None and normalise(base) in MENU_WORDS:
        candidate = _crew_menus(series, pack)
    if candidate is None:
        candidate = series.by_name(base, pack.platform)
        groups = {crew_key(name) for name in pack.group.split(" & ")}
        if candidate is None or crew_key(candidate.group) not in groups:
            return None
    return SeriesMatch(
        candidate.id,
        int(numbered.group("number")),
        (numbered.group("part") or "").upper(),
        numbered.group("version") or "",
    )


def _version(version: str) -> str:
    """One spelling per version, as the TOSEC importer keeps it: "V2.0" -> "v2"."""
    return re.sub(r"(?:\.0+)+$", "", normalise_version(version)) if version else ""


def _media(
    production: int, shot: Screenshot, kind: str, rank: int, content_title: str = ""
) -> MediaRecordIn:
    return MediaRecordIn(
        kind=kind,
        url=shot.url,
        source=INFO.id,
        thumb_url=shot.thumb_url,
        width=shot.width,
        height=shot.height,
        credit=CREDIT.format(id=production),
        page_url=PRODUCTION_URL.format(id=production),
        rank=rank,
        content_title=content_title,
    )


def _extras(dump: Dump, record: DiskRecord, production: int, kind: str) -> None:
    """The production's screenshots, as pictures of the disc, and its notes."""
    for offset, shot in enumerate(dump.screenshots.get(production, [])):
        record.media.append(_media(production, shot, kind, MEDIA_RANK + offset))
    notes = plain_text(dump.notes.get(production))
    if notes:
        record.trivia.append(
            TriviaRecordIn(
                kind="note",
                text=notes,
                source=INFO.id,
                url=PRODUCTION_URL.format(id=production),
            )
        )


class _Sizes:
    """File sizes from Fujiology's folder listings, fetched once per folder."""

    def __init__(self, ctx: BuildContext) -> None:
        self.ctx = ctx
        self.folders: dict[str, dict[str, int] | None] = {}

    def size(self, path: str) -> int | None:
        folder, _slash, name = path.lstrip("/").rpartition("/")
        if folder not in self.folders:
            url = FUJIOLOGY.url + urllib.parse.quote(f"{folder}/")
            try:
                text = self.ctx.fetch_text(
                    url,
                    name="listing.json",
                    max_age_days=LISTING_MAX_AGE,
                    headers={"Accept": "application/json"},
                )
                entries = json.loads(text)
                self.folders[folder] = {
                    str(entry.get("name", "")).casefold(): int(entry.get("size") or 0)
                    for entry in entries
                    if isinstance(entry, dict)
                }
            except (OSError, OfflineError, ValueError) as error:
                self.ctx.log(f"demozoo: no Fujiology listing of {folder}: {error}")
                self.folders[folder] = None
        found = self.folders[folder]
        return None if found is None else found.get(name.casefold())


def download_locations(
    links: Iterable[tuple[str, str]], sizes: _Sizes | None = None
) -> list[LocationRecord]:
    """Download locations for Demozoo's download links of one production.

    Only disk images and zips on the hosts in ``DOWNLOAD_HOSTS`` are taken,
    and a Fujiology zip only when its listing shows it is the size of a disk.
    """
    found: dict[str, LocationRecord] = {}
    for link_class, path in links:
        provider, base = DOWNLOAD_HOSTS[link_class]
        suffix = Path(path).suffix.lower()
        if suffix not in DOWNLOAD_CONTAINERS:
            continue
        size = None
        if provider == FUJIOLOGY.id:
            size = sizes.size(path) if sizes is not None else None
            if size is None or size < DISK_SIZE_MINIMUM:
                continue
        url = base + urllib.parse.quote(path.lstrip("/"))
        found.setdefault(
            url,
            LocationRecord(
                provider=provider,
                url=url,
                container=DOWNLOAD_CONTAINERS[suffix],
                size=size,
                priority=DOWNLOAD_PRIORITY,
            ),
        )
    return list(found.values())


def records_from_dump(ctx: BuildContext, dump: Dump) -> Iterator[DiskRecord]:
    index = _Index(dump)
    sizes = _Sizes(ctx)
    keyed = loose = pictured = located = 0
    for pack in packs(dump, index):
        found = identify(ctx.series, pack)
        definition = ctx.series.get(found.series_id) if found else None
        if found:
            keyed += 1
        else:
            loose += 1
        title = f"{pack.title} ({pack.group})" if pack.group else pack.title
        kind = definition.kind if definition else "pack"
        member_kinds = [content.kind for content in pack.members]
        for content in pack.members:
            if content.kind == CRACKTRO:
                content.kind = "game" if kind == "menu" else "intro"
        record = DiskRecord(
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
            release_date=pack.date,
            crew_ids=index.crew_ids(pack.id),
            locations=download_locations(dump.downloads.get(pack.id, ()), sizes),
        )
        located += bool(record.locations)
        _extras(dump, record, pack.id, "menu")
        seen: set[str] = set()
        for content, member, member_kind in zip(
            pack.members, pack.member_ids, member_kinds, strict=True
        ):
            content.links.append(("demozoo", str(member)))
            shots = dump.screenshots.get(member)
            if shots and content.title not in seen:
                seen.add(content.title)
                media_kind = MEMBER_MEDIA_KINDS.get(member_kind, MEMBER_MEDIA_DEFAULT)
                record.media.append(_media(member, shots[0], media_kind, MEDIA_RANK, content.title))
        pictured += bool(record.media)
        yield record
    menus = 0
    for intro in menu_intros(dump, index):
        found = identify(ctx.series, intro)
        definition = ctx.series.get(found.series_id) if found else None
        if found is None or definition is None or definition.kind != "menu":
            continue
        menus += 1
        record = DiskRecord(
            source=INFO.id,
            platform=intro.platform,
            kind=definition.kind,
            series_key=found.series_id,
            number=found.number,
            part=found.part,
            version=_version(found.version),
            date=intro.date,
            links=[("Demozoo", PRODUCTION_URL.format(id=intro.id))],
            release_date=intro.date,
            crew_ids=index.crew_ids(intro.id),
            locations=download_locations(dump.downloads.get(intro.id, ()), sizes),
        )
        located += bool(record.locations)
        _extras(dump, record, intro.id, "menu")
        pictured += bool(record.media)
        yield record
    ctx.log(
        f"demozoo: {keyed} packs in a series, {loose} other packs, {menus} menu intros; "
        f"{pictured} with screenshots, {located} with a download"
    )


# --- crews -----------------------------------------------------------------


def crew_records(
    dump: Dump, series: SeriesRegistry, groups: GroupRegistry | None = None
) -> list[CrewRecord]:
    """Every group with Amiga or Atari ST productions that Demozoo knows about.

    One record per group, with its Demozoo id and the number of productions
    it is credited with on each platform. The name is the catalogue's
    spelling when a series group or a crew in ``data/groups.toml`` matches it
    ("The Medway Boys" is "Medway Boys"), otherwise Demozoo's. Groups with no
    notes, members or article are left out.
    """
    index = _Index(dump)
    released: dict[int, dict[str, int]] = {}
    for production, releasers in index.author_groups.items():
        for group in dict.fromkeys(releasers):
            counts = released.setdefault(group, {})
            for platform in index.platforms_of.get(production, ()):
                counts[platform] = counts.get(platform, 0) + 1
    ours = {
        crew_key(part): part.strip()
        for definition in series.all()
        for part in definition.group.split(" / ")
        if part.strip()
    }
    members: dict[int, list[tuple[int, str]]] = {}
    for member, group, current in _pairs(dump.memberships, 3):
        if group in released and dump.releasers.get(member):
            members.setdefault(group, []).append((0 if current else 1, dump.releasers[member]))
    found: list[CrewRecord] = []
    for group in sorted(released):
        own = dump.releasers.get(group, "")
        notes = plain_text(dump.group_notes.get(group))
        people = list(dict.fromkeys(name for _order, name in sorted(members.get(group, []))))
        article = dump.wikipedia.get(group, "")
        if not own or not (notes or people or article):
            continue
        found.append(
            CrewRecord(
                name=ours.get(crew_key(own)) or (groups.expand_one(own) if groups else own),
                source=INFO.id,
                notes=notes,
                members=people,
                url=GROUP_URL.format(id=group),
                wikipedia=article,
                id=str(group),
                platforms=dict(sorted(released[group].items())),
            )
        )
    return found


def _lines(path: Path) -> Iterator[str]:
    opener = gzip.open if path.suffix == ".gz" else open
    with (
        opener(path, "rb") as raw,
        io.TextIOWrapper(raw, encoding="utf-8", errors="replace") as text,
    ):
        yield from text


# The export takes a minute to read, so the crews found while collecting
# disks are kept for collect_crews, by the file they came from.
_CREWS: dict[Path, list[CrewRecord]] = {}


def _dump_path(ctx: BuildContext) -> Path:
    return ctx.input(INFO.id) or ctx.fetch(DUMP, max_age_days=MAX_AGE_DAYS)


def collect(ctx: BuildContext) -> Iterator[DiskRecord]:
    path = _dump_path(ctx)
    dump = read_dump(_lines(path))
    _CREWS.clear()
    _CREWS[path] = crew_records(dump, ctx.series, ctx.groups)
    yield from records_from_dump(ctx, dump)


def collect_crews(ctx: BuildContext) -> Iterator[CrewRecord]:
    path = _dump_path(ctx)
    crews = _CREWS.get(path)
    if crews is None:
        crews = crew_records(read_dump(_lines(path)), ctx.series, ctx.groups)
    ctx.log(
        f"demozoo: {len(crews)} crews, {sum(1 for crew in crews if crew.notes)} with notes, "
        f"{sum(1 for crew in crews if crew.wikipedia)} with a Wikipedia article"
    )
    yield from crews

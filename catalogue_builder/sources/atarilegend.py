"""Atari Legend menu disks from the site's weekly MariaDB dump.

Atari Legend publishes a dump of its database every week under
``/data/database-dumps/`` with the users table removed, under CC BY-NC-SA 4.0.
The menu tables describe some 6,900 Atari ST menu disks: the set (a crew or a
series), the menu number, issue and version, the disk part, its condition and
scroll text, what is on it, and the SHA-512 of the MSA dump the site serves.

The dump is read as a stream. Only the tables listed in ``TABLES`` are kept,
and each INSERT statement is tokenised a line at a time, so memory use stays
proportional to the rows kept rather than to the size of the dump.

Each menu disk becomes one DiskRecord keyed so that it merges with the TOSEC
disk of the same menu:

- Series: the rules in ``data/series/match-atari-legend.toml`` are tried on
  the site's label for the menu ("The Medway Boys #20"), then the series
  whose name or alias is the set name; a set that names no series becomes a
  series of its own.
- Number: the menu number. Menus with only an issue letter, or with neither,
  have no number and so no key.
- Version: TOSEC leaves the first edition unversioned, so "1" becomes "",
  "bis" and "ter" become "2" and "3", and other versions are kept.
- Part: letters and single words are kept; other spellings such as "_4A" in
  Pompey Pirates 13 become the letter of the disk's position in the menu,
  which is TOSEC's "Disk 4 of 7".

The disk record carries the condition, notes, scroll text, contents in menu
order, the MSA dump with its SHA-512, the download location and a link to
the disk on its set page.

For the details pane it also carries the menu's release date, the site's
screenshots of the menu (a disc picture) and of each game on it (up to
three title pictures per game), the site's facts about each game and the
editors' notes on the disk. Each game is linked to its Atari Legend id and,
where Wikidata knows it (``wikidata.articles``), to its English Wikipedia
article, and each disk lists the site's ids of the crews of its menu set
(``crew_ids``). ``collect_crews`` gives the history and members of every
crew, one record per crew of the site with its id, named as the catalogue
names the crew of its menus. The site covers the Atari ST only, so every
crew is an ST crew; its release count is the number of menu disks of its
sets and of game releases it is credited with.
"""

from __future__ import annotations

import gzip
import io
import re
import urllib.error
from collections.abc import Iterable, Iterator, Mapping
from pathlib import Path

from ..context import BuildContext, OfflineError
from ..records import (
    ContentRecord,
    CrewRecord,
    DiskRecord,
    ImageRecordIn,
    LocationRecord,
    MediaRecordIn,
    SourceInfo,
    TriviaRecordIn,
)
from ..series import SeriesDef, SeriesRegistry, crew_key, slug
from . import wikidata

INFO = SourceInfo(
    id="atari-legend",
    name="Atari Legend",
    url="https://www.atarilegend.com",
    licence="CC BY-NC-SA 4.0",
)
CONTENT_PRIORITY = 10

SITE = "https://www.atarilegend.com"
DUMPS_URL = f"{SITE}/data/database-dumps/"
PLATFORM = "atari-st"
# The menu set page lists this many disks per page (Laravel paginator).
DISKS_PER_PAGE = 20
# Pictures the site stores, by the id of their row and its file extension.
MENU_SCREENSHOT_URL = SITE + "/storage/images/menu_screenshots/{id}.{ext}"
GAME_SCREENSHOT_URL = SITE + "/storage/images/game_screenshots/{id}.{ext}"
GAME_URL = SITE + "/games/{slug}"
PICTURE_EXTENSIONS = frozenset({"png", "jpg", "jpeg", "gif", "bmp"})
SCREENSHOT_CREDIT = f"Screenshot: Atari Legend (atarilegend.com), {INFO.licence}"
MENU_RANK = 10
GAME_RANK = 30
GAME_SCREENSHOTS = 3  # at most this many pictures of one game

TABLES = frozenset(
    {
        "menu_sets",
        "menus",
        "menu_disks",
        "menu_disk_contents",
        "menu_disk_dumps",
        "menu_disk_conditions",
        "menu_disk_screenshots",
        "menu_software",
        "menu_software_content_types",
        "games",
        "game_akas",
        "game_facts",
        "game_releases",
        "game_release_akas",
        "game_release_crew",
        "pub_devs",
        "individuals",
        "individual_nicks",
        "crews",
        "crew_individual",
        "crew_menu_set",
        "screenshots",
        "screenshot_game",
        "trainer_options",
        "game_release_trainer_option",
    }
)

# --- reading the dump ------------------------------------------------------

_CREATE = re.compile(r"^CREATE TABLE `(?P<table>[^`]+)` \(")
_COLUMN = re.compile(r"^\s+`(?P<column>[^`]+)` ")
_INSERT = re.compile(
    r"^(?:INSERT(?:\s+IGNORE)?|REPLACE)\s+INTO\s+`(?P<table>[^`]+)`\s*"
    r"(?:\((?P<columns>[^)]*)\))?\s*VALUES\s*",
    re.IGNORECASE,
)
# The VALUES of a statement that is not wanted: text up to the closing
# semicolon, stepping over quoted strings so a semicolon inside one is kept.
_SKIP = re.compile(r"(?:[^';]++|'(?:[^'\\]++|\\.|'')*+')*+", re.DOTALL)
_TOKEN = re.compile(
    r"""\s*(?:
        '(?P<string>(?:[^'\\]++|\\.|'')*+)'
      | (?P<hex>0x[0-9A-Fa-f]*)
      | (?P<number>[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?)
      | (?P<word>[A-Za-z_][A-Za-z0-9_]*)
      | (?P<punct>[(),;])
    )""",
    re.VERBOSE | re.DOTALL,
)
_ESCAPES = {
    "0": "\0",
    "b": "\b",
    "n": "\n",
    "r": "\r",
    "t": "\t",
    "Z": "\x1a",
}
_ESCAPE = re.compile(r"\\(.)|''", re.DOTALL)


def _unescape(text: str) -> str:
    def replace(found: re.Match[str]) -> str:
        char = found.group(1)
        if char is None:
            return "'"
        if char in ("%", "_"):
            return "\\" + char  # MySQL keeps the backslash before these two
        return _ESCAPES.get(char, char)

    return _ESCAPE.sub(replace, text) if ("\\" in text or "''" in text) else text


def _number(text: str) -> int | float:
    try:
        return int(text)
    except ValueError:
        return float(text)


class DumpError(ValueError):
    """The dump does not have the layout this importer understands."""


def open_dump(path: Path) -> io.TextIOBase:
    """Open a ``.sql`` or ``.sql.gz`` dump as text, whatever its suffix says."""
    with path.open("rb") as probe:
        magic = probe.read(2)
    if magic == b"\x1f\x8b":
        return io.TextIOWrapper(gzip.open(path, "rb"), encoding="utf-8", errors="replace")
    return path.open("r", encoding="utf-8", errors="replace")


def iter_rows(lines: Iterable[str], tables: Iterable[str]) -> Iterator[tuple[str, dict]]:
    """Yield ``(table, row)`` for every row inserted into one of ``tables``.

    Column names come from the preceding ``CREATE TABLE`` statement or from
    the column list of the ``INSERT`` itself, so a dump whose columns have
    been reordered or extended still reads correctly. Values are ``None`` for
    NULL, ``int`` or ``float`` for numbers and ``str`` otherwise.
    """
    wanted = frozenset(tables)
    columns: dict[str, list[str]] = {}
    creating: str | None = None
    parser: _ValuesParser | _Skipper | None = None
    for line in lines:
        if parser is not None:
            yield from parser.feed(line)
            if parser.done:
                parser = None
            continue
        if creating is not None:
            column = _COLUMN.match(line)
            if column:
                columns[creating].append(column.group("column"))
            elif line.startswith(")"):
                creating = None
            continue
        if line.startswith("CREATE TABLE"):
            found = _CREATE.match(line)
            if found and found.group("table") in wanted:
                creating = found.group("table")
                columns[creating] = []
            continue
        if not line[:7].upper().startswith(("INSERT", "REPLACE")):
            continue
        found = _INSERT.match(line)
        if found is None:
            continue
        table = found.group("table")
        if table not in wanted:
            parser = _Skipper()
        elif found.group("columns"):
            names = [name.strip().strip("`") for name in found.group("columns").split(",")]
        elif table in columns:
            names = columns[table]
        else:
            raise DumpError(f"INSERT into {table} before its CREATE TABLE")
        if table in wanted:
            parser = _ValuesParser(table, names)
        yield from parser.feed(line[found.end() :])
        if parser.done:
            parser = None


class _Skipper:
    """Reads past the VALUES of an INSERT into a table that is not wanted.

    Only quotes are tracked, which is much cheaper than tokenising, and a
    string that runs over several lines is held back like in _ValuesParser.
    """

    def __init__(self) -> None:
        self.done = False
        self._pending = ""

    def feed(self, text: str) -> Iterator[tuple[str, dict]]:
        text = self._pending + text
        self._pending = ""
        end = _SKIP.match(text).end()
        if end < len(text):
            if text[end] == ";":
                self.done = True
            else:
                self._pending = text[end:]  # a string continues on the next line
        return iter(())


class _ValuesParser:
    """Tokenises the VALUES list of one INSERT statement, fed a line at a time.

    A quoted string may run over several lines; the unfinished text is held
    back and retried with the next line. Everything else is a single token
    that cannot span a line in a dump produced by mysqldump or mariadb-dump.
    """

    def __init__(self, table: str, columns: list[str]) -> None:
        self.table = table
        self.columns = columns
        self.done = False
        self._pending = ""
        self._row: list | None = None

    def feed(self, text: str) -> Iterator[tuple[str, dict]]:
        text = self._pending + text
        self._pending = ""
        position = 0
        length = len(text)
        while position < length:
            if text[position].isspace():
                position += 1
                continue
            token = _TOKEN.match(text, position)
            if token is None:
                if text[position] == "'":
                    self._pending = text[position:]  # string continues on the next line
                    return
                raise DumpError(f"{self.table}: unexpected text {text[position : position + 40]!r}")
            position = token.end()
            kind = token.lastgroup
            if kind == "punct":
                char = token.group("punct")
                if char == "(":
                    if self._row is not None:
                        raise DumpError(f"{self.table}: nested parenthesis")
                    self._row = []
                elif char == ")":
                    if self._row is None:
                        raise DumpError(f"{self.table}: unbalanced parenthesis")
                    row, self._row = self._row, None
                    if len(row) != len(self.columns):
                        raise DumpError(
                            f"{self.table}: {len(row)} values for {len(self.columns)} columns"
                        )
                    yield self.table, dict(zip(self.columns, row, strict=True))
                elif char == ";":
                    self.done = True
                    return
                continue
            if self._row is None:
                if kind == "word":
                    continue  # ON DUPLICATE KEY and similar trailers are ignored
                raise DumpError(f"{self.table}: value outside a row")
            if kind == "string":
                self._row.append(_unescape(token.group("string")))
            elif kind == "number":
                self._row.append(_number(token.group("number")))
            elif kind == "hex":
                digits = token.group("hex")[2:]
                digits = "0" * (len(digits) % 2) + digits
                self._row.append(bytes.fromhex(digits).decode("utf-8", "replace"))
            else:
                word = token.group("word")
                upper = word.upper()
                if upper == "NULL":
                    self._row.append(None)
                elif upper in ("TRUE", "FALSE"):
                    self._row.append(1 if upper == "TRUE" else 0)
                elif word.startswith("_"):
                    continue  # charset introducer such as _binary before a string
                else:
                    self._row.append(word)


def load_tables(path: Path, tables: Iterable[str] = TABLES) -> dict[str, list[dict]]:
    """Every row of ``tables`` in the dump at ``path``, grouped by table."""
    result: dict[str, list[dict]] = {table: [] for table in tables}
    with open_dump(path) as handle:
        for table, row in iter_rows(handle, result):
            result[table].append(row)
    return result


# --- fetching --------------------------------------------------------------

_DUMP_LINK = re.compile(r'href="(?P<name>(?P<date>\d{4}-\d{2}-\d{2})\.sql(?:\.gz)?)"')


def latest_dump_name(listing: str) -> str | None:
    """The newest full dump named in the directory listing, by its date."""
    found = {m.group("date"): m.group("name") for m in _DUMP_LINK.finditer(listing)}
    return found[max(found)] if found else None


def fetch_latest_dump(ctx: BuildContext) -> Path:
    listing = ctx.fetch_text(DUMPS_URL, name="database-dumps.html", max_age_days=7)
    name = latest_dump_name(listing)
    if name is None:
        raise DumpError(f"no database dump listed at {DUMPS_URL}")
    # A dump is named by its date and never changes, so a cached copy stays valid.
    return ctx.fetch(DUMPS_URL + name, max_age_days=3650)


def _tables(ctx: BuildContext) -> dict[str, list[dict]]:
    path = ctx.input(INFO.id) or fetch_latest_dump(ctx)
    ctx.log(f"{INFO.id}: reading {path.name}")
    return load_tables(path)


def _articles(ctx: BuildContext) -> Mapping[str, str]:
    """Atari Legend game id -> English Wikipedia article, empty when unavailable."""
    try:
        return wikidata.articles(ctx).by_atari_legend
    except (OfflineError, urllib.error.URLError, OSError, ValueError) as error:
        ctx.log(f"{INFO.id}: no Wikipedia articles for games: {error}")
        return {}


def collect(ctx: BuildContext) -> Iterator[DiskRecord]:
    tables = _tables(ctx)
    yield from build_records(tables, ctx.series, log=ctx.log, articles=_articles(ctx))


def collect_crews(ctx: BuildContext) -> Iterator[CrewRecord]:
    yield from crew_records(_tables(ctx), ctx.series, log=ctx.log, groups=ctx.groups)


# --- turning rows into records ---------------------------------------------

# Words the site's editors use in the free-text subtype of a menu entry, and
# the content kind each one means. The first rule that matches wins.
_SUBTYPE_KINDS = (
    (re.compile(r"\btrainer", re.IGNORECASE), "trainer"),
    (re.compile(r"\b(?:cheat|codes?|passwords?)\b", re.IGNORECASE), "cheat"),
    (
        re.compile(
            r"\b(?:docs?|documentation|manual|instructions|solutions?|sol|tips|hints|maps?|"
            r"walkthrough)\b",
            re.IGNORECASE,
        ),
        "doc",
    ),
    (re.compile(r"\bintro\b", re.IGNORECASE), "intro"),
    (re.compile(r"\bmusic\b", re.IGNORECASE), "music"),
    (re.compile(r"\beditor\b", re.IGNORECASE), "utility"),
)
# The same for the names of the site's menu software content types.
_SOFTWARE_KINDS = (
    (re.compile(r"game", re.IGNORECASE), "game"),
    (re.compile(r"demo", re.IGNORECASE), "demo"),
    (re.compile(r"music", re.IGNORECASE), "music"),
    (re.compile(r"doc|zine", re.IGNORECASE), "doc"),
    (re.compile(r"utilit|source", re.IGNORECASE), "utility"),
    (re.compile(r"intro", re.IGNORECASE), "intro"),
)
# Disk conditions, in the order they are tested against the site's names.
_CONDITIONS = (
    (re.compile(r"missing", re.IGNORECASE), "missing"),
    (re.compile(r"intro only", re.IGNORECASE), "intro only"),
    (re.compile(r"damaged", re.IGNORECASE), "damaged"),
    (re.compile(r"intact", re.IGNORECASE), "intact"),
)
# Latin ordinals the site uses for the second and third edition of a menu.
_ORDINAL_VERSIONS = {"bis": "2", "ter": "3", "quater": "4"}
_FIRST_VERSION = re.compile(r"^v?0*1(?:\.0+)?$", re.IGNORECASE)


def _kind(rules: tuple[tuple[re.Pattern[str], str], ...], text: str | None) -> str | None:
    if text:
        for pattern, kind in rules:
            if pattern.search(text):
                return kind
    return None


def map_condition(name: str | None) -> str:
    return _kind(_CONDITIONS, name) or ""


def map_version(version: str | None) -> str:
    """The menu version as TOSEC spells it.

    TOSEC leaves the first edition of a menu unversioned and calls the next
    one v2 ("Pompey Pirates Menu Disk 001" and "... 001 v2.0"), where Atari
    Legend has "#1 v1" and "#1 v2", or "#41" and "#41 bis". So version 1 maps
    to "", the Latin ordinals to their numbers, and anything else is kept.
    """
    text = (version or "").strip()
    if not text or _FIRST_VERSION.match(text):
        return ""
    return _ORDINAL_VERSIONS.get(text.lower(), text)


def map_parts(parts: list[str | None]) -> list[str]:
    """Disk parts of one menu, in site order, as merge keys.

    Letters and single words ("A", "demo") are kept, since TOSEC spells those
    parts the same way ("Delicious Disk 100 Demo"). Any other spelling
    ("_4A", ".5", "part II B") becomes the letter of the disk's position in
    the menu, which is how TOSEC numbers the disks of a menu ("Disk 4 of 7"
    is D). A disk with no part keeps none: it is the menu's main disk.
    """
    texts = [(part or "").strip() for part in parts]
    if all(text.isalpha() or not text for text in texts):
        kept = [text.upper() for text in texts]
        if len(set(kept)) == len(kept):
            return kept
    if len(texts) == 1:
        return [""]
    return [
        "" if not text else chr(ord("A") + index) if index < 26 else str(index + 1)
        for index, text in enumerate(texts)
    ]


def menu_label(menu: dict) -> str:
    """The site's own label for a menu: "#13", "B" or "#[no number]", then "v2"."""
    if menu.get("number") is not None:
        label = f"#{menu['number']}"
    elif menu.get("issue"):
        label = str(menu["issue"])
    else:
        label = "#[no number]"
    if menu.get("version"):
        label += f" v{menu['version']}"
    return label


def _text(value: object) -> str:
    return str(value).replace("\r\n", "\n").replace("\r", "\n").strip() if value else ""


# The BBCode the site's editors write in facts and crew histories.
_BB_LINK = re.compile(r"\[url=(?P<url>[^\]]*)\](?P<text>.*?)\[/url\]", re.IGNORECASE | re.DOTALL)
_BB_PICTURE = re.compile(r"\[img[^\]]*\].*?\[/img\]", re.IGNORECASE | re.DOTALL)
_BB_TAG = re.compile(r"\[/?(?:b|i|u|s|code|quote|game|url|size|color|list|\*)(?:=[^\]]*)?\]", re.I)
_BLANK_LINES = re.compile(r"\n{3,}")


def plain_text(value: object) -> str:
    """Site text with its BBCode turned into plain text.

    A link keeps its text, with the address after it in brackets when the
    text is not the address itself; pictures (smileys) are dropped and every
    other tag is removed, keeping what it enclosed.
    """

    def link(found: re.Match[str]) -> str:
        url, text = found.group("url").strip(), found.group("text").strip()
        return text if not url or text == url else f"{text} ({url})" if text else url

    text = _text(value).replace("\\'", "'").replace("\u00a0", " ")
    text = _BB_TAG.sub("", _BB_PICTURE.sub("", _BB_LINK.sub(link, text)))
    lines = [line.rstrip() for line in text.split("\n")]
    return _BLANK_LINES.sub("\n\n", "\n".join(lines)).strip()


def release_date(value: object) -> str:
    """A menu's date as a release date, "" when it is missing or not a real date."""
    text = str(value or "")
    found = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", text)
    if found is None or found.group(1) == "0000":
        return ""
    if found.group(2) == "00":
        return found.group(1)
    return text if found.group(3) != "00" else text[:7]


def _asc(value: object) -> tuple[bool, object]:
    """Sort key for MariaDB ascending order: NULL first, text without case."""
    if value is None:
        return (False, 0)
    return (True, value.casefold() if isinstance(value, str) else value)


def site_order(disks: list[dict], menus: dict[int, dict], descending: bool) -> list[dict]:
    """The disks of one set in the order the set page lists them.

    The site orders by number and issue (in the set's direction), then version
    and part ascending. NULL sorts first ascending and last descending.
    """
    ordered = sorted(disks, key=lambda disk: disk["id"])
    ordered.sort(key=lambda disk: _asc(disk.get("part")))
    ordered.sort(key=lambda disk: _asc(menus[disk["menu_id"]].get("version")))
    ordered.sort(key=lambda disk: _asc(menus[disk["menu_id"]].get("issue")), reverse=descending)
    ordered.sort(key=lambda disk: _asc(menus[disk["menu_id"]].get("number")), reverse=descending)
    return ordered


def set_url(set_id: int) -> str:
    return f"{SITE}/menusets/{set_id}"


def disk_url(set_id: int, disk_id: int, index: int) -> str:
    """Link to a disk on its set page; ``index`` is its position in site order."""
    page = index // DISKS_PER_PAGE + 1
    query = f"?page={page}" if page > 1 else ""
    return f"{set_url(set_id)}{query}#menudisk-{disk_id}"


def dump_url(dump_id: int) -> str:
    return f"{SITE}/storage/zips/menus/{dump_id}.zip"


class _SeriesResolver:
    """Finds the series of each menu.

    The rules in ``match-atari-legend.toml`` are tried on the menu's full label
    as the site writes it ("Flame Of Finland/Superior #55"), so a set that
    holds two numbered series can be split by number. A menu no rule
    recognises goes to the series named like its set, and a set that names no
    known series is registered as a series of its own.
    """

    def __init__(self, registry: SeriesRegistry) -> None:
        self.registry = registry
        self._by_set: dict[int, tuple[SeriesDef, str]] = {}
        self.counts = {"rule": 0, "name": 0, "registered": 0}

    def resolve(
        self, set_id: int, set_name: str, label: str, crews: list[str]
    ) -> tuple[SeriesDef, int | None]:
        """The menu's series, and its number when the rule that matched says so."""
        found = self.registry.match(INFO.id, f"{set_name} {label}", platform=PLATFORM)
        series = self.registry.get(found.series_id) if found is not None else None
        if found is not None and series is not None:
            self.counts["rule"] += 1
            return series, found.number
        if set_id not in self._by_set:
            series = self.registry.by_name(set_name, PLATFORM)
            if series is not None:
                self._by_set[set_id] = (series, "name")
            else:
                self._by_set[set_id] = (self._register(set_name, crews), "registered")
        series, how = self._by_set[set_id]
        self.counts[how] += 1
        return series, None

    def _register(self, name: str, crews: list[str]) -> SeriesDef:
        ident = slug(name)
        existing = self.registry.get(ident)
        if existing is not None:
            if existing.platform == PLATFORM and existing.name == name:
                return existing
            ident = f"{ident}-st"
        return self.registry.add(
            SeriesDef(id=ident, name=name, platform=PLATFORM, kind="menu", group=" / ".join(crews))
        )


def _index(tables: dict[str, list[dict]]) -> dict[str, dict[int, dict]]:
    return {
        name: {row["id"]: row for row in tables.get(name, ()) if "id" in row}
        for name in (
            "menu_sets",
            "menus",
            "menu_disk_dumps",
            "menu_disk_conditions",
            "menu_software",
            "menu_software_content_types",
            "games",
            "game_releases",
            "pub_devs",
            "crews",
            "individuals",
            "individual_nicks",
            "screenshots",
            "trainer_options",
        )
    }


def build_records(
    tables: dict[str, list[dict]],
    registry: SeriesRegistry,
    log=lambda message: None,
    articles: Mapping[str, str] | None = None,
) -> Iterator[DiskRecord]:
    """One DiskRecord per menu disk in the dump tables.

    ``articles`` maps an Atari Legend game id (as text) to the title of its
    English Wikipedia article.
    """
    by_id = _index(tables)
    menus = by_id["menus"]
    akas: dict[tuple[str, int], list[str]] = {}
    for row in tables.get("game_akas", ()):
        akas.setdefault(("game", row["game_id"]), []).append(_text(row["name"]))
    for row in tables.get("game_release_akas", ()):
        akas.setdefault(("release", row["game_release_id"]), []).append(_text(row["name"]))
    release_crews: dict[int, list[str]] = {}
    for row in tables.get("game_release_crew", ()):
        crew = by_id["crews"].get(row["crew_id"])
        if crew:
            release_crews.setdefault(row["game_release_id"], []).append(_text(crew["name"]))
    trainers: dict[int, list[str]] = {}
    for row in tables.get("game_release_trainer_option", ()):
        option = by_id["trainer_options"].get(row["trainer_option_id"])
        if option:
            trainers.setdefault(row["game_release_id"], []).append(_text(option["name"]))
    set_crews: dict[int, list[str]] = {}
    set_crew_ids: dict[int, list[str]] = {}
    for row in sorted(tables.get("crew_menu_set", ()), key=lambda row: row["crew_id"]):
        crew = by_id["crews"].get(row["crew_id"])
        if crew:
            set_crews.setdefault(row["menu_set_id"], []).append(_text(crew["name"]))
            set_crew_ids.setdefault(row["menu_set_id"], []).append(str(row["crew_id"]))
    contents: dict[int, list[dict]] = {}
    for row in tables.get("menu_disk_contents", ()):
        contents.setdefault(row["menu_disk_id"], []).append(row)
    disks_by_set: dict[int, list[dict]] = {}
    for disk in tables.get("menu_disks", ()):
        menu = menus.get(disk["menu_id"])
        if menu is not None:
            disks_by_set.setdefault(menu["menu_set_id"], []).append(disk)

    resolver = _SeriesResolver(registry)
    context = _RowContext(by_id, akas, release_crews, trainers)
    context.add_pictures_and_facts(tables, articles or {})
    emitted = 0
    for set_id in sorted(disks_by_set):
        menu_set = by_id["menu_sets"].get(set_id)
        if menu_set is None:
            continue
        set_name = _text(menu_set["name"])
        crews = sorted(set_crews.get(set_id, []))
        ordered = site_order(
            disks_by_set[set_id], menus, descending=menu_set.get("menus_sort") == "desc"
        )
        positions = {disk["id"]: index for index, disk in enumerate(ordered)}
        per_menu: dict[int, list[dict]] = {}
        for disk in ordered:
            per_menu.setdefault(disk["menu_id"], []).append(disk)
        for menu_id, menu_disks in per_menu.items():
            menu = menus[menu_id]
            series, number = resolver.resolve(set_id, set_name, menu_label(menu), crews)
            if number is not None and number != menu.get("number"):
                menu = {**menu, "number": number}
            parts = map_parts([disk.get("part") for disk in menu_disks])
            for disk, part in zip(menu_disks, parts, strict=True):
                emitted += 1
                yield _disk_record(
                    context,
                    series,
                    set_name,
                    crews,
                    menu,
                    disk,
                    part,
                    contents.get(disk["id"], []),
                    disk_url(set_id, disk["id"], positions[disk["id"]]),
                    crew_ids=set_crew_ids.get(set_id, []),
                )
    counts = resolver.counts
    log(
        f"{INFO.id}: {emitted} disks in {sum(counts.values())} menus; series found for "
        f"{counts['rule']} menus by match rule, {counts['name']} by set name, "
        f"{counts['registered']} in series registered from their set"
    )
    log(
        f"{INFO.id}: {context.counts['menu']} menu screenshots, "
        f"{context.counts['snap']} game screenshots, {context.counts['fact']} facts, "
        f"{context.counts['note']} notes, {context.counts['wikipedia']} Wikipedia articles, "
        f"{context.counts['dated']} release dates"
    )


def crew_records(
    tables: dict[str, list[dict]],
    registry: SeriesRegistry,
    log=lambda message: None,
    groups=None,
) -> Iterator[CrewRecord]:
    """History and members of the site's crews, one record per crew.

    A crew is named as the catalogue names the crew of its menus: the group
    of the series its sets' menus belong to when one of those groups is the
    crew ("The Medway Boys" is the group "Medway Boys"), else a series group
    or a crew from ``data/groups.toml`` of the same name, else the site's
    own spelling. Crews with neither a history nor members are left out.
    Each record carries the site's crew id, which the menu disks of the
    crew's sets list in ``crew_ids``, and its ST release count: the menu
    disks of its sets and the game releases it is credited with.
    """
    by_id = _index(tables)
    menus = by_id["menus"]
    crews_of_set: dict[int, list[int]] = {}
    for row in tables.get("crew_menu_set", ()):
        crews_of_set.setdefault(row["menu_set_id"], []).append(row["crew_id"])
    sets_of_crew: dict[int, list[int]] = {}
    for set_id, crew_ids in crews_of_set.items():
        for crew_id in crew_ids:
            sets_of_crew.setdefault(crew_id, []).append(set_id)

    # The ST releases of each crew: the menu disks of its sets and the game
    # releases it is credited with.
    set_disks: dict[int, int] = {}
    for disk in tables.get("menu_disks", ()):
        menu = menus.get(disk["menu_id"])
        if menu is not None:
            set_disks[menu["menu_set_id"]] = set_disks.get(menu["menu_set_id"], 0) + 1
    releases: dict[int, int] = {}
    for row in tables.get("game_release_crew", ()):
        releases[row["crew_id"]] = releases.get(row["crew_id"], 0) + 1

    # The groups of the series each set's menus go to, found as build_records
    # finds them, for the menus that have disks.
    resolver = _SeriesResolver(registry)
    set_groups: dict[int, dict[str, str]] = {}
    menus_with_disks = {disk["menu_id"] for disk in tables.get("menu_disks", ())}
    for menu_id in sorted(menus_with_disks):
        menu = menus.get(menu_id)
        menu_set = by_id["menu_sets"].get(menu["menu_set_id"]) if menu else None
        if menu_set is None:
            continue
        names = sorted(
            _text(by_id["crews"][crew_id]["name"])
            for crew_id in crews_of_set.get(menu_set["id"], [])
            if crew_id in by_id["crews"]
        )
        series, _number = resolver.resolve(
            menu_set["id"], _text(menu_set["name"]), menu_label(menu), names
        )
        for group in series.group.split(" / "):
            if group.strip():
                set_groups.setdefault(menu_set["id"], {})[crew_key(group)] = group.strip()
    everywhere = {
        crew_key(part): part.strip()
        for series in registry.all()
        for part in series.group.split(" / ")
        if part.strip()
    }

    members: dict[int, list[str]] = {}
    nicks = by_id["individual_nicks"]
    for row in sorted(tables.get("crew_individual", ()), key=lambda row: row["id"]):
        person = by_id["individuals"].get(row.get("individual_id"))
        if not person or not _text(person.get("name")):
            continue
        name = _text(person["name"])
        link = nicks.get(row.get("individual_nick_id"))
        nick = by_id["individuals"].get(link["nick_id"]) if link else None
        if nick and _text(nick.get("name")) and _text(nick["name"]) != name:
            name = f"{name} ({_text(nick['name'])})"
        members.setdefault(row["crew_id"], []).append(name)

    found: list[CrewRecord] = []
    for crew_id, crew in sorted(by_id["crews"].items()):
        notes = plain_text(crew.get("history"))
        people = list(dict.fromkeys(members.get(crew_id, [])))
        if not notes and not people:
            continue
        own = _text(crew["name"])
        key = crew_key(own)
        sets = sorted(sets_of_crew.get(crew_id, []))
        name = next(
            (set_groups[s][key] for s in sets if key in set_groups.get(s, {})),
            everywhere.get(key)
            or (groups.expand_one(own, PLATFORM) if groups is not None else own),
        )
        found.append(
            CrewRecord(
                name=name,
                source=INFO.id,
                notes=notes,
                members=people,
                url=set_url(sets[0]) if sets else "",
                id=str(crew_id),
                platforms={
                    PLATFORM: sum(set_disks.get(s, 0) for s in sets) + releases.get(crew_id, 0)
                },
            )
        )
    log(
        f"{INFO.id}: {len(found)} crews, {sum(1 for r in found if r.notes)} with a "
        f"history, {sum(1 for r in found if r.members)} with members"
    )
    yield from found


class _RowContext:
    """Lookups shared by every disk of the dump."""

    def __init__(self, by_id, akas, release_crews, trainers) -> None:
        self.by_id = by_id
        self.akas = akas
        self.release_crews = release_crews
        self.trainers = trainers
        # Menu entries point at the crew's "Unofficial" release, which has no
        # publisher. The game's publisher is taken from its earliest release
        # that names one.
        self.publishers: dict[int, str] = {}
        releases = sorted(
            by_id["game_releases"].values(), key=lambda row: (row.get("date") or "9", row["id"])
        )
        for release in releases:
            pub_dev = by_id["pub_devs"].get(release.get("pub_dev_id"))
            if pub_dev and pub_dev.get("name"):
                self.publishers.setdefault(release["game_id"], _text(pub_dev["name"]))
        self.menu_pictures: dict[int, list[dict]] = {}
        self.game_pictures: dict[int, list[dict]] = {}
        self.facts: dict[int, list[str]] = {}
        self.articles: Mapping[str, str] = {}
        self.counts = dict.fromkeys(("menu", "snap", "fact", "note", "wikipedia", "dated"), 0)

    def add_pictures_and_facts(self, tables: dict[str, list[dict]], articles) -> None:
        for row in sorted(tables.get("menu_disk_screenshots", ()), key=lambda row: row["id"]):
            if _picture_extension(row.get("imgext")):
                self.menu_pictures.setdefault(row["menu_disk_id"], []).append(row)
        screenshots = self.by_id["screenshots"]
        for row in sorted(tables.get("screenshot_game", ()), key=lambda row: row["id"]):
            picture = screenshots.get(row.get("screenshot_id"))
            if picture is not None and _picture_extension(picture.get("imgext")):
                self.game_pictures.setdefault(row["game_id"], []).append(picture)
        for row in sorted(tables.get("game_facts", ()), key=lambda row: row["id"]):
            fact = plain_text(row.get("fact"))
            if fact:
                self.facts.setdefault(row["game_id"], []).append(fact)
        self.articles = articles

    def game_extras(self, record: DiskRecord, game: dict, title: str) -> None:
        """Pictures, facts and the Wikipedia article of a game, as title records."""
        game_id = game["id"]
        page = GAME_URL.format(slug=game["slug"]) if game.get("slug") else ""
        for picture in self.game_pictures.get(game_id, [])[:GAME_SCREENSHOTS]:
            self.counts["snap"] += 1
            record.media.append(
                MediaRecordIn(
                    kind="snap",
                    url=GAME_SCREENSHOT_URL.format(
                        id=picture["id"], ext=_picture_extension(picture["imgext"])
                    ),
                    source=INFO.id,
                    credit=SCREENSHOT_CREDIT,
                    page_url=page,
                    rank=GAME_RANK,
                    content_title=title,
                )
            )
        for fact in self.facts.get(game_id, []):
            self.counts["fact"] += 1
            record.trivia.append(
                TriviaRecordIn(
                    kind="fact",
                    text=fact,
                    source=INFO.id,
                    url=page,
                    licence=INFO.licence,
                    content_title=title,
                )
            )
        article = self.articles.get(str(game_id))
        if article:
            self.counts["wikipedia"] += 1
            record.trivia.append(
                TriviaRecordIn(
                    kind="wikipedia",
                    text=article,
                    source=wikidata.WIKIPEDIA_SOURCE,
                    url=wikidata.article_url(article),
                    licence=wikidata.WIKIPEDIA_LICENCE,
                    content_title=title,
                )
            )


def _picture_extension(value: object) -> str:
    extension = str(value or "").strip().lower()
    return extension if extension in PICTURE_EXTENSIONS else ""


def _disk_record(
    context: _RowContext,
    series: SeriesDef,
    set_name: str,
    crews: list[str],
    menu: dict,
    disk: dict,
    part: str,
    rows: list[dict],
    page_url: str,
    crew_ids: list[str],
) -> DiskRecord:
    by_id = context.by_id
    raw_part = _text(disk.get("part"))
    title = f"{set_name} {menu_label(menu)}"
    if raw_part:
        title += raw_part if raw_part[0] in "_." else f" {raw_part}"
    condition = by_id["menu_disk_conditions"].get(disk.get("menu_disk_condition_id"))
    notes = [_text(disk.get("notes"))]
    donor = by_id["individuals"].get(disk.get("donated_by_individual_id"))
    if donor and donor.get("name"):
        notes.append(f"Donated to Atari Legend by {_text(donor['name'])}.")
    record = DiskRecord(
        source=INFO.id,
        platform=PLATFORM,
        kind=series.kind,
        series_key=series.id,
        number=menu.get("number"),
        part=part,
        version=map_version(menu.get("version")),
        # A numbered disk is labelled from its series, and its title is best
        # taken from TOSEC, so the site's label is only used for other disks.
        title=title if menu.get("number") is None else "",
        date=str(menu["date"]) if menu.get("date") else "",
        publisher=" / ".join(crews),
        condition=map_condition(condition["name"] if condition else None),
        menu_text=_text(disk.get("scrolltext")),
        links=[("Atari Legend", page_url)],
        release_date=release_date(menu.get("date")),
        crew_ids=list(crew_ids),
    )
    if record.release_date:
        context.counts["dated"] += 1
    for index, picture in enumerate(context.menu_pictures.get(disk["id"], [])):
        context.counts["menu"] += 1
        record.media.append(
            MediaRecordIn(
                kind="menu",
                url=MENU_SCREENSHOT_URL.format(
                    id=picture["id"], ext=_picture_extension(picture["imgext"])
                ),
                source=INFO.id,
                credit=SCREENSHOT_CREDIT,
                page_url=page_url,
                rank=MENU_RANK + index,
            )
        )
    if _text(disk.get("notes")):
        context.counts["note"] += 1
        record.trivia.append(
            TriviaRecordIn(
                kind="note",
                text=plain_text(disk["notes"]),
                source=INFO.id,
                url=page_url,
                licence=INFO.licence,
            )
        )
    other_titles: dict[str, dict[str, None]] = {}
    games_done: set[str] = set()
    for row in sorted(rows, key=lambda row: (row.get("order") or 0, row["id"])):
        found = _content(context, row)
        if found is None:
            continue
        content, others, game = found
        record.contents.append(content)
        if others:
            other_titles.setdefault(content.title, {}).update(dict.fromkeys(others))
        if game is not None and content.title not in games_done:
            # A game listed twice on one disk (the game and its cheat) gets
            # its pictures and facts once.
            games_done.add(content.title)
            context.game_extras(record, game, content.title)
        software = by_id["menu_software"].get(row.get("menu_software_id"))
        if software and software.get("demozoo_id"):
            record.links.append(
                (
                    f"Demozoo: {content.title}",
                    f"https://demozoo.org/productions/{software['demozoo_id']}/",
                )
            )
    # The search index reads notes but not content extras, so other titles of
    # the games are noted on the disk as well to be found by them.
    notes.extend(
        f"{title} is also known as {', '.join(others)}." for title, others in other_titles.items()
    )
    record.notes = "\n\n".join(note for note in notes if note)
    dump = by_id["menu_disk_dumps"].get(disk.get("menu_disk_dump_id"))
    if dump is not None:
        extension = (dump.get("format") or "msa").lower()
        name = f"{dump['id']}.{extension}"
        sha512 = (dump.get("sha512") or "").lower()
        record.images.append(
            ImageRecordIn(name=name, format=extension, size=dump.get("size"), sha512=sha512)
        )
        record.locations.append(
            LocationRecord(
                provider=INFO.id,
                url=dump_url(dump["id"]),
                container="zip",
                member="",
                size=dump.get("size"),
                hash_kind="sha512" if sha512 else "",
                hash_value=sha512,
                page_url=page_url,
                priority=30,
                image_name=name,
            )
        )
    return record


def _content(
    context: _RowContext, row: dict
) -> tuple[ContentRecord, list[str], dict | None] | None:
    """One menu entry, the other titles its game is known by, and the game."""
    by_id = context.by_id
    release = by_id["game_releases"].get(row.get("game_release_id"))
    game = by_id["games"].get(row.get("game_id"))
    if game is None and release is not None:
        game = by_id["games"].get(release.get("game_id"))
    software = by_id["menu_software"].get(row.get("menu_software_id"))
    subtype = _text(row.get("subtype"))
    extra: list[str] = []
    if subtype:
        extra.append(f"[{subtype}]")
    if row.get("requirements"):
        extra.append(f"({_text(row['requirements'])})")
    publisher = cracker = ""
    others: list[str] = []
    if game is not None:
        title = _text(game.get("name"))
        kind = "game"
        names = list(context.akas.get(("game", game["id"]), []))
        publisher = context.publishers.get(game["id"], "")
        if release is not None:
            names += context.akas.get(("release", release["id"]), [])
            if release.get("name"):
                names.append(_text(release["name"]))
            pub_dev = by_id["pub_devs"].get(release.get("pub_dev_id"))
            if pub_dev and pub_dev.get("name"):
                publisher = _text(pub_dev["name"])
            cracker = " / ".join(context.release_crews.get(release["id"], []))
            options = context.trainers.get(release["id"], [])
            if options:
                extra.append("trainer: " + ", ".join(options))
        others = [name for name in dict.fromkeys(names) if name and name != title]
        if others:
            extra.append("aka " + "; ".join(others))
    elif software is not None:
        title = _text(software.get("name"))
        software_type = by_id["menu_software_content_types"].get(
            software.get("menu_software_content_type_id")
        )
        kind = _kind(_SOFTWARE_KINDS, software_type["name"] if software_type else None) or "other"
    else:
        return None
    if not title:
        return None
    kind = _kind(_SUBTYPE_KINDS, subtype) or kind
    content = ContentRecord(
        title=title,
        kind=kind,
        publisher=publisher,
        cracker=cracker,
        version=_text(row.get("version")),
        extra=" ".join(extra),
    )
    if game is not None:
        content.links.append(("atari-legend-game", str(game["id"])))
        article = context.articles.get(str(game["id"]))
        if article:
            content.links.append(("wikipedia", article))
    elif software is not None and software.get("demozoo_id"):
        content.links.append(("demozoo", str(software["demozoo_id"])))
    return content, others, game

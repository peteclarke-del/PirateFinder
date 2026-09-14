"""Query parsing and ranked catalogue search.

A query is either a disk reference, a series alias followed by a number and
optionally a part and version ("automation 250", "a250", "pp51",
"d-bug 100b", "automation 100 v2"), or free text. Disk references return the
disks of that series and number first; free text is searched in the full-text
index, with a substring index as a fallback for partial words.

``search_page`` answers the Find screen: one page of titles or discs for a
``Query``, filtered, sorted and counted in SQL.

The user's corrections (``Overrides``) count as the catalogue's values would:
a corrected crew or year is what the filters and sort orders see, a
corrected label or title is what the text and the title orders see. A row
whose corrected text holds every query word is found even when its catalogue
text does not; a row found by text a correction replaced is still found,
because the catalogue's own index holds that text.
"""

from __future__ import annotations

import contextlib
import functools
import re
import weakref
from collections import Counter
from collections.abc import Collection, Iterable, Mapping
from dataclasses import dataclass, field

from ..models import ContentKind, Disk, Facets, Query, ResultMode, SortOrder
from .naming import display_title, normalise, search_text, sort_title, title_search_text

MATCHED_LIMIT = 8  # titles of a disc reported as matching the query

# Words people put between a series name and its number ("pompey pirates
# menu disk 51", "skid row compact 128"). They name kinds of disk, not crews.
_FILLER = frozenset(
    {
        "menu",
        "disk",
        "disc",
        "compact",
        "compacted",
        "cd",
        "no",
        "nr",
        "number",
        "pack",
        "issue",
        "vol",
        "volume",
    }
)
_PART_WORDS = frozenset({"part", "side", "disk", "disc"})
_VERSION = re.compile(r"(?<![\w.])v(\d+(?:\.\d+)*)(?![\w.])", re.IGNORECASE)
_LETTERS_DIGITS = re.compile(r"^([^\W\d_]+)(\d+)([^\W\d_]?)$")
_DIGITS_LETTER = re.compile(r"^(\d+)([^\W\d_])$")

SERIES_SCORE = 1000.0  # added to the score of a row a disk reference names


@dataclass(frozen=True, slots=True)
class ParsedQuery:
    text: str
    series_ids: tuple[str, ...] = ()
    number: int | None = None
    part: str = ""
    version: str = ""
    terms: tuple[str, ...] = ()
    alias: str = ""  # the alias the series was recognised by


def tokens(text: str) -> list[str]:
    """Normalised words, with a number split from letters: "a250" -> "a", "250"."""
    found: list[str] = []
    for word in normalise(text).split():
        letters_digits = _LETTERS_DIGITS.match(word)
        digits_letter = _DIGITS_LETTER.match(word)
        if letters_digits:
            found += [group for group in letters_digits.groups() if group]
        elif digits_letter:
            found += list(digits_letter.groups())
        else:
            found.append(word)
    return found


_ALIAS_TABLES: dict[int, tuple[Mapping[str, tuple[str, ...]], dict, int]] = {}


def _alias_table(aliases: Mapping[str, tuple[str, ...]]) -> tuple[dict, int]:
    cached = _ALIAS_TABLES.get(id(aliases))
    if cached is not None and cached[0] is aliases:
        return cached[1], cached[2]
    table: dict[tuple[str, ...], tuple[str, ...]] = {}
    for alias, ids in aliases.items():
        key = tuple(tokens(alias))
        if key:
            table[key] = tuple(dict.fromkeys(table.get(key, ()) + tuple(ids)))
    longest = max((len(key) for key in table), default=0)
    _ALIAS_TABLES.clear()
    _ALIAS_TABLES[id(aliases)] = (aliases, table, longest)
    return table, longest


def _canonical_version(version: str) -> str:
    """The catalogue's spelling of a version: "v2.0" -> "v2"; the first
    edition ("v1", "v1.0") is stored unversioned, as TOSEC names it."""
    major, _dot, minor = version.lower().lstrip("v").partition(".")
    major, minor = major.lstrip("0") or "0", minor.rstrip("0")
    number = f"{major}.{minor}" if minor else major
    return "" if number == "1" else f"v{number}"


def parse_query(text: str, aliases: Mapping[str, tuple[str, ...]]) -> ParsedQuery:
    """Recognise a series reference at the start of ``text``.

    The longest alias the query starts with names the series. A number must
    follow (after optional words such as "menu" or "disk"); a single letter
    after the number is the part and "v2" the version. Whatever is left is
    free text. Without an alias, or without a number after it, the whole
    query is free text, and ``series_ids`` still names the series recognised.
    """
    version_found = _VERSION.search(text)
    if version_found is not None:
        without_version = text[: version_found.start()] + " " + text[version_found.end() :]
        parsed = _parse(text, without_version, aliases)
        if parsed.number is not None:
            return ParsedQuery(
                text=parsed.text,
                series_ids=parsed.series_ids,
                number=parsed.number,
                part=parsed.part,
                version=_canonical_version(version_found.group(1)),
                terms=parsed.terms,
                alias=parsed.alias,
            )
    return _parse(text, text, aliases)


def _parse(original: str, text: str, aliases: Mapping[str, tuple[str, ...]]) -> ParsedQuery:
    words = tokens(text)
    plain = tuple(normalise(text).split())  # free text keeps "v8" and "xenon2" whole
    table, longest = _alias_table(aliases)
    for length in range(min(len(words), longest), 0, -1):
        ids = table.get(tuple(words[:length]))
        if ids is None:
            continue
        alias = " ".join(words[:length])
        rest = words[length:]
        while rest and rest[0] in _FILLER and not rest[0].isdigit():
            rest = rest[1:]
        if not rest or not rest[0].isdigit():
            return ParsedQuery(text=original, series_ids=ids, terms=plain, alias=alias)
        number = int(rest[0])
        rest = rest[1:]
        part = ""
        if len(rest) >= 2 and rest[0] in _PART_WORDS and len(rest[1]) <= 2:
            part = _part(rest[1])
            rest = rest[2:]
        elif rest and len(rest[0]) == 1 and rest[0].isalpha():
            part = rest[0].upper()
            rest = rest[1:]
        return ParsedQuery(
            text=original,
            series_ids=ids,
            number=number,
            part=part,
            terms=tuple(rest),
            alias=alias,
        )
    return ParsedQuery(text=original, terms=plain)


def _part(text: str) -> str:
    if text.isdigit():
        index = int(text)
        return chr(ord("A") + index - 1) if 1 <= index <= 26 else text
    return text.upper()


# -- searching -------------------------------------------------------------------

# bm25 weights of the entry_fts columns: title, disk, crew, people, facets,
# files, notes; and of the disk_fts columns: label, series, contents, people,
# notes, crew, facets, files.
ENTRY_WEIGHTS = (10.0, 5.0, 4.0, 2.0, 2.0, 1.0, 1.0)
DISK_WEIGHTS = (10.0, 6.0, 8.0, 3.0, 1.0, 5.0, 2.0, 1.0)
# Articles are left out of a query of several words: "the chaos engine" also
# finds a title listed as "Chaos Engine".
_ARTICLES = frozenset({"the", "a", "an"})
SUBSTRING_MINIMUM = 3  # words this long fall back to the substring index
# When more rows than this match, relevance order skips bm25: a word that
# common ranks every row alike, and scoring them all is the slow part.
RANKED_LIMIT = 20000
# An "available only" set of more disks than this is checked row by row
# rather than used to find the rows.
LARGE_DISK_SET = 2000


def _exact(term: str) -> bool:
    """Whether a query word must match a whole word: one character or a number.
    Any other word matches the start of a word."""
    return len(term) == 1 or term.isdigit()


def _fts_query(terms: Iterable[str]) -> str:
    """An FTS5 expression: every term must match, as ``term_matches`` says."""
    parts = []
    for term in terms:
        if not term:
            continue
        expression = f'"{term}"' if _exact(term) else f'"{term}"*'
        split = tokens(term)
        if len(split) > 1:
            # "xenon2" also finds "Xenon 2": ("xenon2"* OR ("xenon"* "2"))
            expression = f"({expression} OR ({_fts_query(split)}))"
        parts.append(expression)
    return " ".join(parts)


def text_words(text: str) -> set[str]:
    """The words of ``text`` as the search index holds them (``naming.search_text``)."""
    return set(search_text(text).split())


def title_words(title: str) -> set[str]:
    """The words of a title as the search index holds them, other spellings
    included (``naming.title_search_text``)."""
    return set(title_search_text(title).split())


def term_matches(term: str, words: Collection[str]) -> bool:
    """Whether a query word matches one of ``words`` as the search index matches it.

    The same rule as ``_fts_query``: the start of a word, or a whole word for
    one character or a number, and a word such as "xenon2" also matches as
    its parts, "xenon" and "2".
    """
    if term in words or (not _exact(term) and any(word.startswith(term) for word in words)):
        return True
    parts = _parts(term)
    return bool(parts) and all(term_matches(part, words) for part in parts)


@functools.lru_cache(maxsize=4096)
def _parts(term: str) -> tuple[str, ...]:
    """The parts a query word also matches as, "xenon2" as "xenon" and "2"; () for none."""
    split = tokens(term)
    return tuple(split) if len(split) > 1 else ()


@dataclass(frozen=True, slots=True)
class Overrides:
    """The user's corrections, for the search to match, filter and sort by.

    ``disks`` holds each disc whose own fields the user corrected, as
    corrected (every field, corrected or not); ``titles`` maps the content id
    of each renamed title to (its disc id, its corrected title). Both hold
    what one person corrected by hand, so they are small.
    """

    disks: Mapping[int, Disk] = field(default_factory=dict)
    titles: Mapping[int, tuple[int, str]] = field(default_factory=dict)

    def __bool__(self) -> bool:
        return bool(self.disks or self.titles)


@dataclass(frozen=True, slots=True)
class CatalogueRow:
    """One row of a result page: a title on a disc, or a disc.

    ``content_id`` is None for a disc that lists no titles and for every row
    in DISCS mode. ``title`` is the shown title in TITLES mode (the disc
    label for a disc that lists no titles) and "" in DISCS mode. ``score``
    is higher for better matches when sorting by relevance, and 0 otherwise.
    """

    disk_id: int
    content_id: int | None
    title: str
    content_kind: ContentKind | None
    score: float
    matched: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class CataloguePage:
    rows: list[CatalogueRow] = field(default_factory=list)
    total: int = 0  # rows matching the query across every page


def _ids(values: Iterable[int]) -> str:
    """Integer ids as an SQL list, for ``IN (...)``."""
    return ", ".join(str(int(value)) for value in sorted(values))


def _runs(ids: Iterable[int]) -> list[tuple[int, int]]:
    """Runs of consecutive ids as (first, last): the entries of one disc make one run."""
    runs: list[tuple[int, int]] = []
    for value in sorted(ids):
        if runs and value == runs[-1][1] + 1:
            runs[-1] = (runs[-1][0], value)
        else:
            runs.append((value, value))
    return runs


# The fields of a disc the search index holds as text, and those that are its names.
_TEXT_FIELDS = ("label", "title", "crew", "publisher", "cracker", "notes")
_NAME_FIELDS = ("label", "title")


class _Corrected:
    """``Overrides`` against one catalogue: the values that differ from it.

    ``crew`` and ``year`` hold the discs whose corrected crew or year is not
    the catalogue's (``catalogue_crew`` and ``catalogue_year`` the
    catalogue's), ``disk_sort`` and ``entry_sort`` the discs and entries
    whose corrected sort title is not, ``entry_title`` the entries shown
    under another title and ``disc_text`` the text fields (and the date, as
    the index spells it) a correction changed. ``disk_rows`` holds (id, sort
    title, year, crew) of every disc the user corrected, for sorting.

    A text search looks again at the rows whose text a correction changed:
    the entries and discs a changed field or title belongs to. Only the
    changed text is matched here; the rest is in the catalogue's index.
    """

    def __init__(self, catalogue, overrides: Overrides) -> None:
        self.overrides = overrides
        self.crew: dict[int, str] = {}
        self.catalogue_crew: dict[int, str] = {}
        self.year: dict[int, int | None] = {}
        self.catalogue_year: dict[int, int | None] = {}
        self.disk_sort: dict[int, str] = {}
        self.disc_text: dict[int, dict[str, str]] = {}
        self.disk_rows: list[tuple[int, str, int | None, str]] = []
        self.discs: set[int] = set()
        self.entry_disk: dict[int, int] = {}  # entry id -> disc id, for every touched entry
        self.entry_title: dict[int, str] = {}
        self.entry_sort: dict[int, str] = {}
        self._words: dict[tuple[bool, bool], dict[int, tuple[set[str], str]]] = {}
        wanted = set(overrides.disks) | {disk_id for disk_id, _title in overrides.titles.values()}
        if not wanted:
            return
        listed = _ids(wanted)
        for disk_id, *values, year, month, sort in catalogue.query(
            f"SELECT id, {', '.join(_TEXT_FIELDS)}, year, month, sort_title FROM disks "
            f"WHERE id IN ({listed})"
        ):
            self.discs.add(disk_id)
            disk = overrides.disks.get(disk_id)
            if disk is None:
                continue
            crew = values[_TEXT_FIELDS.index("crew")]
            changed = {
                name: getattr(disk, name)
                for name, value in zip(_TEXT_FIELDS, values, strict=True)
                if getattr(disk, name) != value
            }
            if disk.year and (disk.year, disk.month) != (year, month):
                # As the index holds a date: "1990", and "1990 06" with a month.
                changed["date"] = f"{disk.year} {disk.month:02d}" if disk.month else str(disk.year)
            if changed:
                self.disc_text[disk_id] = changed
            corrected_sort = sort_title(disk.label)  # as the builder sorts a disc: by its label
            self.disk_rows.append((disk_id, corrected_sort, disk.year, disk.crew))
            if disk.crew != crew:
                self.crew[disk_id], self.catalogue_crew[disk_id] = disk.crew, crew
            if disk.year != year:
                self.year[disk_id], self.catalogue_year[disk_id] = disk.year, year
            if corrected_sort != sort:
                self.disk_sort[disk_id] = corrected_sort
        for entry_id, disk_id, content_id, title, sort in catalogue.query(
            "SELECT id, disk_id, content_id, title, sort_title FROM entries "
            f"WHERE disk_id IN ({listed})"
        ):
            renamed = overrides.titles.get(content_id) if content_id is not None else None
            disk = overrides.disks.get(disk_id)
            if renamed is not None:
                shown = display_title(renamed[1])
            elif disk is not None:
                shown = disk.label if content_id is None else title
            else:
                continue
            self.entry_disk[entry_id] = disk_id
            if shown != title:
                self.entry_title[entry_id] = shown
            if sort_title(shown) != sort:
                self.entry_sort[entry_id] = sort_title(shown)

    def row_words(self, titles: bool, names: bool) -> dict[int, tuple[set[str], str]]:
        """The words of the text corrections changed in each row, and that text as
        words are looked for inside titles and labels: entries for ``titles``,
        else discs. Rows no correction changed the text of are left out.

        ``names`` keeps the names only (the title, the disc's label and
        catalogue name, and for a disc its renamed titles), as the words
        after a disc reference match.
        """
        key = (titles, names)
        if key not in self._words:
            fields = _NAME_FIELDS if names else (*_TEXT_FIELDS, "date")
            renamed: dict[int, list[str]] = {}
            for disk_id, title in self.overrides.titles.values():
                renamed.setdefault(disk_id, []).append(title)
            owners = self.entry_disk.items() if titles else ((disk, disk) for disk in self.discs)
            rows: dict[int, tuple[set[str], str]] = {}
            for row_id, disk_id in owners:
                changed = self.disc_text.get(disk_id, {})
                shown = [self.entry_title[row_id]] if titles and row_id in self.entry_title else []
                if not titles:
                    shown = renamed.get(disk_id, [])
                parts = [*shown, *(changed[name] for name in fields if name in changed)]
                if parts:
                    inside = normalise(" ".join([*shown, changed.get("label", "")]))
                    rows[row_id] = (title_words(" ".join(parts)), inside)
            self._words[key] = rows
        return self._words[key]

    def extras(
        self,
        catalogue,
        titles: bool,
        prefixes: Iterable[str],
        substrings: Iterable[str] = (),
        columns: str = "",
    ) -> set[int]:
        """Touched rows with every word in their corrected text or their catalogue text.

        A row counts only when its corrected text has at least one of the
        words; a row whose catalogue text has them all is found by the plain
        search already. The catalogue side is asked of the search index,
        once for each disc's run of entries. ``columns`` limits it to those
        full-text columns, and the corrected side to names.
        """
        prefixes, substrings = tuple(prefixes), tuple(substrings)
        found: set[int] = set()
        pending: dict[tuple[tuple[str, ...], tuple[str, ...]], list[int]] = {}
        for row_id, (words, text) in self.row_words(titles, bool(columns)).items():
            other_prefixes = tuple(word for word in prefixes if not term_matches(word, words))
            other_substrings = tuple(word for word in substrings if word not in text)
            if len(other_prefixes) + len(other_substrings) == len(prefixes) + len(substrings):
                continue
            if other_prefixes or other_substrings:
                pending.setdefault((other_prefixes, other_substrings), []).append(row_id)
            else:
                found.add(row_id)
        for (other_prefixes, other_substrings), rows in pending.items():
            parameters: dict[str, object] = {}
            text = _text_parts(titles, other_prefixes, other_substrings, parameters, columns)
            source = " ".join(clause for _alias, clause in text.tables)
            where = " AND ".join([*text.where, f"{text.rowid} BETWEEN :low AND :high"])
            key = "e.id" if titles else "d.id"
            wanted = set(rows)
            for low, high in _runs(rows):
                found.update(
                    row
                    for (row,) in catalogue.query(
                        f"SELECT {key} {source} WHERE {where}",
                        {**parameters, "low": low, "high": high},
                    )
                    if row in wanted
                )
        return found


_CORRECTED: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()


def _corrected(catalogue, overrides: Overrides | None) -> _Corrected | None:
    """``overrides`` against ``catalogue``, worked out once for each set of overrides."""
    if not overrides:
        return None
    try:
        cached = _CORRECTED.get(catalogue)
    except TypeError:  # an object that cannot be weakly referenced
        cached = None
    if cached is not None and cached[0] is overrides:
        return cached[1]
    corrected = _Corrected(catalogue, overrides)
    with contextlib.suppress(TypeError):
        _CORRECTED[catalogue] = (overrides, corrected)
    return corrected


@dataclass(slots=True)
class _Plan:
    """The SQL for one query, shared by the count and the page."""

    titles: bool
    tables: list[tuple[str, str]]  # (alias, FROM or JOIN clause); the first is the root
    where: list[str]
    parameters: dict[str, object]
    rank: str = "NULL"  # bm25 of the row, lower is better
    hit: str = "NULL"  # 1 for a row of the disk reference in "Lemmings 2" queries
    wanted: bool = False  # parameters hold the query as a sort title
    prefixes: tuple[str, ...] = ()  # words matched as word prefixes
    substrings: tuple[str, ...] = ()  # words matched anywhere inside a title
    splittable: bool = False  # a word could fall back to the substring index
    available_many: str = ""  # the "available only" table when it holds many disks
    corrected: _Corrected | None = None  # the user's corrections
    # The root for a page ranked by relevance, when the plain root leaves the
    # rank out: working out bm25 for every row of a common word is slow.
    ranked_root: tuple[str, str] | None = None

    def source(self, counting: bool = False) -> str:
        """The FROM clause; for counting, only the joins the filters use."""
        if not counting:
            return " ".join(clause for _alias, clause in self.tables)
        text = " ".join(self.where)
        needed = {alias for alias, _ in self.tables if re.search(rf"\b{alias}\.", text)}
        if self.titles and "d" in needed:
            needed.add("e")  # disks join through entries
        return " ".join(
            clause
            for index, (alias, clause) in enumerate(self.tables)
            if index == 0 or alias in needed
        )


@dataclass(slots=True)
class _Text:
    """What finds the rows whose catalogue text holds every word of a query."""

    tables: list[tuple[str, str]]  # (alias, FROM or JOIN clause); the first is the root
    where: list[str]
    rank: str = "NULL"
    rowid: str = ""  # the column that numbers the rows, for a range of them


def _text_parts(
    titles: bool,
    prefixes: tuple[str, ...] | list[str],
    substrings: tuple[str, ...] | list[str],
    parameters: dict[str, object],
    columns: str = "",
) -> _Text:
    """The tables, conditions and rank for rows with every word in their catalogue text.

    ``prefixes`` are matched in the full-text index (in ``columns`` only,
    such as "{title disk}", when given) and ``substrings`` inside the titles
    and disc labels.
    """
    key = "e.id" if titles else "d.id"
    text = _Text(
        [("e", "FROM entries e"), ("d", "JOIN disks d ON d.id = e.disk_id")]
        if titles
        else [("d", "FROM disks d")],
        [],
        rowid=key,
    )
    if prefixes:
        table = "entry_fts" if titles else "disk_fts"
        weights = ", ".join(str(weight) for weight in (ENTRY_WEIGHTS if titles else DISK_WEIGHTS))
        expression = _fts_query(prefixes)
        parameters["fts"] = f"{columns} : ({expression})" if columns else expression
        text.tables = [
            (table, f"FROM {table}"),
            *(
                [
                    ("e", f"JOIN entries e ON e.id = {table}.rowid"),
                    ("d", "JOIN disks d ON d.id = e.disk_id"),
                ]
                if titles
                else [("d", f"JOIN disks d ON d.id = {table}.rowid")]
            ),
        ]
        text.where.append(f"{table} MATCH :fts")
        text.rank = f"bm25({table}, {weights})"
        text.rowid = f"{table}.rowid"
    if substrings:
        parameters["trigram"] = " ".join(f'"{word}"' for word in substrings)
        disk = "e.disk_id" if titles else "d.id"
        text.where.append(
            f"{disk} IN (SELECT rowid FROM disk_trigram WHERE disk_trigram MATCH :trigram)"
        )
        if titles:
            # A title row needs the text in its own title or in the disc label.
            for index, word in enumerate(substrings):
                parameters[f"like{index}"] = f"%{word}%"
                text.where.append(f"(e.title LIKE :like{index} OR d.label LIKE :like{index})")
    return text


def search_page(
    catalogue,
    query: Query,
    *,
    local_disks: Iterable[int] = (),
    providers: Iterable[str] = (),
    overrides: Overrides | None = None,
) -> CataloguePage:
    """One page of titles or discs answering ``query``, with the total count.

    ``local_disks`` are the disks with an image in the library and
    ``providers`` the online providers switched on; they decide what
    ``available_only`` keeps. ``overrides`` are the user's corrections, which
    the text, the filters and the sort orders see in place of the
    catalogue's values, and which the rows' titles and matched titles show.
    """
    with catalogue.batch():
        corrected = _corrected(catalogue, overrides)
        return _search_page(catalogue, query, local_disks, providers, corrected)


def _search_page(
    catalogue, query: Query, local_disks, providers, corrected: _Corrected | None
) -> CataloguePage:
    size = max(1, query.page_size)
    offset = max(0, query.page) * size
    plan = _plan(catalogue, query, local_disks, providers, corrected)
    where = " AND ".join(plan.where) or "1"
    total = catalogue.query(
        f"SELECT COUNT(*) {plan.source(counting=True)} WHERE {where}", plan.parameters
    )[0][0]
    if not total and plan.splittable:
        # Some word may be unknown to the full-text index: look for such
        # words inside titles instead, and count again.
        plan = _plan(catalogue, query, local_disks, providers, corrected, split=True)
        where = " AND ".join(plan.where) or "1"
        total = catalogue.query(
            f"SELECT COUNT(*) {plan.source(counting=True)} WHERE {where}", plan.parameters
        )[0][0]
    if not total or offset >= total:
        return CataloguePage([], total)
    relevance = query.sort == SortOrder.RELEVANCE
    rank = plan.rank if relevance and (plan.hit != "NULL" or total <= RANKED_LIMIT) else "NULL"
    key = "e.id" if plan.titles else "d.id"
    if rank != "NULL" and plan.ranked_root is not None:
        plan.tables[0] = plan.ranked_root
    keys = _keys(catalogue, plan, query.sort)
    plan.tables.extend(keys.joins)
    order = _order(query.sort, plan, rank, keys)
    if plan.available_many and _walks_an_index(query.sort, plan, order, keys):
        # Most disks are available: walking the rows in the order an index
        # gives and checking each is quicker than sorting every available
        # row. The unary plus keeps SQLite from starting from the set.
        where = " AND ".join(
            f"+{clause}" if clause.endswith(plan.available_many) else clause
            for clause in plan.where
        )
    # Only ids are sorted; the page's values are read afterwards, which keeps
    # the sort small when tens of thousands of rows match.
    ranked = catalogue.query(
        f"SELECT {key}, {rank}, {plan.hit} {plan.source()} WHERE {where} "
        f"ORDER BY {order} LIMIT :limit OFFSET :offset",
        {**plan.parameters, "limit": size, "offset": offset},
    )
    if not plan.titles:
        matched = _matched_titles_page(catalogue, [row[0] for row in ranked], plan)
        page = [
            CatalogueRow(row[0], None, "", None, _score(row[1], row[2]), matched.get(row[0], ()))
            for row in ranked
        ]
        return CataloguePage(page, total)
    marks = ",".join("?" * len(ranked))
    values = {
        row[0]: row[1:]
        for row in catalogue.query(
            f"SELECT id, disk_id, content_id, title, kind FROM entries WHERE id IN ({marks})",
            [row[0] for row in ranked],
        )
    }
    shown = corrected.entry_title if corrected is not None else {}
    page = []
    for entry_id, entry_rank, entry_hit in ranked:
        disk_id, content_id, title, kind = values[entry_id]
        title = shown.get(entry_id, title)
        page.append(
            CatalogueRow(
                disk_id=disk_id,
                content_id=content_id,
                title=title,
                content_kind=_kind(kind) if content_id is not None else None,
                score=_score(entry_rank, entry_hit),
                matched=(title,) if content_id is not None and _title_matches(title, plan) else (),
            )
        )
    return CataloguePage(page, total)


def _kind(value: str) -> ContentKind:
    try:
        return ContentKind(value)
    except ValueError:
        return ContentKind.OTHER


def _score(rank: float | None, hit: int | None) -> float:
    """Higher is better: a disk reference hit, then the full-text relevance."""
    return (SERIES_SCORE if hit else 0.0) + (-rank if rank else 0.0)


def _plan(
    catalogue,
    query: Query,
    local_disks,
    providers,
    corrected: _Corrected | None,
    split: bool = False,
) -> _Plan:
    """The SQL for ``query``. Every word is a full-text word unless ``split``
    is set, when words the full-text index does not know become substrings."""
    titles = query.mode != ResultMode.DISCS
    parameters: dict[str, object] = {}
    plan = _Plan(
        titles=titles,
        tables=(
            [("e", "FROM entries e"), ("d", "JOIN disks d ON d.id = e.disk_id")]
            if titles
            else [("d", "FROM disks d")]
        ),
        where=[],
        parameters=parameters,
        corrected=corrected,
    )
    plan.where, plan.available_many = _filters(
        catalogue, query, parameters, local_disks, providers, corrected
    )
    parsed = parse_query(query.text, catalogue.aliases()) if query.text.strip() else None
    if parsed is None:
        return plan
    series = ""
    words: tuple[str, ...] = ()
    alternative: tuple[str, ...] = ()  # words that find rows besides the disk reference
    if parsed.series_ids and parsed.number is not None:
        series = _series_filter(catalogue, parsed, parameters)
        if parsed.terms:
            words = parsed.terms
        elif len(parsed.alias.replace(" ", "")) >= 3:
            # "Lemmings 2" is a menu disk and a game: show both.
            alternative = tuple(tokens(query.text))
    else:
        words = parsed.terms
    words, alternative = _significant(words), _significant(alternative)

    table = "entry_fts" if titles else "disk_fts"
    weights = ", ".join(str(weight) for weight in (ENTRY_WEIGHTS if titles else DISK_WEIGHTS))
    root_table = "entries" if titles else "disks"
    joins = (
        [("e", "JOIN entries e ON e.id = {root}"), ("d", "JOIN disks d ON d.id = e.disk_id")]
        if titles
        else [("d", "JOIN disks d ON d.id = {root}")]
    )
    if alternative:
        # The disk reference's rows and the rows the whole text finds, as one set.
        # Only names count here: "Pompey Pirates 1" must not also find every
        # menu whose scroll text happens to contain a "1".
        names = "{title disk}" if titles else "{label contents}"
        parameters["fts"] = f"{names} : ({_fts_query(alternative)})"
        reference = (
            "SELECT x.id AS id, 1 AS hit, 0.0 AS rank FROM entries x "
            f"JOIN disks d ON d.id = x.disk_id WHERE {series}"
            if titles
            else f"SELECT d.id AS id, 1 AS hit, 0.0 AS rank FROM disks d WHERE {series}"
        )
        found = f"SELECT rowid, 0, bm25({table}, {weights}) FROM {table} WHERE {table} MATCH :fts"
        extras = corrected.extras(catalogue, titles, alternative, (), names) if corrected else set()
        if extras:
            found += f" UNION ALL SELECT id, 0, 0.0 FROM {root_table} WHERE id IN ({_ids(extras)})"
        plan.tables = [
            (
                "h",
                f"FROM (SELECT id, MAX(hit) AS hit, MIN(rank) AS rank FROM ({reference} "
                f"UNION ALL {found}) GROUP BY id) h",
            ),
            *[(alias, clause.format(root="h.id")) for alias, clause in joins],
        ]
        plan.rank, plan.hit = "h.rank", "h.hit"
        plan.prefixes = alternative
        _want(plan, alternative)
        return plan

    if series:
        plan.where.append(series)
    if split:
        prefixes, substrings = _split_words(catalogue, table, words)
    else:
        prefixes, substrings = list(words), []
        plan.splittable = any(_substring_word(word) for word in words)
    text = _text_parts(titles, prefixes, substrings, parameters)
    extras = corrected.extras(catalogue, titles, prefixes, substrings) if corrected else set()
    if extras:
        # The rows the catalogue text finds and the rows only corrected text
        # finds, as one set.
        key = "e.id" if titles else "d.id"
        source = " ".join(clause for _alias, clause in text.tables)

        def root(rank: str) -> tuple[str, str]:
            found = f"SELECT {key} AS id, {rank} AS rank {source} WHERE {' AND '.join(text.where)}"
            return (
                "h",
                f"FROM (SELECT id, MIN(rank) AS rank FROM ({found} UNION ALL SELECT id, 0.0 "
                f"FROM {root_table} WHERE id IN ({_ids(extras)})) GROUP BY id) h",
            )

        plan.tables = [
            root("NULL"),
            *[(alias, clause.format(root="h.id")) for alias, clause in joins],
        ]
        plan.ranked_root = root(text.rank) if text.rank != "NULL" else None
        plan.rank = "h.rank"
    else:
        plan.tables = text.tables
        plan.where.extend(text.where)
        plan.rank = text.rank
    plan.prefixes, plan.substrings = tuple(prefixes), tuple(substrings)
    _want(plan, words)
    return plan


def _want(plan: _Plan, words: tuple[str, ...]) -> None:
    """Keep the query as a sort title, so a title equal to it can rank first."""
    wanted = sort_title(" ".join(words)) if words else ""
    if wanted:
        plan.parameters.update(want=wanted, want_low=f"{wanted} ", want_high=f"{wanted}!")
        plan.wanted = True


def _significant(words: Iterable[str]) -> tuple[str, ...]:
    words = tuple(word for word in words if word)
    kept = tuple(word for word in words if word not in _ARTICLES)
    return kept if kept else words


def _split_words(catalogue, table: str, words: tuple[str, ...]) -> tuple[list[str], list[str]]:
    """Words for the full-text index, and words it does not know that are
    long enough to look for inside titles instead."""
    prefixes: list[str] = []
    substrings: list[str] = []
    for word in words:
        if _substring_word(word) and not catalogue.query(
            f"SELECT 1 FROM {table} WHERE {table} MATCH ? LIMIT 1", (_fts_query([word]),)
        ):
            substrings.append(word)
        else:
            prefixes.append(word)
    return prefixes, substrings


def _substring_word(word: str) -> bool:
    return len(word) >= SUBSTRING_MINIMUM and not word.isdigit()


def _filters(
    catalogue,
    query: Query,
    parameters: dict[str, object],
    local_disks,
    providers,
    corrected: _Corrected | None,
) -> tuple[list[str], str]:
    """WHERE clauses for the filters, and the name of the "available only"
    table when it holds more than ``LARGE_DISK_SET`` disks."""
    where: list[str] = []
    many = ""
    if query.platform is not None:
        where.append("d.platform = :platform")
        parameters["platform"] = str(query.platform)
    if query.category:
        where.append("d.category = :category")
        parameters["category"] = query.category
    if query.kinds:
        names = []
        for index, kind in enumerate(sorted(str(kind) for kind in query.kinds)):
            parameters[f"kind{index}"] = kind
            names.append(f":kind{index}")
        where.append(f"d.kind IN ({', '.join(names)})")
    if query.crew:
        changed = corrected.crew if corrected is not None else {}
        where.append(_equals("d.crew", "crew", query.crew, changed, parameters))
    if query.year is not None:
        changed = corrected.year if corrected is not None else {}
        where.append(_equals("d.year", "year", int(query.year), changed, parameters))
    if query.available_only:
        # Local disks and hosted ones, as a temporary table of ids that is
        # written once and reused while the library and providers stay the same.
        table, size = catalogue.available_set(local_disks, (str(p) for p in providers))
        if table:
            column = "d.id" if query.mode == ResultMode.DISCS else "e.disk_id"
            where.append(f"{column} IN {table}")
            many = table if size > LARGE_DISK_SET else ""
        else:
            where.append("0")
    return where, many


def _equals(
    column: str, name: str, value: object, changed: Mapping[int, object], parameters
) -> str:
    """SQL for ``column = value``, where the discs in ``changed`` have the corrected
    value instead of the catalogue's. Without corrections the catalogue's index
    serves it as before; a corrected disc is taken out or added by its id."""
    parameters[name] = value
    if not changed:
        return f"{column} = :{name}"
    clause = f"({column} = :{name} AND d.id NOT IN ({_ids(changed)}))"
    wanted = [disk_id for disk_id, corrected in changed.items() if corrected == value]
    return f"({clause} OR d.id IN ({_ids(wanted)}))" if wanted else clause


def _series_filter(catalogue, parsed: ParsedQuery, parameters: dict[str, object]) -> str:
    """SQL for the disks of a disk reference; a part or version that no disk
    has is ignored, so every part and version is shown instead."""
    names = []
    for index, series_id in enumerate(parsed.series_ids):
        parameters[f"series{index}"] = series_id
        names.append(f":series{index}")
    parameters["number"] = parsed.number
    clause = f"d.series_id IN ({', '.join(names)}) AND d.number = :number"
    narrowed = ""
    if parsed.part:
        parameters["part"] = parsed.part
        narrowed += " AND d.part = :part"
    if parsed.version:
        parameters["version"] = parsed.version
        narrowed += " AND d.version = :version"
    if narrowed and catalogue.query(
        f"SELECT 1 FROM disks d WHERE {clause}{narrowed} LIMIT 1", parameters
    ):
        clause += narrowed
    return f"({clause})"


@dataclass(frozen=True, slots=True)
class _Keys:
    """What a query sorts by: the catalogue's columns, or where the user
    corrected them, the corrected value (joined from a temporary table)."""

    title: str
    year: str = "d.year"
    crew: str = "d.crew"
    joins: tuple[tuple[str, str], ...] = ()


def _keys(catalogue, plan: _Plan, sort: SortOrder) -> _Keys:
    """The sort keys of ``plan``, with the corrections that change them."""
    corrected = plan.corrected
    keys = _Keys("e.sort_title" if plan.titles else "d.sort_title")
    if corrected is None or sort == SortOrder.DISC:
        return keys
    by_title = sort in (
        SortOrder.TITLE,
        SortOrder.TITLE_DESC,
        SortOrder.YEAR,
        SortOrder.YEAR_DESC,
        SortOrder.PLATFORM,
    ) or (sort == SortOrder.RELEVANCE and plan.wanted)
    title, year, crew = keys.title, keys.year, keys.crew
    joins: list[tuple[str, str]] = []
    if plan.titles and by_title and corrected.entry_sort:
        table = catalogue.temp_table(
            "id INTEGER PRIMARY KEY, sort_title TEXT", sorted(corrected.entry_sort.items())
        )
        joins.append(("oe", f"LEFT JOIN {table} oe ON oe.id = e.id"))
        title = "IIF(oe.id IS NULL, e.sort_title, oe.sort_title)"
    disc_title = not plan.titles and by_title and bool(corrected.disk_sort)
    disc_year = sort in (SortOrder.YEAR, SortOrder.YEAR_DESC) and bool(corrected.year)
    disc_crew = sort == SortOrder.CREW and bool(corrected.crew)
    if disc_title or disc_year or disc_crew:
        table = catalogue.temp_table(
            "id INTEGER PRIMARY KEY, sort_title TEXT, year INTEGER, crew TEXT",
            sorted(corrected.disk_rows),
        )
        joins.append(("od", f"LEFT JOIN {table} od ON od.id = d.id"))
        if disc_title:
            title = "IIF(od.id IS NULL, d.sort_title, od.sort_title)"
        if disc_year:
            year = "IIF(od.id IS NULL, d.year, od.year)"
        if disc_crew:
            crew = "IIF(od.id IS NULL, d.crew, od.crew)"
    return _Keys(title, year, crew, tuple(joins))


def _walks_an_index(sort: SortOrder, plan: _Plan, order: str, keys: _Keys) -> bool:
    """True when the rows can be read in ``order`` straight from an index
    (the sort title, or the id for disc order), with no text to match and
    no corrected sort key."""
    if plan.tables[0][0] not in ("e", "d") or keys.joins:
        return False
    key = "e.id" if plan.titles else "d.id"
    return sort in (SortOrder.TITLE, SortOrder.TITLE_DESC, SortOrder.DISC) or order == key


def _order(sort: SortOrder, plan: _Plan, rank: str, keys: _Keys) -> str:
    """The ORDER BY clause, always ending in a unique id so pages never
    repeat or skip a row.

    Disk ids follow disc order and entry ids follow disk ids and then menu
    order, so ordering by id is disc order.
    """
    key = "e.id" if plan.titles else "d.id"
    title = keys.title
    if sort == SortOrder.TITLE:
        return f"{title}, {key}"
    if sort == SortOrder.TITLE_DESC:
        return f"{title} DESC, {key} DESC"
    if sort == SortOrder.YEAR:
        return f"{keys.year} IS NULL, {keys.year}, {title}, {key}"
    if sort == SortOrder.YEAR_DESC:
        return f"{keys.year} IS NULL, {keys.year} DESC, {title}, {key}"
    if sort == SortOrder.CREW:
        return f"{keys.crew} COLLATE NOCASE, {keys.crew}, {key}"
    if sort == SortOrder.PLATFORM:
        return f"d.platform, {title}, {key}"
    terms: list[str] = []
    if sort == SortOrder.RELEVANCE:
        if plan.hit != "NULL":
            terms.append(f"{plan.hit} DESC")
        if plan.wanted:
            prefix = "{0} >= :want_low AND {0} < :want_high"
            if plan.titles:
                terms.append(
                    f"CASE WHEN {title} = :want THEN 0 "
                    f"WHEN {prefix.format(title)} THEN 1 ELSE 2 END"
                )
            else:
                exact, starts = _discs_titled(plan, prefix.format("sort_title"))
                terms.append(
                    f"CASE WHEN {title} = :want THEN 0 WHEN {exact} THEN 1 "
                    f"WHEN {prefix.format(title)} THEN 2 WHEN {starts} THEN 3 ELSE 4 END"
                )
        if rank != "NULL":
            terms.append(rank)
    return ", ".join([*terms, key])


def _discs_titled(plan: _Plan, prefix: str) -> tuple[str, str]:
    """SQL for "the disc holds a title equal to the query" and "... a title that
    starts with it", by the corrected titles where the user renamed one."""
    corrected = plan.corrected
    exact, starts = "sort_title = :want", prefix
    if corrected is None or not corrected.entry_sort:
        return (
            f"d.id IN (SELECT disk_id FROM entries WHERE {exact})",
            f"d.id IN (SELECT disk_id FROM entries WHERE {starts})",
        )
    want = str(plan.parameters["want"])
    changed = _ids(corrected.entry_sort)

    def clause(condition: str, holds) -> str:
        text = f"d.id IN (SELECT disk_id FROM entries WHERE {condition} AND id NOT IN ({changed}))"
        discs = {
            corrected.entry_disk[entry] for entry, s in corrected.entry_sort.items() if holds(s)
        }
        return f"({text} OR d.id IN ({_ids(discs)}))" if discs else text

    return (
        clause(exact, lambda value: value == want),
        clause(starts, lambda value: value.startswith(f"{want} ")),
    )


def _title_matches(title: str, plan: _Plan) -> bool:
    """True when a query word matches a word of ``title`` (``term_matches``),
    or a substring word is inside it."""
    words = title_words(title)
    return any(term_matches(word, words) for word in plan.prefixes) or any(
        word in normalise(title) for word in plan.substrings
    )


def _matched_titles_page(catalogue, disk_ids: list[int], plan: _Plan) -> dict[int, tuple]:
    """Up to ``MATCHED_LIMIT`` titles of each disk that a query word matches,
    by the names the user gave titles they renamed."""
    found: dict[int, list[str]] = {}
    if not disk_ids or not (plan.prefixes or plan.substrings):
        return {}
    renamed = plan.corrected.overrides.titles if plan.corrected is not None else {}
    marks = ",".join("?" * len(disk_ids))
    matches: dict[str, bool] = {}  # a title many discs list is looked at once
    for disk_id, content_id, title in catalogue.query(
        f"SELECT disk_id, id, title FROM contents WHERE disk_id IN ({marks}) "
        "ORDER BY disk_id, position, id",
        disk_ids,
    ):
        if content_id in renamed:
            title = renamed[content_id][1]
        if title not in matches:
            matches[title] = _title_matches(title, plan)
        if matches[title]:
            titles = found.setdefault(disk_id, [])
            if len(titles) < MATCHED_LIMIT and title not in titles:
                titles.append(title)
    return {disk_id: tuple(titles) for disk_id, titles in found.items()}


_FACETS: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()
_ASCII_LOWER = str.maketrans("ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz")


def facets(catalogue, overrides: Overrides | None = None) -> Facets:
    """Crews, years and categories with how many discs each has.

    The catalogue's counts are worked out once per open catalogue: the
    catalogue never changes while it is open. A disc whose crew or year the
    user corrected counts under the corrected one (``overrides``).
    """
    found = _catalogue_facets(catalogue)
    corrected = _corrected(catalogue, overrides)
    if corrected is None or not (corrected.crew or corrected.year):
        return found
    crews = Counter(dict(found.crews))
    for disk_id, crew in corrected.crew.items():
        crews[corrected.catalogue_crew[disk_id]] -= 1
        crews[crew] += 1
    years = Counter(dict(found.years))
    for disk_id, year in corrected.year.items():
        years[corrected.catalogue_year[disk_id]] -= 1
        years[year] += 1
    return Facets(
        crews=tuple(
            sorted(
                ((name, count) for name, count in crews.items() if name and count > 0),
                # As SQLite orders them: COLLATE NOCASE folds ASCII letters only.
                key=lambda item: (item[0].translate(_ASCII_LOWER), item[0]),
            )
        ),
        years=tuple(
            sorted((year, count) for year, count in years.items() if year is not None and count)
        ),
        categories=found.categories,
    )


def _catalogue_facets(catalogue) -> Facets:
    try:
        cached = _FACETS.get(catalogue)
    except TypeError:  # an object that cannot be weakly referenced
        cached = None
    if cached is not None:
        return cached
    crews = catalogue.query(
        "SELECT crew, COUNT(*) FROM disks WHERE crew != '' GROUP BY crew "
        "ORDER BY crew COLLATE NOCASE, crew"
    )
    years = catalogue.query(
        "SELECT year, COUNT(*) FROM disks WHERE year IS NOT NULL GROUP BY year ORDER BY year"
    )
    categories = catalogue.query(
        "SELECT category, COUNT(*) FROM disks WHERE category != '' GROUP BY category "
        "ORDER BY category"
    )
    result = Facets(
        crews=tuple((name, count) for name, count in crews),
        years=tuple((year, count) for year, count in years),
        categories=tuple((name, count) for name, count in categories),
    )
    with contextlib.suppress(TypeError):
        _FACETS[catalogue] = result
    return result

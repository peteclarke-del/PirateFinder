"""Query parsing and ranked catalogue search.

A query is either a disk reference, a series alias followed by a number and
optionally a part and version ("automation 250", "a250", "pp51",
"d-bug 100b", "automation 100 v2"), or free text. Disk references return the
disks of that series and number first; free text is searched in the full-text
index, with a substring index as a fallback for partial words.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from ..models import SearchFilters
from .naming import normalise, sort_key

# bm25 weights of the disk_fts columns: label, series, contents, people, notes.
COLUMN_WEIGHTS = (10.0, 6.0, 8.0, 3.0, 1.0)
TRIGRAM_THRESHOLD = 20  # add substring matches when full-text finds fewer disks
MATCHED_LIMIT = 8

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

SERIES_SCORE = 1000.0
FULL_TEXT_SCORE = 100.0
TRIGRAM_SCORE = 10.0


@dataclass(frozen=True, slots=True)
class ParsedQuery:
    text: str
    series_ids: tuple[str, ...] = ()
    number: int | None = None
    part: str = ""
    version: str = ""
    terms: tuple[str, ...] = ()
    alias: str = ""  # the alias the series was recognised by


@dataclass(frozen=True, slots=True)
class CatalogueHit:
    disk_id: int
    score: float
    matched: tuple[str, ...] = ()


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


def _filter_sql(filters: SearchFilters, column: str = "d") -> tuple[str, list[object]]:
    clauses: list[str] = []
    parameters: list[object] = []
    if filters.platform is not None:
        clauses.append(f"{column}.platform = ?")
        parameters.append(str(filters.platform))
    if filters.kinds:
        kinds = sorted(str(kind) for kind in filters.kinds)
        clauses.append(f"{column}.kind IN ({','.join('?' * len(kinds))})")
        parameters += kinds
    return "".join(f" AND {clause}" for clause in clauses), parameters


def _fts_query(terms: Iterable[str]) -> str:
    """An FTS5 expression: every term must match, as a prefix when longer
    than one character and not a bare number."""
    parts = []
    for term in terms:
        if not term:
            continue
        exact = len(term) == 1 or term.isdigit()
        expression = f'"{term}"' if exact else f'"{term}"*'
        split = tokens(term)
        if len(split) > 1:
            # "xenon2" also finds "Xenon 2": ("xenon2"* OR ("xenon"* "2"))
            expression = f"({expression} OR ({_fts_query(split)}))"
        parts.append(expression)
    return " ".join(parts)


def _series_disks(catalogue, parsed: ParsedQuery, filters: SearchFilters, limit: int) -> list[int]:
    where, parameters = _filter_sql(filters)
    marks = ",".join("?" * len(parsed.series_ids))
    base = f"SELECT d.id FROM disks d WHERE d.series_id IN ({marks}) AND d.number = ?{where}"
    common = [*parsed.series_ids, parsed.number, *parameters]
    order = " ORDER BY d.series_id, d.part, d.version, d.id LIMIT ?"
    narrowed = ""
    extra: list[object] = []
    if parsed.part:
        narrowed += " AND d.part = ?"
        extra.append(parsed.part)
    if parsed.version:
        narrowed += " AND d.version = ?"
        extra.append(parsed.version)
    if narrowed:
        rows = catalogue.query(base + narrowed + order, [*common, *extra, limit])
        if rows:
            return [row[0] for row in rows]
    return [row[0] for row in catalogue.query(base + order, [*common, limit])]


def _full_text(catalogue, terms, filters: SearchFilters, limit: int) -> list[tuple[int, float]]:
    expression = _fts_query(terms)
    if not expression:
        return []
    where, parameters = _filter_sql(filters)
    weights = ", ".join(str(weight) for weight in COLUMN_WEIGHTS)
    rows = catalogue.query(
        f"SELECT disk_fts.rowid, bm25(disk_fts, {weights}) AS rank "
        f"FROM disk_fts JOIN disks d ON d.id = disk_fts.rowid "
        f"WHERE disk_fts MATCH ?{where} ORDER BY rank LIMIT ?",
        [expression, *parameters, limit],
    )
    return [(row[0], -row[1]) for row in rows]


def _trigram(catalogue, terms, filters: SearchFilters, limit: int) -> list[tuple[int, float]]:
    long_terms = [term for term in terms if len(term) >= 3]
    if not long_terms or any(term.isdigit() and len(term) < 3 for term in terms):
        return []  # a short number cannot be matched by substring and must not be dropped
    where, parameters = _filter_sql(filters)
    expression = " ".join(f'"{term}"' for term in long_terms)
    rows = catalogue.query(
        f"SELECT disk_trigram.rowid, bm25(disk_trigram) AS rank "
        f"FROM disk_trigram JOIN disks d ON d.id = disk_trigram.rowid "
        f"WHERE disk_trigram MATCH ?{where} ORDER BY rank LIMIT ?",
        [expression, *parameters, limit],
    )
    return [(row[0], -row[1]) for row in rows]


def _matched_titles(catalogue, disk_ids: list[int], terms: tuple[str, ...]) -> dict[int, list]:
    words = [term for term in terms if term]
    found: dict[int, list[str]] = {}
    if not words or not disk_ids:
        return found
    for start in range(0, len(disk_ids), 500):
        batch = disk_ids[start : start + 500]
        rows = catalogue.query(
            f"SELECT disk_id, title FROM contents WHERE disk_id IN ({','.join('?' * len(batch))}) "
            "ORDER BY disk_id, position",
            batch,
        )
        for disk_id, title in rows:
            title_words = normalise(title).split()
            if any(word.startswith(term) for term in words for word in title_words):
                titles = found.setdefault(disk_id, [])
                if len(titles) < MATCHED_LIMIT and title not in titles:
                    titles.append(title)
    return found


_LEADING_ARTICLE = re.compile(r"^(?:the|a|an) ")


def _labels(catalogue, disk_ids: Iterable[int]) -> dict[int, str]:
    ids = list(disk_ids)
    labels: dict[int, str] = {}
    for start in range(0, len(ids), 500):
        batch = ids[start : start + 500]
        rows = catalogue.query(
            f"SELECT id, label FROM disks WHERE id IN ({','.join('?' * len(batch))})", batch
        )
        labels.update(rows)
    return labels


def _base_title(label: str) -> str:
    """The title part of a label, normalised and without a leading article:
    "The Chaos Engine (Disk 1 of 2) [cr Cynix]" -> "chaos engine"."""
    base = re.split(r" [(\[]", label, maxsplit=1)[0]
    return _LEADING_ARTICLE.sub("", normalise(base))


def search_catalogue(
    catalogue,
    text: str,
    filters: SearchFilters | None = None,
    limit: int = 500,
) -> list[CatalogueHit]:
    """Disks answering ``text``, best first."""
    filters = filters or SearchFilters()
    parsed = parse_query(text, catalogue.aliases())
    scores: dict[int, float] = {}

    if parsed.series_ids and parsed.number is not None:
        for position, disk_id in enumerate(_series_disks(catalogue, parsed, filters, limit)):
            scores[disk_id] = SERIES_SCORE - position * 0.001
        text_terms = parsed.terms
        if not text_terms and len(parsed.alias.replace(" ", "")) >= 3:
            # "Lemmings 2" is a menu disk and a game: show both.
            text_terms = tuple(tokens(text))
    else:
        text_terms = parsed.terms

    full_text = _full_text(catalogue, text_terms, filters, limit) if text_terms else []
    for disk_id, relevance in full_text:
        scores.setdefault(disk_id, FULL_TEXT_SCORE + min(relevance, 500.0))
    if parsed.number is None and text_terms and len(full_text) < TRIGRAM_THRESHOLD:
        for disk_id, relevance in _trigram(catalogue, text_terms, filters, limit):
            scores.setdefault(disk_id, TRIGRAM_SCORE + min(relevance, 50.0))
    if not scores:
        return []

    labels = _labels(catalogue, scores)
    if full_text:
        # A disk whose title is the query itself ranks above longer titles.
        wanted = _LEADING_ARTICLE.sub("", " ".join(text_terms))
        for disk_id, _relevance in full_text:
            if scores[disk_id] >= SERIES_SCORE - 1:
                continue
            title = _base_title(labels.get(disk_id, ""))
            scores[disk_id] += 20.0 if title == wanted else 5.0 if title.startswith(wanted) else 0
    ordered = sorted(
        scores.items(),
        key=lambda item: (-round(item[1]), sort_key(labels.get(item[0], "")), item[0]),
    )[:limit]
    matched = _matched_titles(catalogue, [disk_id for disk_id, _ in ordered], text_terms)
    return [
        CatalogueHit(disk_id=disk_id, score=score, matched=tuple(matched.get(disk_id, ())))
        for disk_id, score in ordered
    ]

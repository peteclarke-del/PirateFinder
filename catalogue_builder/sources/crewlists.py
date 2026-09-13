"""Period crew lists from the 1997 Atari ST CD-R by Alien of the Pompey Pirates.

The CD-R, preserved on the Internet Archive as one ISO image, holds MSA dumps
of the Pompey Pirates, Medway Boys, Flame of Finland, Superior, Cynix and
Delight menus and the Sewer Software doc disks, each directory with the
crew's own list of what is on every disk. (The "COMPIL DISKS" list in
``MSAFILES/COMPILS`` names no crew, so it is not tied to any series.) The Internet
Archive serves single files from inside the ISO, so the lists are read one
by one and every MSA becomes a download location.

The lists are plain text, one disk code per line followed by its titles::

    CD13-4A<TAB>AWESOME
    <TAB>POMPEY COLLECTION FILE CHECKER

A line before the first code is the list heading. When the numbering starts
again part way through a file, the line before the restart heads the new
section (the Pompey list ends with its "Crappy Compacts"). ``MENUS/COMPLETE.TXT``
is a merged title index of the same lists and fills in titles the crew lists
leave out. ``DOCS/SEWER/LIST.DOC`` indexes every document by disk.

Series are recognised with the rules in ``data/series/match-crewlists.toml``.
They are matched against "<list path> <heading>: <code>" for list entries,
"MENUS/COMPLETE.TXT <crew> #<code>" for index entries, "DOCS/SEWER/LIST.DOC
<disk>" for the doc list and the ISO path for MSA files. The rules capture the
number, a raw part ("4A", "B", "2") and a version; this module turns the raw
parts of one disk number into the part letters other sources use, counting
the disks of that number in list order, so the seven disks of Pompey Pirates
13 ("CD13", "CD13-2" up to "CD13-5B") become parts A to G.
"""

from __future__ import annotations

import html
import re
import urllib.parse
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field

from ..context import BuildContext
from ..records import (
    NO_LICENCE_STATED,
    ContentRecord,
    DiskRecord,
    LocationRecord,
    SourceInfo,
    normalise_version,
)
from ..series import normalise

ITEM = "atari-st-collection-1997-cdr-alien-pompey-pirates"
ISO = "ATARI_ST.ISO"

INFO = SourceInfo(
    id="crew-lists",
    name="Crew lists on the Pompey Pirates Atari ST CD-R (1997)",
    url=f"https://archive.org/details/{ITEM}",
    licence=NO_LICENCE_STATED,
)
CONTENT_PRIORITY = 50

PLATFORM = "atari-st"
DETAILS = f"https://archive.org/details/{ITEM}"
LISTING = f"https://archive.org/download/{ITEM}/{ISO}/"
LISTS = (
    "MENUS/POMPEY/POMPEY.TXT",
    "MENUS/MEDWAY/MEDWAY.TXT",
    "MENUS/FOF_MENU/FOF.TXT",
    "MENUS/SUPERIOR/SUPERIOR.TXT",
    "MENUS/CYNIX/CYNIX.TXT",
    "MENUS/DELIGHT/DAD.TXT",
)
COMPLETE = "MENUS/COMPLETE.TXT"
DOC_LIST = "DOCS/SEWER/LIST.DOC"
LOCATION_PRIORITY = 30
MAX_AGE_DAYS = 30.0

_CODE_LINE = re.compile(r"^(?P<code>CD\d+[0-9A-Z-]*)\s+(?P<title>\S.*)$", re.IGNORECASE)
_LEADING_NUMBER = re.compile(r"\d+")
_MEMBER = re.compile(rf"{re.escape(ISO)}/(?P<member>[^\"'<>?#]+)\"")
_INDEX_LINE = re.compile(r"^(?P<title>.+?)\t+(?P<disk>[^\t]+?)\s*#(?P<code>[0-9A-Z-]+)\s*$")
_DOC_LINE = re.compile(
    r"^(?P<title>\S.*?)\s{2,}(?P<type>\S.*?)\s{2,}(?P<disk>\d{1,3}|T\d+|[A-Z]{2})\s*$"
)
_DOC_LEGEND = re.compile(r"^\*\s+(?P<code>[A-Z]{1,2})\s*=\s*(?P<name>[A-Z][A-Z ]+?)\s*\*?$")
_TRAILING_NOTE = re.compile(r"\s*\((?P<note>[^()]*)\)\s*$")

# --- title case -----------------------------------------------------------

_SMALL_WORDS = frozenset(
    {"a", "an", "and", "at", "by", "for", "from", "in", "into", "of", "on", "or", "the", "to"}
    | {"vs", "with"}
)
_ROMAN = re.compile(r"^(?:I|II|III|IV|V|VI|VII|VIII|IX|X|XI|XII|XIII|XIV|XV)$")
_WORD_RUN = re.compile(r"[A-Za-z]+")
_KEEP_UPPER = frozenset({"ST", "STE", "TT", "PC", "PD", "UK", "USA", "US", "TV", "HQ", "GB"})
_SPECIAL = {"MR": "Mr", "MRS": "Mrs", "DR": "Dr", "MC": "Mc", "VS": "vs"}


def title_case(text: str) -> str:
    """Readable case for an upper-case title, keeping numerals and acronyms.

    Roman numerals ("II"), words without vowels ("TMFTC", "CJ"), dotted
    initials ("H.A.T.E.") and words mixing letters and digits ("F16") stay
    upper case; "V.2.31" becomes "v2.31". Text that already has lower case
    letters is returned unchanged.
    """
    if any(char.islower() for char in text):
        return text
    words = text.split()
    result = []
    for index, word in enumerate(words):
        result.append(_case_word(word, first=index == 0, last=index == len(words) - 1))
    joined = " ".join(result)
    return re.sub(r"(?<=[A-Za-z])'S\b", "'s", joined)


def _case_word(word: str, *, first: bool, last: bool) -> str:
    if re.fullmatch(r"V\.?\d[\d.A-Z]*", word):
        return "v" + word[1:].lstrip(".")
    if re.search(r"\d", word) and not re.search(r"[A-Z]{3,}", word):
        return word  # "F16", "3D", "ZX81", "F-19"
    if re.fullmatch(r"(?:[A-Z]\.)+[A-Z]?\.?", word):
        return word  # "H.A.T.E.", "S.E.U.C.K."
    bare = word.strip("()[]\"'!?,.:;-+&")
    if bare.lower() in _SMALL_WORDS and not first and not last:
        return word.lower()

    def run(found: re.Match[str]) -> str:
        part = found.group(0)
        if part in _SPECIAL:
            return _SPECIAL[part]
        if _ROMAN.match(part) or part in _KEEP_UPPER:
            return part
        if len(part) > 1 and not re.search(r"[AEIOUY]", part):
            return part
        if len(part) == 1:
            return part
        return part.capitalize()

    return _WORD_RUN.sub(run, word)


_UTILITY = re.compile(
    r"\b(?:packer|fix|fixes|copier|copy|emulator|devpack|monst|lzh|disktool|checker|pick file"
    r"|file compare|popimenu|debug|virus killer)\b",
    re.IGNORECASE,
)


def content_for(raw: str) -> ContentRecord | None:
    """A list title with its bracketed notes ("(BOOT+DATA)", "(1 MEG)") moved to extra."""
    pieces = [piece.strip() for piece in raw.split("\t") if piece.strip()]
    if not pieces:
        return None
    title, notes = pieces[0], [piece.strip("() ") for piece in pieces[1:]]
    while (found := _TRAILING_NOTE.search(title)) and found.start() > 0:
        notes.insert(0, found.group("note").strip())
        title = title[: found.start()].rstrip()
    title = title.strip(" -")
    if not title:
        return None
    lowered = title.lower()
    if re.search(r"\bdocs?\b", lowered) and not re.search(r"\+\s*docs?\b", lowered):
        kind = "doc"
    elif _UTILITY.search(title):
        kind = "utility"
    elif re.search(r"\bdemo\b", lowered):
        kind = "demo"
    elif re.search(r"\bintro\b", lowered):
        kind = "intro"
    else:
        kind = "game"
    extra = ", ".join(note.lower() for note in notes if note)
    return ContentRecord(title=title_case(title), kind=kind, extra=extra)


# --- parsing -----------------------------------------------------------------


@dataclass(slots=True)
class ListDisk:
    heading: str
    code: str
    titles: list[str] = field(default_factory=list)


def parse_list(text: str) -> list[ListDisk]:
    """Disks of one crew list in file order, each with its section heading."""
    heading = ""
    disks: list[ListDisk] = []
    previous = -1
    for raw in text.splitlines():
        line = raw.rstrip(" \t\r\x1a")
        if not line.strip():
            continue
        found = _CODE_LINE.match(line)
        if found:
            number = int(_LEADING_NUMBER.search(found.group("code")).group(0))
            if disks and number < previous and disks[-1].titles:
                heading = " ".join(disks[-1].titles.pop().split())
            previous = number
            disks.append(ListDisk(heading, found.group("code").upper(), [found.group("title")]))
        elif not disks:
            heading = " ".join(line.split()).rstrip(":")
        else:
            disks[-1].titles.append(line.strip())
    return disks


def parse_index(text: str) -> Iterator[tuple[str, str, str]]:
    """(title, crew label, code) for every line of the merged title index."""
    for raw in text.splitlines():
        found = _INDEX_LINE.match(raw.rstrip("\r"))
        if found:
            yield found.group("title").strip(), found.group("disk").strip(), found.group("code")


def parse_doc_list(text: str) -> tuple[dict[str, str], list[tuple[str, str, str]]]:
    """The Sewer doc list: legend (code -> disk name) and (title, type, disk) rows."""
    legend: dict[str, str] = {}
    rows = []
    for raw in text.splitlines():
        line = raw.rstrip("\r")
        found = _DOC_LEGEND.match(line.strip())
        if found:
            legend[found.group("code")] = found.group("name").strip()
            continue
        found = _DOC_LINE.match(line)
        if found and found.group("title") != "Program Name":
            rows.append((found.group("title"), found.group("type"), found.group("disk")))
    return legend, rows


def parse_listing(page: str) -> list[str]:
    """Member paths of the ISO from the Internet Archive's archive listing page."""
    members = {
        urllib.parse.unquote(html.unescape(found.group("member")))
        for found in _MEMBER.finditer(page)
    }
    return sorted(member for member in members if not member.endswith("/"))


def member_url(member: str) -> str:
    return f"https://archive.org/download/{ITEM}/{ISO}/{urllib.parse.quote(member, safe='')}"


# --- identities ----------------------------------------------------------------

RawId = tuple[str, int, str, str]  # series, number, raw part, version


def _raw_id(ctx: BuildContext, text: str) -> RawId | None:
    found = ctx.series.match(INFO.id, text, PLATFORM)
    if found is None or found.number is None:
        return None
    version = normalise_version(found.version) if found.version else ""
    return found.series_id, found.number, found.part.strip("-_ ").upper(), version


def assign_parts(raw_ids: Iterable[RawId]) -> dict[RawId, tuple[str, str]]:
    """Final (part, version) for every raw identity of one list, in list order.

    One disk for a number keeps no part. Several disks for one number are
    parts A, B, C in list order, except a plain code followed by the same code
    with "B" and no "A" ("CD13", "CD13B"): those are two versions of the disk.
    """
    groups: dict[tuple[str, int, str], list[str]] = {}
    for series, number, part, version in raw_ids:
        parts = groups.setdefault((series, number, version), [])
        if part not in parts:
            parts.append(part)
    final: dict[RawId, tuple[str, str]] = {}
    for (series, number, version), parts in groups.items():
        if len(parts) == 1:
            part = parts[0]
            final[(series, number, part, version)] = (part if len(part) == 1 else "", version)
        elif parts == ["", "B"] and not version:
            final[(series, number, "", version)] = ("", "v1")
            final[(series, number, "B", version)] = ("", "v2")
        else:
            for index, part in enumerate(parts):
                final[(series, number, part, version)] = (chr(ord("A") + index), version)
    return final


def _lookup(final: dict[RawId, tuple[str, str]], raw: RawId) -> tuple[str, str] | None:
    """The final identity for a raw identity spelt differently ("A" for "", "013_2")."""
    if raw in final:
        return final[raw]
    series, number, part, version = raw
    alternative = {"A": "", "": "A"}.get(part)
    if alternative is not None:
        return final.get((series, number, alternative, version))
    return None


@dataclass(slots=True)
class Disk:
    series: str
    number: int
    part: str
    version: str
    contents: list[ContentRecord] = field(default_factory=list)
    locations: list[LocationRecord] = field(default_factory=list)

    def record(self, ctx: BuildContext, links: list[tuple[str, str]]) -> DiskRecord:
        series = ctx.series.get(self.series)
        return DiskRecord(
            source=INFO.id,
            platform=PLATFORM,
            kind=series.kind if series else "menu",
            series_key=self.series,
            number=self.number,
            part=self.part,
            version=self.version,
            contents=self.contents,
            locations=self.locations,
            links=list(links),
        )


class Builder:
    """Collects the disks of every list, keyed by (series, number, part, version)."""

    def __init__(self, ctx: BuildContext) -> None:
        self.ctx = ctx
        self.disks: dict[tuple[str, int, str, str], Disk] = {}
        self.final: dict[RawId, tuple[str, str]] = {}
        self.unmatched: list[str] = []

    def disk(self, raw: RawId) -> Disk:
        part, version = _lookup(self.final, raw) or (raw[2] if len(raw[2]) == 1 else "", raw[3])
        key = (raw[0], raw[1], part, version)
        if key not in self.disks:
            self.disks[key] = Disk(raw[0], raw[1], part, version)
        return self.disks[key]

    def add_list(self, path: str, text: str) -> None:
        entries = []
        for item in parse_list(text):
            raw = _raw_id(self.ctx, f"{path} {item.heading}: {item.code}")
            if raw is None:
                self.unmatched.append(f"{path} {item.heading}: {item.code}")
                continue
            entries.append((raw, item))
        final = assign_parts(raw for raw, _item in entries)
        self.final.update(final)
        for raw, item in entries:
            disk = self.disk(raw)
            for title in item.titles:
                content = content_for(title)
                if content is not None:
                    disk.contents.append(content)

    def add_index(self, text: str) -> None:
        seen = {
            key: {normalise(content.title) for content in disk.contents}
            for key, disk in self.disks.items()
        }
        for title, crew, code in parse_index(text):
            raw = _raw_id(self.ctx, f"{COMPLETE} {crew} #{code}")
            content = content_for(title)
            if raw is None or content is None:
                continue
            disk = self.disk(raw)
            key = (disk.series, disk.number, disk.part, disk.version)
            titles = seen.setdefault(key, set())
            if normalise(content.title) not in titles:
                titles.add(normalise(content.title))
                disk.contents.append(content)

    def add_doc_list(self, text: str) -> Iterator[DiskRecord]:
        """Contents of the Sewer doc disks; disks without a series rule stay unkeyed."""
        legend, rows = parse_doc_list(text)
        loose: dict[str, DiskRecord] = {}
        for title, kind_of_doc, code in rows:
            content = ContentRecord(title=title.strip(), kind="doc", extra=kind_of_doc.strip())
            raw = _raw_id(self.ctx, f"{DOC_LIST} {code}")
            if raw is not None:
                self.disk(raw).contents.append(content)
                continue
            name = legend.get(code) or legend.get(code[:1]) or f"Disk {code}"
            record = loose.get(code)
            if record is None:
                label = title_case(name)
                if code[1:].isdigit():
                    label = f"{label} {int(code[1:])}"
                record = loose[code] = DiskRecord(
                    source=INFO.id,
                    platform=PLATFORM,
                    kind="pack",
                    title=f"{label} (Sewer Software doc list)",
                    links=[("Internet Archive", DETAILS)],
                )
            record.contents.append(content)
        yield from loose.values()

    def add_image(self, member: str) -> bool:
        raw = _raw_id(self.ctx, member)
        if raw is None:
            return False
        self.disk(raw).locations.append(
            LocationRecord(
                provider="internet-archive",
                url=member_url(member),
                page_url=DETAILS,
                priority=LOCATION_PRIORITY,
            )
        )
        return True


def _fetch(ctx: BuildContext, url: str, name: str) -> str | None:
    try:
        return ctx.fetch_text(url, encoding="latin-1", name=name, max_age_days=MAX_AGE_DAYS)
    except (OSError, RuntimeError) as error:  # network errors and OfflineError
        ctx.log(f"crew-lists: could not fetch {url}: {error}")
        return None


def collect(ctx: BuildContext) -> Iterator[DiskRecord]:
    builder = Builder(ctx)
    listing = _fetch(ctx, LISTING, "atari-st-iso-listing.html")
    members = parse_listing(listing) if listing else []
    present = set(members)
    for path in LISTS:
        if present and path not in present:
            ctx.log(f"crew-lists: {path} is no longer in the ISO listing")
            continue
        text = _fetch(ctx, member_url(path), path.replace("/", "_"))
        if text is not None:
            builder.add_list(path, text)
    if not present or COMPLETE in present:
        text = _fetch(ctx, member_url(COMPLETE), COMPLETE.replace("/", "_"))
        if text is not None:
            builder.add_index(text)
    loose: list[DiskRecord] = []
    if not present or DOC_LIST in present:
        text = _fetch(ctx, member_url(DOC_LIST), DOC_LIST.replace("/", "_"))
        if text is not None:
            loose = list(builder.add_doc_list(text))
    images = [member for member in members if member.upper().endswith((".MSA", ".ST"))]
    placed = sum(builder.add_image(member) for member in images)
    for text in builder.unmatched[:20]:
        ctx.log(f"crew-lists: no series rule for {text!r}")
    ctx.log(
        f"crew-lists: {len(builder.disks)} disks, {len(loose)} unnumbered doc disks, "
        f"{placed} of {len(images)} images placed, {len(builder.unmatched)} list entries unmatched"
    )
    links = [("Internet Archive", DETAILS)]
    for disk in builder.disks.values():
        yield disk.record(ctx, links)
    yield from loose

"""Automation and D-Bug menu disks from the D-Bug search engine at d-bug.me.

The D-Bug site keeps a database of every Automation compact disk (0 to 512)
and every D-Bug menu (1 to 200): the games on each disk with publisher and
cracker, the menu coder, artist and musician, and separate entries for parts
("part A") and later versions ("Version 2"). D-Bug hosts its own menus as MSA
files, so D-Bug disks also get a download location.

The search form accepts SQL wildcards, so a title search for ``%`` lists a
whole group in one page. With menu credits switched on the site stops
writing part way through a long result, so credits for the disks missing
from that page are fetched one menu number at a time. Every page is cached
for 30 days and requests are spaced 1.5 seconds apart.

Pages with menu credits also show a screenshot of each menu (Mr. Sam's
pictures, under ``gfx/automenugfx`` and ``gfx/dbugmenugfx``). The picture's
address becomes a disc picture of the disk; the application fetches it when
it is shown.
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
    MediaRecordIn,
    SourceInfo,
)

INFO = SourceInfo(
    id="d-bug",
    name="D-Bug search engine",
    url="https://d-bug.me/",
    licence=NO_LICENCE_STATED,
)
CONTENT_PRIORITY = 15

SITE = "https://d-bug.me/"
SEARCH = SITE + "newsearch.php"
GROUPS = ("Automation", "D-Bug")  # values of the search form's group field
PLATFORM = "atari-st"
MAX_AGE_DAYS = 30.0
LOCATION_PRIORITY = 35
PICTURE_RANK = 20
PICTURE_CREDIT = "Menu screenshot: D-Bug archive, d-bug.me (screenshots by Mr. Sam)"

_HEADER = re.compile(
    r"<TD style=\"margin: 0 auto; background-color: blue; color: yellow\">"
    r"(?:<a href=\"(?P<href>[^\"]*)\">)?(?P<header>[^<]*)",
    re.IGNORECASE,
)
_END = re.compile(r"Matches Found:", re.IGNORECASE)
_CONTENT_CELL = re.compile(r"<TD ALIGN=LEFT[^>]*>(?P<cell>.*?)</TD>", re.IGNORECASE | re.DOTALL)
_ANCHOR = re.compile(
    r"<a href=\"+newsearch\.php\?[^\"]*?\b(?P<field>comp|crack)=[^\"]*\">(?P<text>.*?)</a>",
    re.IGNORECASE | re.DOTALL,
)
_TAG_NOTE = re.compile(r"\[(?P<tag>[A-Z0-9]{1,5})\]")
_CREDIT = re.compile(
    r">(?P<role>Code|Graphics|Music):</TD></TR><TR><TD ALIGN=RIGHT>(?P<value>.*?)</TD>",
    re.IGNORECASE | re.DOTALL,
)
_MARKUP = re.compile(r"<[^>]+>")
_PICTURE = re.compile(r"<img\s[^>]*?src=\"(?P<src>gfx/[^\"]*menugfx/[^\"]+)\"", re.IGNORECASE)
_NUMBER = re.compile(r"\bCD\s+(?P<number>\d+)\b", re.IGNORECASE)
_UNKNOWN = {"", "n/a", "unknown", "-", "?", "none"}

_KIND_WORDS = (
    ("doc", re.compile(r"^docs?\b|\bsolution\b|\binstructions\b", re.IGNORECASE)),
    ("cheat", re.compile(r"\bcheats?\b|\bhints?\b", re.IGNORECASE)),
    ("utility", re.compile(r"\b(?:packer|copier|utility|depacker|virus killer)\b", re.IGNORECASE)),
    ("demo", re.compile(r"\bdemo\b", re.IGNORECASE)),
    ("intro", re.compile(r"\bintro\b", re.IGNORECASE)),
    ("other", re.compile(r"^(?:CD|voting|menu|games?|disk) list", re.IGNORECASE)),
)


@dataclass(slots=True)
class Entry:
    """One result block of the search page: a disk, a part or a version."""

    header: str
    href: str = ""
    contents: list[ContentRecord] = field(default_factory=list)
    credits: str = ""
    picture: str = ""  # address of the menu screenshot, relative to the site
    complete: bool = False  # the block ended normally, so its credits are trustworthy


def _text(fragment: str) -> str:
    return " ".join(html.unescape(_MARKUP.sub(" ", fragment)).split())


def _person(text: str) -> str:
    return "" if text.strip().lower() in _UNKNOWN else text.strip()


def _version(version: str) -> str:
    return f"v{version}" if version[:1].isdigit() else version


def content_kind(title: str) -> str:
    for kind, pattern in _KIND_WORDS:
        if pattern.search(title):
            return kind
    return "game"


def _content(segment: str) -> ContentRecord | None:
    title_html, _, rest = segment.partition("<a ")
    title = _text(title_html)
    if not title:
        return None
    publisher = cracker = ""
    for anchor in _ANCHOR.finditer(segment):
        text = _text(anchor.group("text"))
        if anchor.group("field").lower() == "comp":
            publisher = _person(re.sub(r"^by\s+", "", text))
        else:
            cracker = _person(re.sub(r"^-?\s*Cracked by\s+", "", text, flags=re.IGNORECASE))
    tags = " ".join(f"[{found.group('tag')}]" for found in _TAG_NOTE.finditer(_text(rest)))
    kind = content_kind(title)
    if kind == "doc":
        title = re.sub(r"^Docs?\s*:-?\s*", "", title, flags=re.IGNORECASE) or title
        title = re.sub(r"\s*,\s*", ", ", title)
    return ContentRecord(title=title, kind=kind, publisher=publisher, cracker=cracker, extra=tags)


def _credits(block: str) -> str:
    parts = []
    for found in _CREDIT.finditer(block):
        value = _text(found.group("value"))
        if value.lower() not in _UNKNOWN:
            parts.append(f"{found.group('role').capitalize()}: {value}")
    return "; ".join(parts)


def parse_results(page: str) -> list[Entry]:
    """Every disk block on a search result page, in page order.

    A block counts as complete when another block or the closing match count
    follows it; the last block of a page the site cut short does not.
    """
    headers = [found for found in _HEADER.finditer(page) if found.group("header").strip()]
    end = _END.search(page)
    entries = []
    for index, found in enumerate(headers):
        stop = headers[index + 1].start() if index + 1 < len(headers) else None
        complete = stop is not None or end is not None
        block = page[found.end() : stop if stop is not None else (end.start() if end else None)]
        entry = Entry(
            header=" ".join(found.group("header").split()), href=found.group("href") or ""
        )
        cell = _CONTENT_CELL.search(block)
        if cell:
            for segment in re.split(r"<BR>", cell.group("cell"), flags=re.IGNORECASE):
                content = _content(segment)
                if content is not None:
                    entry.contents.append(content)
        entry.credits = _credits(block)
        picture = _PICTURE.search(block)
        entry.picture = html.unescape(picture.group("src")) if picture else ""
        entry.complete = complete and "Music:" in block
        entries.append(entry)
    return entries


def search_url(group: str, *, title: str = "", menu: str = "", credits: bool = True) -> str:
    query = urllib.parse.urlencode(
        {
            "cgroup": group,
            "game": title,
            "menu": menu,
            "menupic": "Yes" if credits else "No",
        }
    )
    return f"{SEARCH}?{query}"


def _fetch(ctx: BuildContext, url: str, name: str) -> str:
    return ctx.fetch_text(url, encoding="latin-1", name=name, max_age_days=MAX_AGE_DAYS)


def _group_entries(ctx: BuildContext, group: str) -> Iterator[Entry]:
    """All entries of one group, with credits, fetching as few pages as possible."""
    slug = re.sub(r"[^a-z0-9]+", "-", group.lower())
    listing = parse_results(
        _fetch(ctx, search_url(group, title="%", credits=False), f"{slug}.html")
    )
    if not listing:
        ctx.log(f"d-bug: the {group} listing is empty")
        return
    detailed = {
        entry.header: entry
        for entry in parse_results(
            _fetch(ctx, search_url(group, title="%"), f"{slug}-credits.html")
        )
        if entry.complete
    }
    missing = sorted(
        {
            int(found.group("number"))
            for entry in listing
            if entry.header not in detailed and (found := _NUMBER.search(entry.header))
        }
    )
    for number in missing:
        page = _fetch(ctx, search_url(group, menu=str(number)), f"{slug}-{number:03d}.html")
        for entry in parse_results(page):
            if entry.complete:
                detailed.setdefault(entry.header, entry)
    for entry in listing:
        full = detailed.get(entry.header)
        if full is not None:
            entry.credits = full.credits
            entry.href = entry.href or full.href
            entry.picture = entry.picture or full.picture
            if len(full.contents) >= len(entry.contents):
                entry.contents = full.contents
        yield entry


def records_for(ctx: BuildContext, group: str, entries: Iterable[Entry]) -> Iterator[DiskRecord]:
    for entry in entries:
        found = ctx.series.match(INFO.id, entry.header, PLATFORM)
        series = ctx.series.get(found.series_id) if found else None
        number_match = _NUMBER.search(entry.header)
        menu = number_match.group("number") if number_match else ""
        page = search_url(group, menu=menu) if menu else search_url(group, title="%")
        record = DiskRecord(
            source=INFO.id,
            platform=PLATFORM,
            kind=series.kind if series else "menu",
            series_key=found.series_id if found else "",
            number=found.number if found else None,
            part=found.part if found else "",
            version=_version(found.version) if found else "",
            title="" if found else entry.header,
            credits=entry.credits,
            contents=entry.contents,
            links=[("D-Bug", page)],
        )
        if entry.picture:
            record.media.append(
                MediaRecordIn(
                    kind="menu",
                    url=urllib.parse.urljoin(SITE, entry.picture),
                    source=INFO.id,
                    credit=PICTURE_CREDIT,
                    page_url=page,
                    rank=PICTURE_RANK,
                )
            )
        if entry.href.lower().endswith((".msa", ".st")):
            record.locations.append(
                LocationRecord(
                    provider="d-bug",
                    url=urllib.parse.urljoin(SITE, entry.href),
                    page_url=page,
                    priority=LOCATION_PRIORITY,
                )
            )
        yield record


def collect(ctx: BuildContext) -> Iterator[DiskRecord]:
    for group in GROUPS:
        count = pictures = 0
        for record in records_for(ctx, group, _group_entries(ctx, group)):
            if record.key is None:
                ctx.log(f"d-bug: no series rule for {record.title!r}")
            count += 1
            pictures += len(record.media)
            yield record
        ctx.log(f"d-bug: {count} {group} disks, {pictures} with a menu screenshot")

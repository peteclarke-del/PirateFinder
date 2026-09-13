"""The Automation compact disk catalogue on the Steem site.

Chris Edgar tested every Automation disk image he could find in the Steem
emulator and published the result in 2002 as one page: an entry per image file
(``A_000`` to ``A_512``, with ``A_069_1`` to ``A_069_5`` and ``A_148_A`` style
names for multi-disk menus), a list of what is on it, and notes such as
"(STE v.2)" for a second version or "(AVAILABLE IMAGE IS DAMAGED)". The list
is a cross-check for the D-Bug and Atari Legend contents and confirms the part
numbering. Its download links point at an FTP server that has gone, so no
locations are taken from it.
"""

from __future__ import annotations

import html
import re
from collections.abc import Iterator

from ..context import BuildContext
from ..records import NO_LICENCE_STATED, ContentRecord, DiskRecord, SourceInfo

INFO = SourceInfo(
    id="steem",
    name="Steem Automation compact disk catalogue",
    url="http://steem.atari.st/automation.htm",
    licence=NO_LICENCE_STATED,
)
CONTENT_PRIORITY = 40

PAGE = "http://steem.atari.st/automation.htm"
PLATFORM = "atari-st"

_ENTRY = re.compile(
    r"<a href=\"[^\"]*/(?P<file>A_[0-9A-Z_]+)\.ST\">[^<]*</a>(?P<body>.*?)(?=<hr>|$)",
    re.IGNORECASE | re.DOTALL,
)
_ITEM = re.compile(r"<li>(?P<item>.*?)</li>", re.IGNORECASE | re.DOTALL)
_MARKUP = re.compile(r"<[^>]+>")
_OFFENSIVE = re.compile(r"<b>\s*\[\*\]\s*</b>", re.IGNORECASE)
_VERSION_NOTE = re.compile(r"\bv\.?\s*(?P<version>\d+)\b", re.IGNORECASE)
_DAMAGED = re.compile(r"AVAILABLE IMAGE IS (?P<how>POSSIBLY )?DAMAGED", re.IGNORECASE)
# Qualifiers the list writes in brackets after a title; anything else in
# brackets is part of the title ("Barbarian II (Axe of Rage)").
_QUALIFIER = re.compile(
    r"\s*\((?P<note>1 meg[^)]*|french|german|italian|spanish|dutch|ste(?:/st)?|us version"
    r"|shareware version|release version|utility|demo|[a-e]|must be in drive [ab]"
    r"|doesn't work\??|part \d+)\)\s*$",
    re.IGNORECASE,
)
_WITH_DOCS = re.compile(r"\s+w/\s*docs?\b", re.IGNORECASE)


def _text(fragment: str) -> str:
    return " ".join(html.unescape(_MARKUP.sub(" ", fragment)).split())


def parse_item(text: str) -> ContentRecord | None:
    """One list line: a title with its qualifiers moved into ``extra``."""
    title = text.strip()
    if not title:
        return None
    kind = "game"
    notes: list[str] = []
    if title.startswith("(") and title.endswith(")"):
        inner = title[1:-1].strip()
        kind = "doc" if re.search(r"\bdocs?\b", inner, re.IGNORECASE) else "other"
        return ContentRecord(title=inner, kind=kind)
    while found := _QUALIFIER.search(title):
        note = found.group("note")
        lowered = note.lower()
        if lowered == "utility":
            kind = "utility"
        elif lowered == "demo":
            kind = "demo"
        elif len(note) == 1:
            notes.insert(0, f"disk {note.lower()}")
        else:
            notes.insert(0, note)
        title = title[: found.start()].rstrip()
    if _WITH_DOCS.search(title):
        title = _WITH_DOCS.sub("", title).strip()
        notes.append("with docs")
    return ContentRecord(title=title, kind=kind, extra=", ".join(notes))


def parse_page(page: str) -> Iterator[tuple[str, list[ContentRecord], list[str]]]:
    """(image file name, contents, notes) for every entry on the page."""
    for found in _ENTRY.finditer(page):
        body = found.group("body")
        notes: list[str] = []
        contents: list[ContentRecord] = []
        head, _, _ = body.partition("<li>")
        head_note = _text(head)
        items = [item.group("item") for item in _ITEM.finditer(body)]
        if items and re.fullmatch(
            r"\(.*v\.?\s*\d.*\)|\(STE\)", _text(_OFFENSIVE.sub("", items[0]))
        ):
            head_note = f"{head_note} {_text(_OFFENSIVE.sub('', items[0]))}".strip()
            items = items[1:]
        if head_note:
            notes.append(head_note.strip("() "))
        for item in items:  # a line with only an asterisk marks an offensive menu
            content = parse_item(_text(_OFFENSIVE.sub("", item)))
            if content is not None:
                contents.append(content)
        yield found.group("file").upper(), contents, notes


def records_from_page(ctx: BuildContext, page: str) -> Iterator[DiskRecord]:
    for name, contents, notes in parse_page(page):
        found = ctx.series.match(INFO.id, name, PLATFORM)
        series = ctx.series.get(found.series_id) if found else None
        version = found.version if found else ""
        if version[:1].isdigit():
            version = f"v{version}"
        remarks = []
        for note in notes:
            if (version_note := _VERSION_NOTE.search(note)) and not version:
                version = f"v{version_note.group('version')}"
            damaged = _DAMAGED.search(note)
            if damaged:
                how = "possibly damaged" if damaged.group("how") else "damaged"
                remarks.append(f"The image tested for the Steem list was {how}.")
            elif not _VERSION_NOTE.search(note) and note.upper() != "STE":
                remarks.append(f"Steem list: {note.capitalize()}.")
            if re.search(r"\bSTE\b", note):
                remarks.append("Steem list: needs an STE.")
        yield DiskRecord(
            source=INFO.id,
            platform=PLATFORM,
            kind=series.kind if series else "menu",
            series_key=found.series_id if found else "",
            number=found.number if found else None,
            part=found.part if found else "",
            version=version,
            title="" if found else name,
            notes=" ".join(remarks),
            contents=contents,
        )


def collect(ctx: BuildContext) -> Iterator[DiskRecord]:
    page = ctx.fetch_text(PAGE, encoding="latin-1", max_age_days=30)
    count = 0
    for record in records_from_page(ctx, page):
        if record.key is None:
            ctx.log(f"steem: no series rule for {record.title!r}")
        count += 1
        yield record
    ctx.log(f"steem: {count} disks")

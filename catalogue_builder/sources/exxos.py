"""Persistence of Vision demo compilations from exxos's Atari pages.

exxos keeps a gallery of the Persistence of Vision (POV) demo compilation
disks for the Atari ST: one cell per disk with a heading such as "POV_001",
the demos on the disk one per line, and a link to a zip of the disk image
named after the disk number, its version and year ("006v2.0(1989).zip").

The gallery is split over several pages linked from the first. Each cell
becomes a numbered disk record with its contents and the zip as a download
location. The series is recognised from the heading with the rules in
``data/series/match-exxos.toml``.
"""

from __future__ import annotations

import html.parser
import re
import urllib.parse
from collections.abc import Iterator
from dataclasses import dataclass, field

from ..context import BuildContext
from ..records import ContentRecord, DiskRecord, LocationRecord, SourceInfo

INFO = SourceInfo(
    id="exxos",
    name="exxos Atari pages",
    url="https://www.exxosforum.co.uk/atari/",
)
CONTENT_PRIORITY = 30

FIRST_PAGE = "https://www.exxosforum.co.uk/atari/games/POV/page1.htm"
PLATFORM = "atari-st"
# A small hobby site: one request every two seconds, pages kept for a month.
MIN_INTERVAL = 2.0
MAX_AGE_DAYS = 30.0

_PAGE_LINK = re.compile(r"^page\d+\.html?$", re.IGNORECASE)
# The zip name: number, optional version, year in brackets, then flags.
_ZIP_NAME = re.compile(
    r"^(?P<number>\d+)\s*(?:v(?P<version>[\d.]+))?\s*\((?P<year>[\dx]{4})\)(?P<rest>.*)\.zip$",
    re.IGNORECASE,
)
_FLAG = re.compile(r"\[([^\]]+)\]")
_INTRO = re.compile(r"^intro\s*:\s*", re.IGNORECASE)


@dataclass(slots=True)
class Entry:
    """One disk cell of a gallery page."""

    heading: str
    lines: list[str] = field(default_factory=list)
    href: str = ""


class _GalleryParser(html.parser.HTMLParser):
    """Collects each h3 heading, the lines after it and its zip link."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.entries: list[Entry] = []
        self.page_links: list[str] = []
        self._heading: list[str] | None = None
        self._entry: Entry | None = None
        self._line: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "h3":
            self._finish_entry()
            self._heading = []
        elif tag == "br":
            self._finish_line()
        elif tag == "a":
            href = (dict(attrs).get("href") or "").strip()
            path = urllib.parse.urlsplit(href).path
            if _PAGE_LINK.match(path.rsplit("/", 1)[-1]):
                self.page_links.append(href)
            elif self._entry is not None and path.lower().endswith(".zip"):
                self._finish_line()
                self._entry.href = self._entry.href or href

    def handle_endtag(self, tag: str) -> None:
        if tag == "h3" and self._heading is not None:
            heading = " ".join("".join(self._heading).split())
            self._heading = None
            self._entry = Entry(heading=heading)
            self._line = []
        elif tag in ("div", "td", "table"):
            self._finish_entry()

    def handle_data(self, data: str) -> None:
        if self._heading is not None:
            self._heading.append(data)
        elif self._entry is not None:
            self._line.append(data)

    def _finish_line(self) -> None:
        if self._entry is not None:
            line = " ".join("".join(self._line).split())
            if line:
                self._entry.lines.append(line)
        self._line = []

    def _finish_entry(self) -> None:
        if self._entry is not None:
            self._finish_line()
            self.entries.append(self._entry)
        self._entry = None

    def close(self) -> None:
        super().close()
        self._finish_entry()


def parse_page(text: str) -> tuple[list[Entry], list[str]]:
    """The disk cells of a gallery page and the links to its other pages."""
    parser = _GalleryParser()
    parser.feed(text)
    parser.close()
    return parser.entries, parser.page_links


def decode(data: bytes) -> str:
    """The pages declare ISO-8859-1; UTF-8 is tried first in case that changes."""
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data.decode("cp1252", errors="replace")


def content(line: str) -> ContentRecord:
    """One line of a cell: a demo, or an intro when it starts with "intro :"."""
    if _INTRO.match(line):
        return ContentRecord(title=_INTRO.sub("", line).strip(), kind="intro")
    return ContentRecord(title=line, kind="demo")


def entry_record(ctx: BuildContext, entry: Entry, page_url: str) -> DiskRecord | None:
    """The disk record for one cell, None if its heading names no known series."""
    found = ctx.series.match(INFO.id, entry.heading, platform=PLATFORM)
    if found is None or found.number is None:
        return None
    series = ctx.series.get(found.series_id)
    version = found.version
    date = ""
    flags: list[str] = []
    record = DiskRecord(
        source=INFO.id,
        platform=PLATFORM,
        kind=series.kind if series else "pack",
        series_key=found.series_id,
        number=found.number,
        part=found.part,
        # The disk is labelled from its series and titled from TOSEC; the cell
        # heading ("POV_001") adds nothing to either.
        contents=[content(line) for line in entry.lines],
        links=[("exxos", page_url)],
    )
    if entry.href:
        url = urllib.parse.urljoin(page_url, entry.href)
        file_name = urllib.parse.unquote(urllib.parse.urlsplit(url).path.rsplit("/", 1)[-1])
        named = _ZIP_NAME.match(file_name)
        if named:
            # The zip is the version the page offers, which may be a later
            # edition than the first ("006v2.0(1989).zip").
            version = version or (named.group("version") or "")
            year = named.group("year")
            date = year if year.isdigit() else ""
            flags = _FLAG.findall(named.group("rest"))
        record.locations.append(
            LocationRecord(
                provider=INFO.id,
                url=url,
                container="zip",
                member="",
                page_url=page_url,
                priority=40,
            )
        )
    record.version = version
    record.date = date
    record.notes = " ".join(f"[{flag}]" for flag in flags)
    return record


def collect(ctx: BuildContext) -> Iterator[DiskRecord]:
    pages = [FIRST_PAGE]
    seen = {FIRST_PAGE}
    emitted = skipped = 0
    while pages:
        page_url = pages.pop(0)
        text = decode(
            ctx.fetch(page_url, min_interval=MIN_INTERVAL, max_age_days=MAX_AGE_DAYS).read_bytes()
        )
        entries, links = parse_page(text)
        for link in links:
            url = urllib.parse.urljoin(page_url, link)
            if url not in seen:
                seen.add(url)
                pages.append(url)
        for entry in entries:
            record = entry_record(ctx, entry, page_url)
            if record is None:
                skipped += 1
                continue
            emitted += 1
            yield record
    ctx.log(
        f"{INFO.id}: {emitted} disks from {len(seen)} pages, "
        f"{skipped} headings not recognised by any series rule"
    )

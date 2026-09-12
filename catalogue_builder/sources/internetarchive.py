"""Download locations for TOSEC-named disk images on the Internet Archive.

The Internet Archive holds several mirrors of TOSEC-named Atari ST and Amiga
disk images. Some are items of loose zips, one disk image per zip (the Amiga
TOSEC mirrors in the Software Capsules collection and an older Atari ST
compilations set). Others are single multi-gigabyte zip or 7z archives, from
which the Archive serves one member at a time, so a single disk can still be
fetched on its own.

This importer only says where images can be downloaded. Every record it
produces is location-only: no series, no number and no image, just locations
whose ``image_name`` is the TOSEC name of the disk image inside the zip. The
merge step attaches each location to the disk that owns an image of that
name, which the TOSEC importer provides, and drops the ones it cannot place.

The Archive publishes hashes of the zips, not of the images inside them, so
the locations carry no hash; the application checks a download against the
hash of the catalogue image the location is attached to.
"""

from __future__ import annotations

import html.parser
import json
import re
import urllib.parse
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import PurePosixPath

from ..context import BuildContext, OfflineError
from ..records import DiskRecord, LocationRecord, SourceInfo

INFO = SourceInfo(
    id="internet-archive",
    name="Internet Archive",
    url="https://archive.org",
    licence="",
)

ARCHIVE = "https://archive.org"
# The Archive's metadata and archive listings of these old sets rarely change.
LISTING_MAX_AGE = 30.0
# One request a second at most; the Archive answers bursts with 429.
MIN_INTERVAL = 1.0


@dataclass(frozen=True, slots=True)
class ArchiveSet:
    """A zip or 7z archive in an item whose members the Archive serves singly."""

    item: str
    file: str
    platform: str
    kind: str
    priority: int
    include: tuple[str, ...] = ()  # folders to read, all of them when empty


@dataclass(frozen=True, slots=True)
class ItemSet:
    """An item whose loose zip files each hold one disk image."""

    item: str
    platform: str
    kind: str
    priority: int


@dataclass(frozen=True, slots=True)
class Discovery:
    """Items found with a search, kept when their title matches ``title``.

    The Amiga TOSEC mirrors are split into many items whose identifiers do not
    follow their titles (``commodore-amiga-games-adf-r_202301`` holds the T
    games), so they are found by search rather than by guessing identifiers.
    """

    query: str
    title: str  # regular expression, matched against the item title
    platform: str
    kind: str
    priority: int


# The same item's "[Games].7z" and "[Compilations - Games].7z" are left out:
# their zips hold several disks each under names that are not TOSEC names, so
# none of them could be tied to a catalogue image.
ARCHIVE_SETS = (
    ArchiveSet("atari-st-collection", "[Menus].7z", "atari-st", "menu", 20),
    # The 2012 TOSEC snapshot is older than the DATs the catalogue is built
    # from, so its names match fewer disks and it is tried after the others.
    # Only the compilations and games are read: the collections, coverdisks,
    # diskmags and loose demos in it are outside the catalogue.
    ArchiveSet(
        "Atari_ST_TOSEC_2012_04_23",
        "Atari_ST_TOSEC_2012_04_23.zip",
        "atari-st",
        "",
        25,
        include=("Atari ST [TOSEC]/Compilations/", "Atari ST [TOSEC]/Games/"),
    ),
)
ITEM_SETS = (
    ItemSet("ROMs_-_Atari_ST_-_Compilations-Demos-20050110-Update", "atari-st", "compilation", 25),
)
DISCOVERIES = (
    Discovery(
        "identifier:commodore-amiga-*",
        r"^Commodore Amiga - (?:Games - \[ADF\]|Compilations - |Demos - (?:Packs|Music)$)",
        "amiga",
        "",
        20,
    ),
)

# The raw image format each platform's TOSEC sets use, when a path does not
# say otherwise. A TOSEC folder named after its format ("[STX]") overrides it.
DEFAULT_FORMAT = {"atari-st": "st", "amiga": "adf"}
IMAGE_FORMATS = frozenset({"st", "stx", "msa", "adf", "dms", "adz", "ipf"})
_FOLDER_FORMAT = re.compile(r"\[(?P<format>[A-Za-z0-9]+)\]")


# --- the archive listing -----------------------------------------------------


@dataclass(frozen=True, slots=True)
class Member:
    path: str  # path inside the archive, as the listing shows it
    url: str  # absolute download URL of this one member
    size: int | None


class _ListingParser(html.parser.HTMLParser):
    """Reads the rows of the table on an Archive "view archive" page.

    Each file row has the member path as the text of a link in its first
    cell and the size in its last cell. Folder rows have no link.
    """

    def __init__(self, base_url: str) -> None:
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.members: list[Member] = []
        self._table_depth = 0
        self._cells: list[list[str]] | None = None
        self._href: str | None = None
        self._link_text: list[str] | None = None
        self._first_link: tuple[str, str] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        if tag == "table":
            classes = (attributes.get("class") or "").split()
            if self._table_depth or "archext" in classes:
                self._table_depth += 1
        elif not self._table_depth:
            return
        elif tag == "tr":
            self._finish_row()
            self._cells = []
            self._first_link = None
        elif tag in ("td", "th") and self._cells is not None:
            self._cells.append([])
        elif tag == "a" and self._cells is not None and attributes.get("href"):
            self._href = attributes["href"]
            self._link_text = []

    def handle_endtag(self, tag: str) -> None:
        if not self._table_depth:
            return
        if tag == "a" and self._link_text is not None:
            if self._first_link is None and self._href and self._cells and len(self._cells) == 1:
                self._first_link = (self._href, "".join(self._link_text).strip())
            self._href = None
            self._link_text = None
        elif tag == "tr":
            self._finish_row()
        elif tag == "table":
            self._finish_row()
            self._table_depth -= 1

    def handle_data(self, data: str) -> None:
        if self._link_text is not None:
            self._link_text.append(data)
        if self._cells:
            self._cells[-1].append(data)

    def _finish_row(self) -> None:
        cells, link = self._cells, self._first_link
        self._cells = None
        self._first_link = None
        if not cells or link is None:
            return
        href, path = link
        size_text = "".join(cells[-1]).strip() if len(cells) > 1 else ""
        self.members.append(
            Member(
                path=path,
                url=urllib.parse.urljoin(self.base_url, href),
                size=int(size_text) if size_text.isdigit() else None,
            )
        )

    def close(self) -> None:
        super().close()
        self._finish_row()


def parse_listing(text: str, base_url: str) -> list[Member]:
    """Every file in an Archive "view archive" page, with its download URL."""
    parser = _ListingParser(base_url)
    parser.feed(text)
    parser.close()
    return parser.members


# --- naming ------------------------------------------------------------------


def image_name(path: str, platform: str) -> str | None:
    """The TOSEC name of the disk image inside the zip at ``path``.

    TOSEC zips hold one image named like the zip itself, so the image name is
    the zip name with the image extension. The extension comes from the
    nearest folder named after an image format, else from the platform.
    Returns None for members that are not zips.
    """
    pure = PurePosixPath(path)
    if pure.suffix.lower() != ".zip" or not pure.stem:
        return None
    image_format = DEFAULT_FORMAT.get(platform, "")
    for folder in reversed(pure.parent.parts):
        tags = [tag.lower() for tag in _FOLDER_FORMAT.findall(folder)]
        known = [tag for tag in tags if tag in IMAGE_FORMATS]
        if known:
            image_format = known[-1]
            break
    return f"{pure.stem}.{image_format}" if image_format else pure.stem


def details_url(item: str) -> str:
    return f"{ARCHIVE}/details/{urllib.parse.quote(item)}"


def download_url(item: str, file: str) -> str:
    return f"{ARCHIVE}/download/{urllib.parse.quote(item)}/{urllib.parse.quote(file)}"


def location_record(
    *,
    url: str,
    name: str,
    platform: str,
    kind: str,
    item: str,
    size: int | None,
    priority: int,
) -> DiskRecord:
    """A location-only record for one zipped image."""
    return DiskRecord(
        source=INFO.id,
        platform=platform,
        kind=kind or "single",
        title=PurePosixPath(name).stem,
        locations=[
            LocationRecord(
                provider=INFO.id,
                url=url,
                container="zip",
                member="",
                size=size,
                page_url=details_url(item),
                priority=priority,
                image_name=name,
            )
        ],
    )


# --- collecting --------------------------------------------------------------

# Network failures (URLError and HTTPError are OSErrors), a cache miss while
# building offline, and a malformed JSON answer.
_UNAVAILABLE = (OSError, OfflineError, ValueError)


def archive_records(ctx: BuildContext, spec: ArchiveSet) -> Iterator[DiskRecord]:
    listing_url = download_url(spec.item, spec.file) + "/"
    text = ctx.fetch_text(
        listing_url,
        name=f"{spec.item}-listing.html",
        max_age_days=LISTING_MAX_AGE,
        min_interval=MIN_INTERVAL,
    )
    for member in parse_listing(text, listing_url):
        if spec.include and not member.path.startswith(spec.include):
            continue
        name = image_name(member.path, spec.platform)
        if name is None:
            continue
        yield location_record(
            url=member.url,
            name=name,
            platform=spec.platform,
            kind=spec.kind,
            item=spec.item,
            size=member.size,
            priority=spec.priority,
        )


def item_files(ctx: BuildContext, item: str) -> list[dict]:
    """The files of an item from the metadata API, [] if the item is unavailable."""
    text = ctx.fetch_text(
        f"{ARCHIVE}/metadata/{urllib.parse.quote(item)}",
        name=f"{item}.json",
        max_age_days=LISTING_MAX_AGE,
        min_interval=MIN_INTERVAL,
    )
    document = json.loads(text or "{}")
    if not isinstance(document, dict) or document.get("is_dark"):
        return []
    files = document.get("files", [])
    return files if isinstance(files, list) else []


def item_records(ctx: BuildContext, spec: ItemSet) -> Iterator[DiskRecord]:
    for entry in item_files(ctx, spec.item):
        file = str(entry.get("name", ""))
        name = image_name(file, spec.platform)
        if name is None or entry.get("source", "original") != "original":
            continue
        size = str(entry.get("size", ""))
        yield location_record(
            url=download_url(spec.item, file),
            name=name,
            platform=spec.platform,
            kind=spec.kind,
            item=spec.item,
            size=int(size) if size.isdigit() else None,
            priority=spec.priority,
        )


def discover(ctx: BuildContext, spec: Discovery) -> list[tuple[str, str]]:
    """Identifiers and titles of the items ``spec`` finds, sorted by identifier."""
    title = re.compile(spec.title)
    found: dict[str, str] = {}
    cursor = ""
    for _page in range(100):
        query = {"q": spec.query, "fields": "identifier,title", "count": "1000"}
        if cursor:
            query["cursor"] = cursor
        url = f"{ARCHIVE}/services/search/v1/scrape?{urllib.parse.urlencode(query)}"
        document = json.loads(
            ctx.fetch_text(
                url, name="scrape.json", max_age_days=LISTING_MAX_AGE, min_interval=MIN_INTERVAL
            )
        )
        for entry in document.get("items", []):
            identifier = str(entry.get("identifier", ""))
            name = entry.get("title", "")
            name = " ".join(name) if isinstance(name, list) else str(name)
            if identifier and title.search(name.strip()):
                found[identifier] = name.strip()
        cursor = document.get("cursor") or ""
        if not cursor:
            break
    return sorted(found.items())


def collect(ctx: BuildContext) -> Iterator[DiskRecord]:
    counts: dict[str, int] = {}

    def run(label: str, records: Iterator[DiskRecord]) -> Iterator[DiskRecord]:
        count = 0
        try:
            for record in records:
                count += 1
                yield record
        except _UNAVAILABLE as error:
            ctx.log(f"{INFO.id}: {label} unavailable, skipped: {error}")
        counts[label] = count
        ctx.log(f"{INFO.id}: {label}: {count} locations")

    for spec in ARCHIVE_SETS:
        yield from run(f"{spec.item}/{spec.file}", archive_records(ctx, spec))
    for spec in ITEM_SETS:
        yield from run(spec.item, item_records(ctx, spec))
    for discovery in DISCOVERIES:
        try:
            items = discover(ctx, discovery)
        except _UNAVAILABLE as error:
            ctx.log(f"{INFO.id}: search {discovery.query!r} failed, skipped: {error}")
            continue
        ctx.log(f"{INFO.id}: search {discovery.query!r} found {len(items)} items")
        for identifier, _title in items:
            spec = ItemSet(identifier, discovery.platform, discovery.kind, discovery.priority)
            yield from run(identifier, item_records(ctx, spec))
    ctx.log(f"{INFO.id}: {sum(counts.values())} locations from {len(counts)} sets")

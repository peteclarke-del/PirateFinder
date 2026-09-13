"""Download locations for TOSEC-named disk images on the Internet Archive.

The Internet Archive holds several mirrors of TOSEC-named Atari ST and Amiga
disk images. Some are items of loose zips, one disk image per zip (the Amiga
TOSEC mirrors in the Software Capsules collection and an older Atari ST
compilations set). Others are single multi-gigabyte zip or 7z archives, from
which the Archive serves one member at a time, so a single disk can still be
fetched on its own.

This importer only says where images can be downloaded. Each zip becomes
one download location, placed in one of three ways, tried in this order:

1. By hash. Many items are copies of TOSEC sets made years ago, and their
   zips carry the TOSEC names of that time. When an older TOSEC DAT
   (``data/old-tosec-dats.toml``) knows the name, the location carries the
   hash of that image (SHA-1, else MD5, else CRC32 with the image size), so
   the merge attaches it to the catalogue image with that hash whatever it
   is called now, and the application checks the download against it. For
   each item the DATs are asked in order of how close their release is to
   the item's upload date.
2. By series. Menu zips with short names ("[Menus]/P/Pompey Pirates/
   PP_054.zip") are recognised with the rules in
   ``data/series/match-internet-archive.toml``, matched against the member
   path without ".zip". They become keyed records that only attach to a disc
   other sources describe (``DiskRecord.attach_only``). They carry no hash:
   the application checks the download against the disc's known dumps.
3. By name. Every other location is location-only, with the zip's name as
   the TOSEC name of the image inside (``image_name``); the merge attaches
   it to the disk that owns an image of that name and drops the ones it
   cannot place.

The Archive publishes hashes of the zips, not of the images inside them, so
only the old DATs give a location a hash.
"""

from __future__ import annotations

import datetime
import hashlib
import html.parser
import json
import re
import tomllib
import urllib.parse
import zipfile
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from ..context import BuildContext, OfflineError
from ..records import DiskRecord, LocationRecord, SourceInfo, image_name_keys
from ..series import DATA_DIR
from . import tosec

INFO = SourceInfo(
    id="internet-archive",
    name="Internet Archive",
    url="https://archive.org",
    licence="archive.org terms of use",
)

ARCHIVE = "https://archive.org"
# The Archive's metadata and archive listings of these old sets rarely change.
LISTING_MAX_AGE = 30.0
OLD_DATS_FILE = DATA_DIR.parent / "old-tosec-dats.toml"
# An old DAT is a dated file that never changes: it is downloaded once.
OLD_DAT_MAX_AGE = 36500.0


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


# --- older TOSEC names ---------------------------------------------------------


@dataclass(frozen=True, slots=True)
class OldDat:
    """An older TOSEC DAT pack or DAT, as ``data/old-tosec-dats.toml`` lists it."""

    id: str
    released: datetime.date
    url: str
    sha1: str = ""
    dats: tuple[re.Pattern[str], ...] = ()  # DATs to read from a pack; all when empty

    def wanted(self, dat_name: str) -> bool:
        return not self.dats or any(pattern.match(dat_name) for pattern in self.dats)


def load_old_dats(path: Path = OLD_DATS_FILE) -> list[OldDat]:
    with path.open("rb") as handle:
        document = tomllib.load(handle)
    return [
        OldDat(
            id=entry["id"],
            released=datetime.date.fromisoformat(str(entry["released"])),
            url=entry["url"],
            sha1=entry.get("sha1", "").lower(),
            dats=tuple(re.compile(pattern) for pattern in entry.get("dats", [])),
        )
        for entry in document.get("dat", [])
    ]


@dataclass(frozen=True, slots=True)
class ImageHash:
    """The hash a location is checked and placed by."""

    kind: str  # "sha1", "md5" or "crc32"
    value: str
    size: int | None = None  # the image size, which a CRC32 needs


def image_hash(rom: dict[str, str]) -> ImageHash | None:
    """SHA-1, else MD5, else CRC32 with the size, of one DAT rom."""
    for kind, field in (("sha1", "sha1"), ("md5", "md5")):
        if rom.get(field, "").strip():
            return ImageHash(kind, rom[field].strip().lower())
    size = rom.get("size", "")
    if rom.get("crc", "").strip() and size.isdigit():
        return ImageHash("crc32", rom["crc"].strip().lower(), int(size))
    return None


class OldNames:
    """The image hashes of the names in older TOSEC DATs."""

    def __init__(self, indexes: list[tuple[OldDat, dict[str, ImageHash]]]) -> None:
        self.indexes = indexes

    @classmethod
    def load(cls, ctx: BuildContext, definitions: list[OldDat] | None = None) -> OldNames:
        """Download (once) and read every old DAT; one that fails is logged and left out."""
        indexes = []
        for definition in load_old_dats() if definitions is None else definitions:
            try:
                index = _read_old_dat(ctx, definition)
            except (*_UNAVAILABLE, zipfile.BadZipFile) as error:
                ctx.log(f"{INFO.id}: old DAT {definition.id} unavailable, skipped: {error}")
                continue
            ctx.log(f"{INFO.id}: old DAT {definition.id}: {len(index)} image names")
            indexes.append((definition, index))
        return cls(indexes)

    def lookup(self, name: str, uploaded: datetime.date | None) -> ImageHash | None:
        """The hash of the image an old DAT names ``name``, asking the DAT
        released nearest to ``uploaded`` first (the newest first when the
        upload date is not known)."""
        if uploaded is None:
            order = sorted(self.indexes, key=lambda item: item[0].released, reverse=True)
        else:
            order = sorted(self.indexes, key=lambda item: abs(item[0].released - uploaded))
        key = _old_key(name)
        for _definition, index in order:
            found = index.get(key)
            if found is not None:
                return found
        return None


def _old_key(name: str) -> str:
    """An image name as the old DATs are looked up by: the file name without
    folders or case, extension included, so a name never finds the dump of
    another format."""
    return image_name_keys(name)[0]


def _read_old_dat(ctx: BuildContext, definition: OldDat) -> dict[str, ImageHash]:
    base = urllib.parse.unquote(PurePosixPath(urllib.parse.urlsplit(definition.url).path).name)
    path = ctx.fetch(definition.url, name=base, max_age_days=OLD_DAT_MAX_AGE)
    if definition.sha1:
        digest = hashlib.sha1()
        with path.open("rb") as handle:
            while chunk := handle.read(1 << 20):
                digest.update(chunk)
        if digest.hexdigest() != definition.sha1:
            if not ctx.offline:
                path.unlink(missing_ok=True)  # fetched again next time
            raise ValueError(f"{base} has SHA-1 {digest.hexdigest()}, not {definition.sha1}")
    index: dict[str, ImageHash] = {}
    single = not zipfile.is_zipfile(path)
    for dat_name, _version, opener in tosec.dat_files(path):
        if not single and not definition.wanted(dat_name):
            continue
        with opener() as handle:
            for _game, roms in tosec.dat_games(handle):
                for rom in roms:
                    found = image_hash(rom)
                    if found is not None and rom.get("name"):
                        index.setdefault(_old_key(rom["name"]), found)
    return index


# --- records -------------------------------------------------------------------


def location_record(
    *,
    url: str,
    name: str,
    platform: str,
    kind: str,
    item: str,
    size: int | None,
    priority: int,
    found: ImageHash | None = None,
) -> DiskRecord:
    """A location-only record for one zipped image, placed by ``found`` when
    an old DAT gives its hash, otherwise by its name."""
    location = LocationRecord(
        provider=INFO.id,
        url=url,
        container="zip",
        member="",
        size=size,
        page_url=details_url(item),
        priority=priority,
        image_name=name,
    )
    if found is not None:
        location.hash_kind, location.hash_value = found.kind, found.value
        if found.size is not None:
            location.size = found.size  # a CRC32 identifies an image only with its size
    return DiskRecord(
        source=INFO.id,
        platform=platform,
        kind=kind or "single",
        title=PurePosixPath(name).stem,
        locations=[location],
    )


def series_record(
    ctx: BuildContext,
    *,
    path: str,
    url: str,
    platform: str,
    item: str,
    size: int | None,
    priority: int,
) -> DiskRecord | None:
    """A keyed record for a zip whose path a series rule recognises, else None.

    The record only attaches to a disc another source describes; its
    location names no image and carries no hash.
    """
    found = ctx.series.match(INFO.id, str(PurePosixPath(path).with_suffix("")), platform)
    if found is None or found.number is None:
        return None
    definition = ctx.series.get(found.series_id)
    return DiskRecord(
        source=INFO.id,
        platform=platform,
        kind=definition.kind if definition else "menu",
        series_key=found.series_id,
        number=found.number,
        part=found.part,
        version=found.version,
        attach_only=True,
        locations=[
            LocationRecord(
                provider=INFO.id,
                url=url,
                container="zip",
                member="",
                size=size,
                page_url=details_url(item),
                priority=priority,
            )
        ],
    )


def zip_record(
    ctx: BuildContext,
    *,
    path: str,
    url: str,
    platform: str,
    kind: str,
    item: str,
    size: int | None,
    priority: int,
    old: OldNames | None,
    uploaded: datetime.date | None,
) -> DiskRecord | None:
    """The record for one zip of an item: by hash, by series or by name."""
    name = image_name(path, platform)
    if name is None:
        return None
    found = old.lookup(name, uploaded) if old is not None else None
    if found is None:
        keyed = series_record(
            ctx, path=path, url=url, platform=platform, item=item, size=size, priority=priority
        )
        if keyed is not None:
            return keyed
    return location_record(
        url=url,
        name=name,
        platform=platform,
        kind=kind,
        item=item,
        size=size,
        priority=priority,
        found=found,
    )


# --- collecting --------------------------------------------------------------

# Network failures (URLError and HTTPError are OSErrors), a cache miss while
# building offline, and a malformed JSON answer.
_UNAVAILABLE = (OSError, OfflineError, ValueError)


def archive_records(
    ctx: BuildContext, spec: ArchiveSet, old: OldNames | None = None
) -> Iterator[DiskRecord]:
    listing_url = download_url(spec.item, spec.file) + "/"
    text = ctx.fetch_text(
        listing_url,
        name=f"{spec.item}-listing.html",
        max_age_days=LISTING_MAX_AGE,
    )
    uploaded = item_uploaded(ctx, spec.item) if old is not None else None
    for member in parse_listing(text, listing_url):
        if spec.include and not member.path.startswith(spec.include):
            continue
        record = zip_record(
            ctx,
            path=member.path,
            url=member.url,
            platform=spec.platform,
            kind=spec.kind,
            item=spec.item,
            size=member.size,
            priority=spec.priority,
            old=old,
            uploaded=uploaded,
        )
        if record is not None:
            yield record


def item_metadata(ctx: BuildContext, item: str) -> dict:
    """An item's metadata API answer, {} if the item is unavailable."""
    text = ctx.fetch_text(
        f"{ARCHIVE}/metadata/{urllib.parse.quote(item)}",
        name=f"{item}.json",
        max_age_days=LISTING_MAX_AGE,
    )
    document = json.loads(text or "{}")
    if not isinstance(document, dict) or document.get("is_dark"):
        return {}
    return document


def item_files(ctx: BuildContext, item: str) -> list[dict]:
    """The files of an item from the metadata API, [] if the item is unavailable."""
    files = item_metadata(ctx, item).get("files", [])
    return files if isinstance(files, list) else []


def uploaded_date(document: dict) -> datetime.date | None:
    """The day an item was made public, from its metadata."""
    metadata = document.get("metadata")
    text = str(metadata.get("publicdate", "")) if isinstance(metadata, dict) else ""
    try:
        return datetime.date.fromisoformat(text[:10])
    except ValueError:
        return None


def item_uploaded(ctx: BuildContext, item: str) -> datetime.date | None:
    """The upload date of an item whose files are read from a listing, or None."""
    try:
        return uploaded_date(item_metadata(ctx, item))
    except _UNAVAILABLE as error:
        ctx.log(f"{INFO.id}: no upload date for {item}: {error}")
        return None


def item_records(
    ctx: BuildContext, spec: ItemSet, old: OldNames | None = None
) -> Iterator[DiskRecord]:
    document = item_metadata(ctx, spec.item)
    files = document.get("files", [])
    uploaded = uploaded_date(document)
    for entry in files if isinstance(files, list) else []:
        file = str(entry.get("name", ""))
        if entry.get("source", "original") != "original":
            continue
        size = str(entry.get("size", ""))
        record = zip_record(
            ctx,
            path=file,
            url=download_url(spec.item, file),
            platform=spec.platform,
            kind=spec.kind,
            item=spec.item,
            size=int(size) if size.isdigit() else None,
            priority=spec.priority,
            old=old,
            uploaded=uploaded,
        )
        if record is not None:
            yield record


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
        document = json.loads(ctx.fetch_text(url, name="scrape.json", max_age_days=LISTING_MAX_AGE))
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
    placed = {"hash": 0, "series": 0}
    old = OldNames.load(ctx)

    def run(label: str, records: Iterator[DiskRecord]) -> Iterator[DiskRecord]:
        count = 0
        try:
            for record in records:
                count += 1
                if record.attach_only:
                    placed["series"] += 1
                elif record.locations[0].hash_value:
                    placed["hash"] += 1
                yield record
        except _UNAVAILABLE as error:
            ctx.log(f"{INFO.id}: {label} unavailable, skipped: {error}")
        counts[label] = count
        ctx.log(f"{INFO.id}: {label}: {count} locations")

    for spec in ARCHIVE_SETS:
        yield from run(f"{spec.item}/{spec.file}", archive_records(ctx, spec, old))
    for spec in ITEM_SETS:
        yield from run(spec.item, item_records(ctx, spec, old))
    for discovery in DISCOVERIES:
        try:
            items = discover(ctx, discovery)
        except _UNAVAILABLE as error:
            ctx.log(f"{INFO.id}: search {discovery.query!r} failed, skipped: {error}")
            continue
        ctx.log(f"{INFO.id}: search {discovery.query!r} found {len(items)} items")
        for identifier, _title in items:
            spec = ItemSet(identifier, discovery.platform, discovery.kind, discovery.priority)
            yield from run(identifier, item_records(ctx, spec, old))
    ctx.log(
        f"{INFO.id}: {sum(counts.values())} locations from {len(counts)} sets; "
        f"{placed['hash']} with a hash from an old TOSEC DAT, "
        f"{placed['series']} recognised by a series rule"
    )

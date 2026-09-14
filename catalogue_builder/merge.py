"""Join the records of every source per disk and write the catalogue.

Rules, in the order they are applied:

1. Records with a key (series, number, part, version) describe the same disk
   whichever source they come from, and merge.
2. Records without a key merge into a disk that already owns one of their
   images, compared by MD5, SHA-1, SHA-512 or CRC32 with size. Otherwise they
   become a disk of their own, labelled from their title.
3. Records that carry only locations (no key, no images) attach each location
   to the disk owning an image with its hash, or, when it carries no hash,
   of the same name (compared without case, with or without the extension).
   Such a location never creates a disk: one that matches nothing is counted
   and logged. Only a record with a title whose locations name no image at
   all (an archive of a whole disk, known by its title) becomes a disk of
   its own. A keyed record marked ``attach_only`` joins the disk with its
   key when another record made one, and is otherwise dropped the same way.
4. Records that carry only pictures or notes (no key, images, contents or
   locations) never create a disk. A picture attaches to the disk owning an
   image of the file name it gives, or to every title on its platform whose
   normalised title equals its title key. A note attaches to every title
   with its title.

The disk shows the contents of the best source that lists any (the lowest
``CONTENT_PRIORITY``), while the search index holds the titles from every
source. Other single values come from the first source, by priority, that
gives one; credits, notes and menu texts from all sources are kept.

Each disk also gets its category and crew from ``piratefinder.archive_layout``
(so the Find filters and the download folders agree), its most precise
release date and a sort title. Each shown title, and each disk that lists no
titles, gets a row in ``entries`` and ``entry_fts``. A disk points at the
history of its crew (``disks.crew_id``) only through crew records of its own
platform; see ``_Crews``. docs/CATALOGUE.md describes every rule.
"""

from __future__ import annotations

import json
import re
import sqlite3
import urllib.parse
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field

from piratefinder.archive_layout import UNKNOWN_CREW, archive_crew, archive_type, platform_folder
from piratefinder.catalogue import schema
from piratefinder.catalogue.naming import (
    display_title,
    image_rank_key,
    normalise,
    search_text,
    sort_key,
    sort_title,
    tidy_label,
    title_key,
)
from piratefinder.models import Content, ContentKind, Disk, DiskKind, Platform

from .records import (
    HOSTS,
    ContentRecord,
    CrewRecord,
    DiskRecord,
    ImageRecordIn,
    LocationRecord,
    MediaRecordIn,
    SourceInfo,
    TriviaRecordIn,
    base_name,
    image_name_keys,
    normalise_part,
    normalise_version,
)
from .series import CrewChoice, GroupRegistry, SeriesDef, SeriesRegistry, crew_key

DEFAULT_PRIORITY = 90
CATALOGUE_LICENCE = "CC BY-NC-SA 4.0"
WIKIPEDIA = "wikipedia"  # trivia kind whose text is an English Wikipedia article title
WIKIPEDIA_ARTICLE = "https://en.wikipedia.org/wiki/{title}"
# Sources that pictures and notes may be credited to without being an
# importer. Each gets a sources row when some row names it. The catalogue
# holds Wikipedia article titles only; the text is fetched by the app.
CREDITED_SOURCES = {
    WIKIPEDIA: SourceInfo(WIKIPEDIA, "Wikipedia", "https://en.wikipedia.org/", "CC BY-SA 4.0"),
}
FILES_PER_DISK = 40  # image file names indexed per disk

# Words added to the facets column so free text finds a machine or a kind of
# release: "commodore", "menu disk", "compact". They describe machines and
# release types, never a crew. The platform's folder name ("Amiga", "Atari
# ST") is always indexed as well.
PLATFORM_WORDS = {"amiga": "Commodore"}
KIND_WORDS = {
    "menu": "menu disk compact",
    "pack": "pack",
    "single": "single",
    "compilation": "compilation",
}

# A beginning of location and media addresses ending in "/" gets an
# address_prefix row when at least this many addresses share it.
SHARED_PREFIX = 50

# Another source's title is taken for a shown title when most of the words
# of the shorter one are in the other, and they share enough words overall.
SIMILAR_CONTAINMENT = 0.75
SIMILAR_OVERLAP = 0.4

_DATE_TEXT = re.compile(r"^\s*(\d{4})(?:-(\d{1,2}|[xX?]{2})(?:-(\d{1,2}|[xX?]{2}))?)?(?!\d)")
_AKA = re.compile(r"(?:^|\s)aka\s+(?P<names>.+)$", re.IGNORECASE)
_ROMAN = {"ii": "2", "iii": "3", "iv": "4", "vi": "6", "vii": "7", "viii": "8", "ix": "9"}
_MINOR_WORDS = frozenset({"the", "a", "an", "and", "of"})


@dataclass(slots=True)
class SourceBatch:
    """Everything one source produced, with what the merge needs to know about it."""

    info: SourceInfo
    records: list[DiskRecord]
    priority: int = DEFAULT_PRIORITY
    status: str = "ok"
    retrieved: str = ""
    crews: list[CrewRecord] = field(default_factory=list)


@dataclass(slots=True)
class _Image:
    record: ImageRecordIn
    source: str
    order: tuple[int, int]
    id: int = 0


@dataclass(slots=True)
class _Disk:
    platform: str
    kind: str
    series_key: str = ""
    number: int | None = None
    part: str = ""
    version: str = ""
    records: list[tuple[int, int, DiskRecord]] = field(default_factory=list)
    images: list[_Image] = field(default_factory=list)
    locations: list[tuple[LocationRecord, _Image | None]] = field(default_factory=list)
    media: list[tuple[MediaRecordIn, str]] = field(default_factory=list)  # attached by image
    id: int = 0


@dataclass(slots=True)
class MergeResult:
    disks: list[_Disk]
    series: dict[str, SeriesDef]
    batches: list[SourceBatch]
    unmatched_locations: int = 0
    hash_merges: int = 0
    url_merges: int = 0
    url_refusals: int = 0
    # Pictures and notes of media-only records that attach to titles by name,
    # resolved when content ids exist: (platform, record, source id).
    title_media: list[tuple[str, MediaRecordIn, str]] = field(default_factory=list)
    title_trivia: list[tuple[str, TriviaRecordIn, str]] = field(default_factory=list)
    unmatched_media: int = 0
    unmatched_trivia: int = 0


class AddressPrefixes:
    """The address_prefix table: beginnings that many addresses share.

    Each address is stored as the longest beginning of it that ends in "/"
    and that at least ``SHARED_PREFIX`` of the catalogue's addresses share
    (its prefix, stored once), and the rest. Addresses no beginning is
    shared enough for have the empty prefix, id 0.
    """

    def __init__(self, addresses: Iterable[str]) -> None:
        self.counts: Counter[str] = Counter()
        for address in set(addresses):
            for index, char in enumerate(address):
                if char == "/":
                    self.counts[address[: index + 1]] += 1
        self.ids: dict[str, int] = {"": 0}

    def prefix(self, address: str) -> str:
        for index in range(len(address) - 1, -1, -1):
            if address[index] == "/" and self.counts[address[: index + 1]] >= SHARED_PREFIX:
                return address[: index + 1]
        return ""

    def split(self, address: str) -> tuple[int, str]:
        """(prefix id, rest) of an address."""
        prefix = self.prefix(address)
        return self.ids.setdefault(prefix, len(self.ids)), address[len(prefix) :]

    def write(self, connection: sqlite3.Connection) -> None:
        connection.executemany(
            "INSERT INTO address_prefix(id, text) VALUES (?, ?)",
            [(number, text) for text, number in self.ids.items()],
        )


def _addresses(result: MergeResult) -> Iterator[str]:
    """Every location and picture address the catalogue will hold."""
    items: list[MediaRecordIn] = [item for _platform, item, _source in result.title_media]
    for disk in result.disks:
        for location, _image in disk.locations:
            yield location.url
            yield location.page_url
        items.extend(item for _priority, _sequence, record in disk.records for item in record.media)
        items.extend(item for item, _source in disk.media)
    for item in items:
        yield item.url
        yield item.thumb_url
        yield item.page_url


def credit_template(credit: str, page_url: str) -> str:
    """A credit line as media_credit stores it: braces doubled, and the page
    address without its scheme, when the credit names it, as "{page}"."""
    template = credit.replace("{", "{{").replace("}", "}}")
    bare = page_url.split("://", 1)[-1] if page_url else ""
    if bare:
        template = template.replace(bare.replace("{", "{{").replace("}", "}}"), "{page}")
    return template


def distinct_words(text: str) -> str:
    """The words of a search text, each once, in their first order.

    Notes and scroll texts repeat their words a great deal; a word matches
    a row whether it is indexed once or ten times, so it is indexed once.
    """
    return " ".join(dict.fromkeys(text.split()))


def parse_date(text: str) -> tuple[int | None, int | None, int | None]:
    """(year, month, day) from "1989", "1989-06" or "1989-06-17".

    Unknown parts are None: "1989-xx-xx" gives only the year, and "19xx" or
    "198x" give nothing.
    """
    found = _DATE_TEXT.match(text or "")
    if found is None:
        return None, None, None
    year = int(found.group(1))
    if not 1950 <= year <= 2100:
        return None, None, None
    month = _date_number(found.group(2), 12)
    day = _date_number(found.group(3), 31) if month is not None else None
    return year, month, day


def _date_number(text: str | None, highest: int) -> int | None:
    if not text or not text.isdigit():
        return None
    value = int(text)
    return value if 1 <= value <= highest else None


def release_date(records: Iterable[DiskRecord]) -> tuple[int | None, int | None, int | None]:
    """The most precise date the records give.

    ``release_date`` of any record wins over date texts; among those the
    most precise wins, and on a tie the record first by priority. Without a
    release date, the date texts of every record are compared the same way.
    """
    records = list(records)
    for name in ("release_date", "date"):
        best: tuple[int | None, int | None, int | None] = (None, None, None)
        best_precision = 0
        for record in records:
            parsed = parse_date(getattr(record, name))
            precision = sum(value is not None for value in parsed)
            if precision > best_precision:
                best, best_precision = parsed, precision
        if best_precision:
            return best
    return None, None, None


def canonical_key(record: DiskRecord) -> tuple[str, int, str, str] | None:
    """The merge key, with "v2.0" and "v2" spelled the same."""
    if not record.series_key or record.number is None:
        return None
    version = re.sub(r"(?:\.0+)+$", "", normalise_version(record.version))
    return (record.series_key, record.number, normalise_part(record.part), version)


def _hash_keys(image: ImageRecordIn) -> list[tuple]:
    keys: list[tuple] = []
    if image.md5:
        keys.append(("md5", image.md5.lower()))
    if image.sha1:
        keys.append(("sha1", image.sha1.lower()))
    if image.sha512:
        keys.append(("sha512", image.sha512.lower()))
    if image.crc32 and image.size is not None:
        keys.append(("crc", image.crc32.lower(), image.size))
    return keys


class _Merger:
    def __init__(self, registry: SeriesRegistry, log: Callable[[str], None]) -> None:
        self.registry = registry
        self.log = log
        self.keyed: dict[tuple, _Disk] = {}
        self.disks: list[_Disk] = []
        self.by_hash: dict[tuple, tuple[_Disk, _Image]] = {}
        self.shared_hashes: set[tuple] = set()
        self.hash_merges = 0
        # Download address -> the disk it belongs to (see disk_for_url).
        self.by_url: dict[str, _Disk] = {}
        self.url_merges = 0
        self.url_refusals = 0
        self.unmatched_locations = 0
        self.unmatched_media = 0
        self.unmatched_trivia = 0
        self.title_media: list[tuple[str, MediaRecordIn, str]] = []
        self.title_trivia: list[tuple[str, TriviaRecordIn, str]] = []
        self.unknown_series: Counter[str] = Counter()

    # -- images ------------------------------------------------------------

    def add_image(self, disk: _Disk, image: ImageRecordIn, source: str, order: tuple) -> _Image:
        keys = _hash_keys(image)
        for key in keys:
            owner = self.by_hash.get(key)
            if owner is not None and owner[0] is disk:
                existing = owner[1]
                _fill_image(existing.record, image)
                self._register(disk, existing)
                return existing
        if not keys:
            names = set(image_name_keys(image.name))
            for existing in disk.images:
                if names & set(image_name_keys(existing.record.name)):
                    return existing
        added = _Image(ImageRecordIn(**_image_fields(image)), source, order)
        disk.images.append(added)
        self._register(disk, added)
        return added

    def _register(self, disk: _Disk, image: _Image) -> None:
        for key in _hash_keys(image.record):
            owner = self.by_hash.get(key)
            if owner is None:
                self.by_hash[key] = (disk, image)
            elif owner[0] is not disk:
                self.shared_hashes.add(key)

    def disk_for_images(self, record: DiskRecord) -> _Disk | None:
        for image in record.images:
            for key in _hash_keys(image):
                if key in self.shared_hashes:
                    continue
                owner = self.by_hash.get(key)
                if owner is not None and owner[0].platform == record.platform:
                    return owner[0]
        return None

    def image_index(self) -> tuple[dict, dict]:
        """Every image by file name keys and by hash value."""
        by_name: dict[str, list[tuple[_Disk, _Image]]] = defaultdict(list)
        by_value: dict[str, list[tuple[_Disk, _Image]]] = defaultdict(list)
        for disk in self.disks:
            for image in disk.images:
                for key in image_name_keys(image.record.name):
                    by_name[key].append((disk, image))
                for value in (image.record.md5, image.record.sha1, image.record.sha512):
                    if value:
                        by_value[value.lower()].append((disk, image))
                if image.record.crc32:
                    by_value[f"crc:{image.record.crc32.lower()}"].append((disk, image))
        return by_name, by_value

    # -- records -----------------------------------------------------------

    def add_record(self, disk: _Disk, record: DiskRecord, priority: int, sequence: int) -> None:
        disk.records.append((priority, sequence, record))
        for position, image in enumerate(record.images):
            self.add_image(disk, image, record.source, (priority, sequence, position))
        for location in record.locations:
            disk.locations.append((location, _image_for_location(disk, location)))
            if not _names_an_image(location):
                self.by_url.setdefault(_url_key(location.url), disk)

    def disk_for_url(self, record: DiskRecord) -> _Disk | None:
        """The disk that already has one of ``record``'s downloads, or None.

        Demozoo names the file of many packs on the amigascne archive, and so
        does the amigascne importer, but only one of them can key the disk.
        A record whose download is another disk's is that disk, unless the
        numbers in their titles disagree (Demozoo links a few packs to the
        wrong issue).
        """
        for location in record.locations:
            if _names_an_image(location):
                continue
            disk = self.by_url.get(_url_key(location.url))
            if disk is None or disk.platform != record.platform:
                continue
            if _numbers_disagree(disk, record):
                self.url_refusals += 1
                continue
            self.url_merges += 1
            return disk
        return None

    def new_disk(self, record: DiskRecord, key: tuple | None) -> _Disk:
        kind = record.kind
        if key is not None:
            series = self.series_for(record)
            kind = series.kind or kind
        disk = _Disk(platform=record.platform, kind=kind)
        if key is not None:
            disk.series_key, disk.number, disk.part, disk.version = key
        else:
            disk.part = record.part
            disk.version = record.version
        self.disks.append(disk)
        return disk

    def series_for(self, record: DiskRecord) -> SeriesDef:
        series = self.registry.get(record.series_key)
        if series is None:
            self.unknown_series[f"{record.source}:{record.series_key}"] += 1
            series = self.registry.add(
                SeriesDef(
                    id=record.series_key,
                    name=record.series_key.replace("-", " ").title(),
                    platform=record.platform,
                    kind=record.kind,
                )
            )
        return series

    def merge(self, batches: list[SourceBatch]) -> None:
        ordered = sorted(enumerate(batches), key=lambda item: (item[1].priority, item[0]))
        sequence = 0
        keyed: list[tuple[int, int, DiskRecord, tuple]] = []
        with_images: list[tuple[int, int, DiskRecord]] = []
        plain: list[tuple[int, int, DiskRecord]] = []
        location_only: list[tuple[int, int, DiskRecord]] = []
        media_only: list[tuple[int, int, DiskRecord]] = []
        attach_only: list[tuple[int, int, DiskRecord, tuple]] = []
        for _index, batch in ordered:
            for record in batch.records:
                sequence += 1
                key = canonical_key(record)
                if key is not None and record.attach_only:
                    attach_only.append((batch.priority, sequence, record, key))
                elif key is not None:
                    keyed.append((batch.priority, sequence, record, key))
                elif record.images:
                    with_images.append((batch.priority, sequence, record))
                elif record.locations and not record.contents:
                    location_only.append((batch.priority, sequence, record))
                elif not record.contents and (record.media or record.trivia):
                    media_only.append((batch.priority, sequence, record))
                else:
                    plain.append((batch.priority, sequence, record))

        for priority, number, record, key in keyed:
            disk = self.keyed.get(key)
            if disk is None:
                disk = self.keyed[key] = self.new_disk(record, key)
            self.add_record(disk, record, priority, number)
        for priority, number, record in with_images:
            disk = self.disk_for_images(record)
            if disk is None:
                disk = self.disk_for_url(record) or self.new_disk(record, None)
            else:
                self.hash_merges += 1
            self.add_record(disk, record, priority, number)
        for priority, number, record in plain:
            disk = self.disk_for_url(record) or self.new_disk(record, None)
            self.add_record(disk, record, priority, number)
        self.attach_keyed(attach_only)
        self.attach_locations(location_only)
        self.attach_media(media_only)
        for name, count in sorted(self.unknown_series.items()):
            self.log(
                f"merge: {count} records name series {name.split(':', 1)[1]!r} "
                f"that is not declared (from {name.split(':', 1)[0]})"
            )

    def attach_keyed(self, records: list[tuple[int, int, DiskRecord, tuple]]) -> None:
        """Add attach-only keyed records to the disks other records made."""
        dropped = 0
        for priority, number, record, key in records:
            disk = self.keyed.get(key)
            if disk is None:
                dropped += 1
                self.unmatched_locations += len(record.locations)
                continue
            self.add_record(disk, record, priority, number)
        if dropped:
            self.log(f"merge: {dropped} attach-only records name a disc no other source has")

    def attach_locations(self, records: list[tuple[int, int, DiskRecord]]) -> None:
        """Attach location-only records to the disks that own their images.

        A location that names an image (by file name or hash) points at a
        catalogue image: it goes to the disk that owns that image, or is
        counted as unmatched. A record with a title whose locations name no
        image at all (an archive of a whole disk) describes a disk of its
        own and becomes one.
        """
        if not records:
            return
        by_name, by_value = self.image_index()
        for priority, sequence, record in records:
            if record.title.strip() and not any(_names_an_image(loc) for loc in record.locations):
                disk = self.disk_for_url(record) or self.new_disk(record, None)
                self.add_record(disk, record, priority, sequence)
                continue
            owners = [_locate(location, by_name, by_value) for location in record.locations]
            for location, owner in zip(record.locations, owners, strict=True):
                if owner is None:
                    self.unmatched_locations += 1
                    continue
                owner[0].locations.append((location, owner[1]))
        if self.unmatched_locations:
            self.log(f"merge: {self.unmatched_locations} locations match no catalogue disc")

    def attach_media(self, records: list[tuple[int, int, DiskRecord]]) -> None:
        """Place the pictures and notes of media-only records.

        A picture that names an image file goes to the disk owning an image
        of that name now; one with a title key, and every note, waits until
        the titles have ids (``write_catalogue``).
        """
        if not records:
            return
        by_name, _by_value = self.image_index()
        for _priority, _sequence, record in records:
            for item in record.media:
                source = item.source or record.source
                if item.image_name:
                    owner = next(
                        (
                            by_name[key][0]
                            for key in image_name_keys(item.image_name)
                            if by_name.get(key)
                        ),
                        None,
                    )
                    if owner is None:
                        self.unmatched_media += 1
                    else:
                        owner[0].media.append((item, source))
                elif item.title_key:
                    self.title_media.append((record.platform, item, source))
                else:
                    self.unmatched_media += 1
            for note in record.trivia:
                if note.content_title:
                    self.title_trivia.append((record.platform, note, note.source or record.source))
                else:
                    self.unmatched_trivia += 1


def _url_key(url: str) -> str:
    """A download address for comparison: unquoted, without case."""
    return urllib.parse.unquote(url).strip().casefold()


_LAST_NUMBER = re.compile(r"(\d+)(?!.*\d)")


def _record_number(record: DiskRecord) -> int | None:
    if record.number is not None:
        return record.number
    found = _LAST_NUMBER.search(record.title)
    return int(found.group(1)) if found else None


def _numbers_disagree(disk: _Disk, record: DiskRecord) -> bool:
    """Whether a disk and a record carry different disk numbers."""
    theirs = _record_number(record)
    if theirs is None:
        return False
    ours = disk.number
    if ours is None:
        ours = next(
            (n for _p, _s, other in disk.records if (n := _record_number(other)) is not None),
            None,
        )
    return ours is not None and ours != theirs


def _names_an_image(location: LocationRecord) -> bool:
    return bool(location.image_name or location.member or location.hash_value)


def _locate(
    location: LocationRecord,
    by_name: dict[str, list[tuple[_Disk, _Image]]],
    by_value: dict[str, list[tuple[_Disk, _Image]]],
) -> tuple[_Disk, _Image] | None:
    """The disk and image a location-only location belongs to, or None.

    A location with a hash goes only to an image with that hash: its name may
    since have passed to another dump, which the download would not match.
    """
    value = location.hash_value.lower()
    if value:
        if location.hash_kind.lower() in ("crc32", "crc"):
            candidates = by_value.get(f"crc:{value}", [])
            if location.size is not None:
                candidates = [c for c in candidates if c[1].record.size == location.size]
        else:
            candidates = by_value.get(value, [])
        return candidates[0] if candidates else None
    for key in image_name_keys(location.image_name or location.member):
        if key and by_name.get(key):
            return by_name[key][0]
    return None


def _image_for_location(disk: _Disk, location: LocationRecord) -> _Image | None:
    value = location.hash_value.lower()
    names = set(image_name_keys(location.image_name)) if location.image_name else set()
    for image in disk.images:
        record = image.record
        if value and value in (record.md5, record.sha1, record.sha512, record.crc32):
            return image
        if names and names & set(image_name_keys(record.name)):
            return image
    return None


def _image_hash(image: _Image, kind: str) -> str:
    """The image's hash of a location's hash kind ("crc32", "md5", ...), or ""."""
    field = {"crc": "crc32"}.get(kind.lower(), kind.lower())
    value = getattr(image.record, field, "") if field in ("crc32", "md5", "sha1", "sha512") else ""
    return value.lower()


def _image_fields(image: ImageRecordIn) -> dict:
    return {name: getattr(image, name) for name in ImageRecordIn.__slots__}


def _fill_image(target: ImageRecordIn, other: ImageRecordIn) -> None:
    for name in ("crc32", "md5", "sha1", "sha512", "flags", "format", "virus", "antivirus"):
        if not getattr(target, name) and getattr(other, name):
            setattr(target, name, getattr(other, name))
    if target.size is None and other.size is not None:
        target.size = other.size
    target.virus_damage = target.virus_damage or other.virus_damage


def merge_records(
    batches: list[SourceBatch],
    registry: SeriesRegistry,
    log: Callable[[str], None] = lambda message: None,
) -> MergeResult:
    """Join the records of every batch into disks."""
    merger = _Merger(registry, log)
    merger.merge(batches)
    series = {disk.series_key: registry.get(disk.series_key) for disk in merger.disks}
    series = {key: value for key, value in series.items() if key and value is not None}
    return MergeResult(
        disks=merger.disks,
        series=series,
        batches=batches,
        unmatched_locations=merger.unmatched_locations,
        hash_merges=merger.hash_merges,
        url_merges=merger.url_merges,
        url_refusals=merger.url_refusals,
        title_media=merger.title_media,
        title_trivia=merger.title_trivia,
        unmatched_media=merger.unmatched_media,
        unmatched_trivia=merger.unmatched_trivia,
    )


# -- titles ----------------------------------------------------------------------


def _title_keys(title: str) -> list[str]:
    """The spellings a title is looked up by: its ``title_key`` (no brackets
    or version, article in front), and the whole title with its article in
    front and as written."""
    keys = [title_key(title), normalise(display_title(title)), normalise(title)]
    return [key for index, key in enumerate(keys) if key and key not in keys[:index]]


def _title_words(title: str) -> frozenset[str]:
    words = [_ROMAN.get(word, word) for word in normalise(display_title(title)).split()]
    significant = frozenset(word for word in words if word not in _MINOR_WORDS)
    return significant or frozenset(words)


def akas(extra: str) -> list[str]:
    """Other titles named in a content note: "[doc] aka Foo; Bar" gives Foo and Bar."""
    found = _AKA.search(extra or "")
    if found is None:
        return []
    return [name.strip() for name in found.group("names").split(";") if name.strip()]


class _TitleMatcher:
    """Finds the shown title of a disk that another source's title stands for.

    The same title, compared normalised with or without a trailing article,
    matches first. Otherwise the shown title sharing the most words wins,
    when most words of the shorter title are in the longer one, so "Xenon II"
    finds "Xenon 2 - Megablast".
    """

    def __init__(self, titles: Iterable[tuple[int, str]]) -> None:
        self._exact: dict[str, int] = {}
        self._words: list[tuple[int, frozenset[str]]] = []
        for content_id, title in titles:
            for key in _title_keys(title):
                self._exact.setdefault(key, content_id)
            self._words.append((content_id, _title_words(title)))

    def find(self, title: str) -> int | None:
        for key in _title_keys(title):
            if key in self._exact:
                return self._exact[key]
        words = _title_words(title)
        best: int | None = None
        best_score = (0.0, 0.0)
        for content_id, other in self._words:
            common = len(words & other)
            if not common:
                continue
            score = (common / min(len(words), len(other)), common / len(words | other))
            if score > best_score:
                best, best_score = content_id, score
        if best_score[0] >= SIMILAR_CONTAINMENT and best_score[1] >= SIMILAR_OVERLAP:
            return best
        return None


# -- writing -------------------------------------------------------------------


def _first(records: list[DiskRecord], name: str) -> str:
    return next((getattr(r, name) for r in records if getattr(r, name)), "")


def _joined(records: list[DiskRecord], name: str) -> str:
    values: dict[str, None] = {}
    for record in records:
        text = getattr(record, name).strip()
        if text:
            values.setdefault(text, None)
    return "\n".join(values)


def _label(disk: _Disk, series: SeriesDef | None, title: str) -> str:
    if series is not None and disk.number is not None:
        return series.format_label(disk.number, disk.part, disk.version)
    label = tidy_label(title) if title else ""
    if disk.part and disk.part.lower() not in label.lower():
        label = f"{label} ({disk.part})" if label else disk.part
    return label or "Untitled disk"


def _display_contents(
    records: list[tuple[int, int, DiskRecord]],
) -> list[tuple[ContentRecord, str]]:
    """The contents of the best source that lists any, each with its source id."""
    listing = [(p, s, r) for p, s, r in records if r.contents]
    if not listing:
        return []
    best = min(listing, key=lambda item: (item[0], item[1]))[2].source
    chosen: dict[tuple[str, str, str], tuple[ContentRecord, str]] = {}
    for _priority, _sequence, record in listing:
        if record.source == best:
            for content in record.contents:
                identity = (normalise(content.title) or content.title, content.kind, content.extra)
                chosen.setdefault(identity, (content, best))
    return list(chosen.values())


def _disk_order(disk: _Disk, series: SeriesDef | None, label: str) -> tuple:
    """Disc order, which is also the order of disk ids.

    Numbered disks come first by series name, number, part and version;
    then every other disk by its sort title. Sorting by disk id therefore
    sorts by disc, and the Find screen needs no sort for that order.
    """
    if series is not None and disk.number is not None:
        return (
            0,
            sort_title(series.name),
            series.id,
            disk.number,
            sort_key(disk.part),
            sort_key(disk.version),
            "",
            disk.platform,
            sort_key(label),
        )
    return (1, "", "", -1, "", "", sort_title(label), disk.platform, sort_key(label))


def _enum(kind: type, value: str, default):
    try:
        return kind(value)
    except ValueError:
        return default


def _file_names(disk: _Disk) -> str:
    """Search text for the image file names of a disk: each name without its
    folder, as words, with its extension."""
    names: dict[str, None] = {}
    for image in disk.images:
        names.setdefault(base_name(image.record.name), None)
    for location, _image in disk.locations:
        for name in (location.image_name, location.member):
            if name:
                names.setdefault(base_name(name), None)
    words: list[str] = []
    for name in list(names)[:FILES_PER_DISK]:
        stem, dot, extension = name.rpartition(".")
        words.append(f"{search_text(stem)} {extension}" if dot and stem else search_text(name))
    return " ".join(dict.fromkeys(word.strip() for word in words if word.strip()))


def _article_url(title: str) -> str:
    return WIKIPEDIA_ARTICLE.format(title=urllib.parse.quote(title.strip().replace(" ", "_")))


@dataclass(slots=True)
class _DiskText:
    """The search text every entry of one disk shares."""

    disk: str
    crew: list[str]
    people: str
    facets: list[str]
    files: str
    notes: str  # notes and menu texts, as search text


class _Writer:
    """Writes the merged disks, their entries, media and trivia, and the crews."""

    def __init__(
        self,
        connection: sqlite3.Connection,
        result: MergeResult,
        groups: GroupRegistry,
        log: Callable[[str], None],
        crew_choice: CrewChoice,
    ) -> None:
        self.connection = connection
        self.result = result
        self.groups = groups
        self.log = log
        self.stats: Counter[str] = Counter()
        self.image_id = 0
        self.content_id = 0
        self.entry_id = 0
        self.media_seen: set[tuple[int, int | None, str]] = set()
        self.prefixes = AddressPrefixes(_addresses(result))
        self.credits: dict[tuple[str, str], int] = {}  # (source, credit template) -> id
        self.trivia_seen: set[tuple[int, int | None, str, str]] = set()
        # Trivia rows wait until every disk is written, so Wikipedia articles
        # can be kept once per disk: (disk, title, kind, text, source, url, licence).
        self.trivia_rows: list[list] = []
        self.content_kinds: dict[int, str] = {}
        self.disk_notes: dict[int, set[str]] = {}
        # (platform, normalised title) -> (disk id, content id, only title of a single disk)
        self.titles: dict[tuple[str, str], list[tuple[int, int, bool]]] = defaultdict(list)
        self.crews = _Crews(result.batches, groups, crew_choice)
        self.media_sources: Counter[str] = Counter()
        # (platform, crew as the disk gives it) -> the spelling written; see settle_crews.
        self.crew_names: dict[tuple[str, str], str] = {}

    # -- crews -----------------------------------------------------------------

    def disk_crew(
        self, disk: _Disk, records: list[DiskRecord], definition: SeriesDef | None
    ) -> str:
        """The crew of a disk as its records give it, a tag expanded through groups.toml."""
        model = Disk(
            id=disk.id,
            label="",
            platform=_enum(Platform, disk.platform, Platform.ATARI_ST),
            kind=_enum(DiskKind, disk.kind, DiskKind.MENU),
            series_id=disk.series_key or None,
            series_name=definition.name if definition else "",
            publisher=_first(records, "publisher"),
            cracker=_first(records, "cracker"),
        )
        crew = archive_crew(model, definition.group if definition else "")
        return crew if crew == UNKNOWN_CREW else self.groups.expand(crew, disk.platform)

    def settle_crews(self, crews: Iterable[tuple[str, str]]) -> None:
        """One spelling for each crew on each platform.

        Spellings that differ only in case, "The", spaces or punctuation
        ("Flash Light Design" and "Flashlight Design", "The Replicants" and
        "Replicants") are one crew: the most common spelling is written for
        all of them, so the Crew filter and the download folders show one.
        """
        counts = Counter(crews)
        spellings: dict[tuple[str, str], Counter[str]] = defaultdict(Counter)
        for (platform, crew), count in counts.items():
            spellings[(platform, spelling_key(crew))][crew] += count
        joined = 0
        for (platform, _key), found in spellings.items():
            if len(found) < 2:
                continue
            chosen = min(found, key=lambda crew: (-found[crew], crew))
            for crew in found:
                if crew != chosen:
                    self.crew_names[(platform, crew)] = chosen
                    joined += 1
        self.stats["crew spellings joined"] = joined

    # -- one disk --------------------------------------------------------------

    def write_disk(
        self,
        disk: _Disk,
        records: list[DiskRecord],
        title: str,
        label: str,
        definition: SeriesDef | None,
    ) -> None:
        contents = _display_contents(disk.records)
        model = Disk(
            id=disk.id,
            label=label,
            platform=_enum(Platform, disk.platform, Platform.ATARI_ST),
            kind=_enum(DiskKind, disk.kind, DiskKind.MENU),
            series_id=disk.series_key or None,
            series_name=definition.name if definition else "",
            number=disk.number,
            publisher=_first(records, "publisher"),
            cracker=_first(records, "cracker"),
        )
        category = archive_type(
            model,
            [
                Content(disk.id, content.title, _enum(ContentKind, content.kind, ContentKind.OTHER))
                for content, _source in contents
            ],
        )
        crew = self.disk_crew(disk, records, definition)
        crew = self.crew_names.get((disk.platform, crew), crew)
        credited = {(record.source, crew_id) for record in records for crew_id in record.crew_ids}
        crew_id = self.crews.row_for(crew, disk.platform, credited)
        year, month, day = release_date(records)
        self.connection.execute(
            "INSERT INTO disks(id, series_id, number, part, version, label, title, date, "
            "platform, kind, publisher, cracker, condition, notes, credits, menu_text, "
            "category, crew, crew_id, year, month, day, sort_title) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                disk.id,
                disk.series_key or None,
                disk.number,
                disk.part,
                disk.version,
                label,
                title,
                _first(records, "date"),
                disk.platform,
                disk.kind,
                model.publisher,
                model.cracker,
                _first(records, "condition"),
                _joined(records, "notes"),
                _joined(records, "credits"),
                _joined(records, "menu_text"),
                category,
                crew,
                crew_id,
                year,
                month,
                day,
                sort_title(label),
            ),
        )
        shown: list[tuple[int, ContentRecord]] = []
        for content, _source in contents:
            self.content_id += 1
            shown.append((self.content_id, content))
        self.connection.executemany(
            "INSERT INTO contents(id, disk_id, position, title, kind, publisher, cracker, "
            "version, extra, source) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    content_id,
                    disk.id,
                    position,
                    content.title,
                    content.kind,
                    content.publisher,
                    content.cracker,
                    content.version,
                    content.extra,
                    source,
                )
                for position, ((content_id, content), (_content, source)) in enumerate(
                    zip(shown, contents, strict=True), start=1
                )
            ],
        )
        self.write_images(disk)
        locations = self.write_locations(disk)
        links = dict.fromkeys((label_, url) for record in records for label_, url in record.links)
        self.connection.executemany(
            "INSERT INTO links(disk_id, label, url) VALUES (?, ?, ?)",
            [(disk.id, link_label, url) for link_label, url in links],
        )
        matcher = _TitleMatcher((content_id, content.title) for content_id, content in shown)
        shown_source = contents[0][1] if contents else ""
        text = self.disk_text(disk, label, title, definition, records, category, crew, year, month)
        self.write_entries(disk, label, records, shown, shown_source, matcher, text)
        self.write_disk_index(disk, label, title, definition, records, text)
        self.write_media(disk, records, matcher)
        self.write_trivia(disk, records, matcher)
        single = disk.kind == DiskKind.SINGLE and len(shown) == 1
        notes = {normalise(record.notes) for record in records if record.notes.strip()}
        if notes:
            self.disk_notes[disk.id] = notes | {normalise(_joined(records, "notes"))}
        for content_id, content in shown:
            self.content_kinds[content_id] = content.kind
            for key in _title_keys(content.title):
                self.titles[(disk.platform, key)].append((disk.id, content_id, single))

        stats = self.stats
        stats["disks"] += 1
        stats[f"disks:{disk.platform}:{disk.kind}"] += 1
        stats["with contents"] += bool(contents)
        stats["with images"] += bool(disk.images)
        stats["with locations"] += bool(locations)
        stats["images"] += len(disk.images)
        stats["contents"] += len(contents)
        stats["locations"] += locations
        stats["with year"] += year is not None
        stats["with month"] += month is not None
        stats["with day"] += day is not None
        stats["with crew history"] += crew_id is not None

    def write_images(self, disk: _Disk) -> None:
        ranked = sorted(
            disk.images,
            key=lambda image: (
                max(image_rank_key(image.record.flags)[0], 2 if image.record.bad else 0),
                image_rank_key(image.record.flags)[1:],
                image.order,
            ),
        )
        for rank, image in enumerate(ranked):
            self.image_id += 1
            image.id = self.image_id
            item = image.record
            self.connection.execute(
                "INSERT INTO images(id, disk_id, name, format, flags, size, crc32, md5, sha1, "
                "sha512, bad, rank, source, virus, virus_damage, antivirus) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    image.id,
                    disk.id,
                    item.name,
                    item.format.lower(),
                    item.flags,
                    item.size,
                    item.crc32.lower(),
                    item.md5.lower(),
                    item.sha1.lower(),
                    item.sha512.lower(),
                    1 if item.bad else 0,
                    rank,
                    image.source,
                    item.virus,
                    1 if item.virus_damage else 0,
                    item.antivirus,
                ),
            )
            self.stats["images with virus"] += bool(item.virus)
            self.stats["images with virus damage"] += bool(item.virus_damage)
            self.stats["images with antivirus"] += bool(item.antivirus)

    def write_locations(self, disk: _Disk) -> int:
        """Write the disk's locations. A location keeps its own hash only when
        the image it is attached to lacks it: a download is otherwise checked
        against the image's hashes, which include it."""
        seen: set[tuple[str, str]] = set()
        # One row per file: the same address in other letter case is the same
        # download (Demozoo's links do not always keep the archive's case), and
        # the source that reads the archive itself, with the lower priority
        # number, has its spelling.
        ordered = sorted(disk.locations, key=lambda item: item[0].priority)
        for location, image in ordered:
            identity = (_url_key(location.url), location.member)
            if identity in seen:
                continue
            seen.add(identity)
            hash_kind, hash_value = location.hash_kind, location.hash_value.lower()
            if image is not None and hash_value and hash_value == _image_hash(image, hash_kind):
                hash_kind, hash_value = "", ""
            url_prefix, url_rest = self.prefixes.split(location.url)
            page_prefix, page_rest = (
                self.prefixes.split(location.page_url) if location.page_url else (None, "")
            )
            self.connection.execute(
                "INSERT INTO locations(disk_id, image_id, provider, url_prefix, url, container, "
                "member, size, hash_kind, hash_value, page_prefix, page_url, priority) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    disk.id,
                    image.id if image is not None else None,
                    location.provider,
                    url_prefix,
                    url_rest,
                    location.container,
                    location.member,
                    location.size,
                    hash_kind,
                    hash_value,
                    page_prefix,
                    page_rest,
                    location.priority,
                ),
            )
        return len(seen)

    # -- search text -------------------------------------------------------------

    def spell(self, found: dict[str, None], text: str, platform: str) -> None:
        """Add every spelling of the crews in ``text``: as written and expanded."""
        if text.strip():
            for spelling in self.groups.spellings(text, platform):
                found.setdefault(spelling, None)

    def disk_text(
        self,
        disk: _Disk,
        label: str,
        title: str,
        definition: SeriesDef | None,
        records: list[DiskRecord],
        category: str,
        crew: str,
        year: int | None,
        month: int | None,
    ) -> _DiskText:
        series_words: list[str] = []
        if definition is not None:
            series_words = [definition.name, definition.group, *definition.aliases]
        crews: dict[str, None] = {}
        for name in (crew, *(r.cracker for r in records), *(r.publisher for r in records)):
            self.spell(crews, name, disk.platform)
        facets = [
            platform_folder(disk.platform),
            PLATFORM_WORDS.get(disk.platform, ""),
            category,
            KIND_WORDS.get(disk.kind, disk.kind),
        ]
        if year is not None:
            facets.append(str(year))
            if month is not None:
                facets.append(f"{year} {month:02d}")
        notes = [text for record in records for text in (record.notes, record.menu_text) if text]
        return _DiskText(
            disk=search_text(" ".join([label, *series_words, title])),
            crew=list(crews),
            people=search_text(_joined(records, "credits")),
            facets=[word for word in facets if word],
            files=_file_names(disk),
            notes=distinct_words(search_text(" ".join(notes))),
        )

    def write_entries(
        self,
        disk: _Disk,
        label: str,
        records: list[DiskRecord],
        shown: list[tuple[int, ContentRecord]],
        shown_source: str,
        matcher: _TitleMatcher,
        text: _DiskText,
    ) -> None:
        """One entry per shown title, or one for the disk when it lists none.

        Titles that other sources give for the same disk are indexed with the
        shown title they stand for, with its crackers and publishers; titles
        that stand for none are indexed in the notes of every entry.
        """
        others: dict[int, dict[str, None]] = defaultdict(dict)
        other_crews: dict[int, dict[str, None]] = defaultdict(dict)
        loose: dict[str, None] = {}
        for record in records:
            if record.source == shown_source:
                continue
            for content in record.contents:
                content_id = matcher.find(content.title)
                if content_id is None:
                    loose.setdefault(content.title, None)
                    continue
                others[content_id].setdefault(content.title, None)
                for name in akas(content.extra):
                    others[content_id].setdefault(name, None)
                self.spell(other_crews[content_id], content.cracker, disk.platform)
                if content.publisher:
                    other_crews[content_id].setdefault(content.publisher, None)
        rows: list[tuple] = []
        index: list[tuple] = []
        loose_text = search_text(" ".join(loose))
        entries = shown or [(None, None)]
        for content_id, content in entries:
            self.entry_id += 1
            if content is None:
                shown_title, kind, extra = label, "", ""
                names: list[str] = [label]
                crews = list(text.crew)
                facets = text.facets
            else:
                shown_title, kind, extra = display_title(content.title), content.kind, content.extra
                names = [shown_title, *akas(extra), *others.get(content_id, {})]
                found = dict.fromkeys(text.crew)
                self.spell(found, content.cracker, disk.platform)
                if content.publisher:
                    found.setdefault(content.publisher, None)
                found.update(other_crews.get(content_id, {}))
                crews = list(found)
                facets = [*text.facets, kind]
            rows.append(
                (self.entry_id, disk.id, content_id, shown_title, sort_title(shown_title), kind)
            )
            index.append(
                (
                    self.entry_id,
                    search_text(" ".join(dict.fromkeys(names))),
                    text.disk,
                    search_text(" ".join(crews)),
                    text.people,
                    search_text(" ".join(facets)),
                    text.files,
                    " ".join(part for part in (text.notes, search_text(extra), loose_text) if part),
                )
            )
        self.connection.executemany(
            "INSERT INTO entries(id, disk_id, content_id, title, sort_title, kind) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            rows,
        )
        self.connection.executemany(
            "INSERT INTO entry_fts(rowid, title, disk, crew, people, facets, files, notes) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            index,
        )
        self.stats["entries"] += len(rows)

    def write_disk_index(
        self,
        disk: _Disk,
        label: str,
        title: str,
        definition: SeriesDef | None,
        records: list[DiskRecord],
        text: _DiskText,
    ) -> None:
        titles: dict[str, None] = {}
        extras: dict[str, None] = {}
        crews = dict.fromkeys(text.crew)
        kinds: dict[str, None] = {}
        for record in records:
            for content in record.contents:
                titles.setdefault(content.title, None)
                kinds.setdefault(content.kind, None)
                if content.extra:
                    extras.setdefault(content.extra, None)
                if content.publisher:
                    crews.setdefault(content.publisher, None)
                self.spell(crews, content.cracker, disk.platform)
        series_text = ""
        if definition is not None:
            series_text = " ".join([definition.name, definition.group, *definition.aliases])
        self.connection.execute(
            "INSERT INTO disk_fts(rowid, label, series, contents, people, notes, crew, facets, "
            "files) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                disk.id,
                search_text(f"{label} {title}"),
                search_text(series_text),
                search_text(" ".join([*titles, *extras])),
                text.people,
                text.notes,
                search_text(" ".join(crews)),
                search_text(" ".join([*text.facets, *kinds])),
                text.files,
            ),
        )
        self.connection.execute(
            "INSERT INTO disk_trigram(rowid, text) VALUES (?, ?)",
            (disk.id, normalise(" ".join([label, *titles]))),
        )

    # -- media and trivia ----------------------------------------------------------

    def add_media(
        self, disk_id: int, content_id: int | None, item: MediaRecordIn, source: str
    ) -> None:
        """Write a picture compactly (schema.py): its addresses as prefix and
        rest, and its source and credit line as one media_credit row."""
        identity = (disk_id, content_id, item.url)
        if not item.url or identity in self.media_seen:
            return
        self.media_seen.add(identity)
        url_prefix, url_rest = self.prefixes.split(item.url)
        thumb_prefix = thumb_rest = None
        if item.thumb_url:
            thumb_prefix, thumb_rest = self.prefixes.split(item.thumb_url)
            thumb_rest = None if thumb_rest == url_rest else thumb_rest
        page_prefix, page_rest = self.prefixes.split(item.page_url) if item.page_url else (None, "")
        credit = (source, credit_template(item.credit, item.page_url))
        self.connection.execute(
            "INSERT INTO media(disk_id, content_id, kind, url_prefix, url, thumb_prefix, "
            "thumb_url, width, height, credit_id, page_prefix, page_url, rank) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                disk_id,
                content_id,
                item.kind,
                url_prefix,
                url_rest,
                thumb_prefix,
                thumb_rest,
                item.width,
                item.height,
                self.credits.setdefault(credit, len(self.credits) + 1),
                page_prefix,
                page_rest,
                item.rank,
            ),
        )
        self.stats["media"] += 1
        self.media_sources[source] += 1

    def write_shared_text(self) -> None:
        """The address prefixes and media credits the rows point at."""
        self.prefixes.write(self.connection)
        self.connection.executemany(
            "INSERT INTO media_credit(id, source, credit) VALUES (?, ?, ?)",
            [(number, source, credit) for (source, credit), number in self.credits.items()],
        )

    def add_trivia(
        self,
        disk_id: int,
        content_id: int | None,
        kind: str,
        text: str,
        source: str,
        url: str = "",
        licence: str = "",
    ) -> None:
        identity = (disk_id, content_id, kind, text.strip())
        if not text.strip() or identity in self.trivia_seen:
            return
        self.trivia_seen.add(identity)
        self.trivia_rows.append([disk_id, content_id, kind, text.strip(), source, url, licence])

    def write_trivia_rows(self) -> None:
        """Write the trivia, each Wikipedia article once per disk and no note
        that repeats the disk's notes.

        An article that several titles of one disk share (a game and its
        documents) is kept for the first game among them, or else for the
        first of them.
        """
        kept: list[list] = []
        articles: dict[tuple[int, str], list] = {}
        for row in self.trivia_rows:
            disk_id, content_id, kind, text = row[:4]
            if kind == "note" and normalise(text) in self.disk_notes.get(disk_id, ()):
                self.stats["trivia repeating disk notes"] += 1
                continue
            if kind == WIKIPEDIA:
                first = articles.get((disk_id, normalise(text)))
                if first is not None:
                    if not self.is_game(first[1]) and self.is_game(content_id):
                        first[1] = content_id
                    self.stats["wikipedia repeats"] += 1
                    continue
                articles[(disk_id, normalise(text))] = row
            kept.append(row)
        self.connection.executemany(
            "INSERT INTO trivia(disk_id, content_id, kind, text, source, url, licence) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            kept,
        )
        self.stats["trivia"] += len(kept)
        for row in kept:
            self.media_sources[row[4]] += 1

    def is_game(self, content_id: int | None) -> bool:
        return content_id is not None and self.content_kinds.get(content_id) == ContentKind.GAME

    def write_media(self, disk: _Disk, records: list[DiskRecord], matcher: _TitleMatcher) -> None:
        placed = [
            (item, item.source or record.source) for record in records for item in record.media
        ]
        for item, source in [*placed, *disk.media]:
            content_id = None
            if item.content_title:
                content_id = matcher.find(item.content_title)
                if content_id is None:
                    self.stats["media unmatched"] += 1
                    continue
            self.add_media(disk.id, content_id, item, source)

    def write_trivia(self, disk: _Disk, records: list[DiskRecord], matcher: _TitleMatcher) -> None:
        for record in records:
            for note in record.trivia:
                content_id = None
                if note.content_title:
                    content_id = matcher.find(note.content_title)
                    if content_id is None:
                        self.stats["trivia unmatched"] += 1
                        continue
                self.add_trivia(
                    disk.id,
                    content_id,
                    note.kind,
                    note.text,
                    note.source or record.source,
                    note.url,
                    note.licence,
                )
        for record in records:
            for content in record.contents:
                articles = [value for kind, value in content.links if kind == WIKIPEDIA]
                if not any(article.strip() for article in articles):
                    continue
                content_id = matcher.find(content.title)
                if content_id is None:
                    continue
                for article in articles:
                    self.add_trivia(
                        disk.id,
                        content_id,
                        WIKIPEDIA,
                        article,
                        record.source,
                        _article_url(article),
                    )

    def write_title_media(self) -> None:
        """Pictures and notes of media-only records, placed by title."""
        for platform, item, source in self.result.title_media:
            targets = self.title_targets(platform, item.title_key)
            if not targets:
                self.stats["media unmatched"] += 1
                continue
            for disk_id, content_id, single in targets:
                self.add_media(disk_id, content_id, item, source)
                if single:
                    self.add_media(disk_id, None, item, source)
        for platform, note, source in self.result.title_trivia:
            targets = self.title_targets(platform, note.content_title)
            if not targets:
                self.stats["trivia unmatched"] += 1
                continue
            for disk_id, content_id, _single in targets:
                self.add_trivia(
                    disk_id, content_id, note.kind, note.text, source, note.url, note.licence
                )
        self.stats["media unmatched"] += self.result.unmatched_media
        self.stats["trivia unmatched"] += self.result.unmatched_trivia

    def title_targets(self, platform: str, title: str) -> list[tuple[int, int, bool]]:
        found: dict[tuple[int, int], bool] = {}
        for key in _title_keys(title):
            for disk_id, content_id, single in self.titles.get((platform, key), ()):
                found.setdefault((disk_id, content_id), single)
        return [(disk_id, content_id, single) for (disk_id, content_id), single in found.items()]


class _Crews:
    """Chooses the crew history of each disk and writes the crews table.

    Crew names are not unique: "Awesome" is an Atari ST menu crew and an
    unrelated Amiga demo group. A crew record describes one crew of one
    source and lists the platforms it released on, and it only ever goes to
    a disk of one of those platforms whose crew it names (compared by
    ``GroupRegistry.crew_keys``: as written or expanded through
    data/groups.toml, without "The"). From each source a disk takes the
    record of the crew its own records credit (``DiskRecord.crew_ids``);
    without such a credit, the one record of that source with the disk's
    crew name and platform. When a source has several such records, the one
    pinned for that name and platform in data/crew-pins.toml wins, else the
    one with most of their releases there when it has enough of them
    (``CrewChoice``); otherwise the source gives none, because a wrong
    history is worse than none. Disks with the same crew name, platform and
    records share a row and point at it with ``disks.crew_id``.
    """

    def __init__(
        self, batches: Iterable[SourceBatch], groups: GroupRegistry, choice: CrewChoice
    ) -> None:
        self.groups = groups
        self.choice = choice
        # (disk crew, platform, source) -> disks that took a pinned crew, or
        # the dominant one, from a source with several crews of that name.
        self.pinned: Counter[tuple[str, str, str]] = Counter()
        self.dominant: Counter[tuple[str, str, str]] = Counter()
        self.pins_used: set[tuple[str, str]] = set()  # (source, crew id)
        # Every record, sources by priority, so a record's index orders it.
        self.records: list[CrewRecord] = []
        for _order, batch in sorted(
            enumerate(batches), key=lambda item: (item[1].priority, item[0])
        ):
            self.records.extend(batch.crews)
        # (crew key, platform) -> source -> indexes of its records there.
        self.named: dict[tuple[str, str], dict[str, dict[int, None]]] = defaultdict(dict)
        for index, record in enumerate(self.records):
            for platform in record.platforms:
                for key in groups.crew_keys(record.name, platform):
                    self.named[(key, platform)].setdefault(record.source, {})[index] = None
        # (disk crew, platform, record indexes) -> crews.id
        self.rows: dict[tuple[str, str, tuple[int, ...]], int] = {}
        # (disk crew, platform, source) -> disks left without that source's
        # history because it has several crews of the name there.
        self.ambiguous: Counter[tuple[str, str, str]] = Counter()

    def row_for(self, crew: str, platform: str, credited: set[tuple[str, str]]) -> int | None:
        """The crews row of a disk of ``crew`` on ``platform``, or None.

        ``credited`` holds the crews the disk's records credit, as
        (source, CrewRecord.id).
        """
        if not crew or crew == UNKNOWN_CREW:
            return None
        keys = self.groups.crew_keys(crew, platform)
        candidates: dict[str, dict[int, None]] = {}
        for key in keys:
            for source, indexes in self.named.get((key, platform), {}).items():
                candidates.setdefault(source, {}).update(indexes)
        chosen: list[int] = []
        for source, indexes in candidates.items():
            linked = [
                index
                for index in indexes
                if self.records[index].id and (source, self.records[index].id) in credited
            ]
            if linked:
                chosen.append(min(linked))
            elif len(indexes) == 1:
                chosen.extend(indexes)
            else:
                picked = self.pick(crew, keys, platform, source, list(indexes))
                if picked is None:
                    self.ambiguous[(crew, platform, source)] += 1
                else:
                    chosen.append(picked)
        if not chosen:
            return None
        return self.rows.setdefault((crew, platform, tuple(sorted(chosen))), len(self.rows) + 1)

    def pick(
        self, crew: str, keys: set[str], platform: str, source: str, indexes: list[int]
    ) -> int | None:
        """One of a source's several crews of the disk's crew name, or None."""
        pin = self.choice.pinned(keys, platform, source)
        if pin is not None:
            found = [index for index in indexes if self.records[index].id == pin]
            if found:
                self.pins_used.add((source, pin))
                self.pinned[(crew, platform, source)] += 1
                return found[0]
        releases = {index: self.records[index].platforms.get(platform, 0) for index in indexes}
        dominant = self.choice.dominant(releases)
        if dominant is not None:
            self.dominant[(crew, platform, source)] += 1
        return dominant

    def write(self, connection: sqlite3.Connection, log: Callable[[str], None]) -> dict[str, int]:
        """Write the rows; notes from several sources are kept in source order,
        members pooled, and the other values taken from the first source that
        gives one."""
        for (name, platform, chosen), row_id in self.rows.items():
            records = [self.records[index] for index in chosen]
            notes = dict.fromkeys(r.notes.strip() for r in records if r.notes.strip())
            members = dict.fromkeys(m.strip() for r in records for m in r.members if m.strip())
            connection.execute(
                "INSERT INTO crews(id, name, platform, notes, members, founded, source, url, "
                "wikipedia) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    row_id,
                    name,
                    platform,
                    "\n\n".join(notes),
                    ", ".join(members),
                    next((r.founded for r in records if r.founded), ""),
                    ", ".join(dict.fromkeys(r.source for r in records if r.source)),
                    next((r.url for r in records if r.url), ""),
                    next((r.wikipedia for r in records if r.wikipedia), ""),
                ),
            )
        used = {index for _name, _platform, chosen in self.rows for index in chosen}
        unmatched = len(self.records) - len(used)
        if unmatched:
            log(f"merge: {unmatched} crew records describe the crew of no disk")
        if self.ambiguous:
            worst = sorted(self.ambiguous.items(), key=lambda item: (-item[1], item[0]))
            log(
                f"merge: {sum(self.ambiguous.values())} disks of {len(self.ambiguous)} crew "
                "names take no history from a source with several crews of that name on "
                "their platform and no credit to tell them apart: "
                + ", ".join(
                    f"{name} ({platform}, {source}, {count})"
                    for (name, platform, source), count in worst[:20]
                )
            )
        unused = sorted(
            f"{name} ({platform}, {source} {crew_id})"
            for (name, platform, source), crew_id in self.choice.pins.items()
            if (source, crew_id) not in self.pins_used
        )
        if unused:
            log(f"merge: crew pins that chose no crew: {', '.join(unused)}")
        return {
            "crews": len(self.rows),
            "crews unmatched": unmatched,
            "crews ambiguous": sum(self.ambiguous.values()),
            "crews pinned": sum(self.pinned.values()),
            "crews dominant": sum(self.dominant.values()),
        }


def spelling_key(crew: str) -> str:
    """A crew name with case, "The", spaces and punctuation left out."""
    return re.sub(r"[^0-9a-z]", "", crew_key(crew).casefold()) or crew.casefold()


def write_catalogue(
    connection: sqlite3.Connection,
    result: MergeResult,
    *,
    meta: dict[str, str] | None = None,
    groups: GroupRegistry | None = None,
    log: Callable[[str], None] = lambda message: None,
    crew_choice: CrewChoice | None = None,
) -> dict[str, int]:
    """Create the catalogue tables in an empty database and fill them."""
    groups = groups or GroupRegistry.load()
    crew_choice = crew_choice or CrewChoice.load()
    schema.create(connection)
    series = result.series

    prepared: list[tuple[_Disk, list[DiskRecord], str, str]] = []
    for disk in result.disks:
        disk.records.sort(key=lambda item: (item[0], item[1]))
        records = [record for _priority, _sequence, record in disk.records]
        title = _first(records, "title")
        prepared.append((disk, records, title, _label(disk, series.get(disk.series_key), title)))
    prepared.sort(key=lambda item: _disk_order(item[0], series.get(item[0].series_key), item[3]))
    for number, (disk, _records, _title, _label_text) in enumerate(prepared, start=1):
        disk.id = number

    for definition in sorted(series.values(), key=lambda s: sort_key(s.name)):
        connection.execute(
            "INSERT INTO series(id, name, platform, kind, group_name, description) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                definition.id,
                definition.name,
                definition.platform,
                definition.kind,
                definition.group,
                definition.description,
            ),
        )
        aliases = {normalise(definition.name)} | {normalise(a) for a in definition.aliases}
        connection.executemany(
            "INSERT INTO series_alias(alias, series_id) VALUES (?, ?)",
            [(alias, definition.id) for alias in sorted(aliases) if alias],
        )

    writer = _Writer(connection, result, groups, log, crew_choice)
    writer.settle_crews(
        (disk.platform, writer.disk_crew(disk, records, series.get(disk.series_key)))
        for disk, records, _title, _label in prepared
    )
    for disk, records, title, label in prepared:
        writer.write_disk(disk, records, title, label, series.get(disk.series_key))
    writer.write_title_media()
    writer.write_shared_text()
    writer.write_trivia_rows()
    writer.stats.update(writer.crews.write(connection, log))
    stats = writer.stats

    known = {batch.info.id for batch in result.batches}
    credited = [
        (CREDITED_SOURCES[source], writer.media_sources[source])
        for source in sorted(set(writer.media_sources) - known)
        if source in CREDITED_SOURCES
    ]
    for source in sorted(set(writer.media_sources) - known - set(CREDITED_SOURCES)):
        log(
            f"merge: {writer.media_sources[source]} media and trivia rows name source "
            f"{source!r}, which is not in the sources table"
        )
    for name in ("media unmatched", "trivia unmatched"):
        if stats[name]:
            log(f"merge: {stats[name]} {name.split()[0]} records match no disk or title")

    retrieved = (meta or {}).get("built_at", "")[:10]
    connection.executemany(
        "INSERT OR REPLACE INTO sources(id, name, url, licence, retrieved, records) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        [(i.id, i.name, i.url, i.licence, retrieved, count) for i, count in credited],
    )
    used = dict(connection.execute("SELECT provider, count(*) FROM locations GROUP BY provider"))
    connection.executemany(
        "INSERT OR REPLACE INTO sources(id, name, url, licence, retrieved, records) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        [
            (host.id, host.name, host.url, host.licence, retrieved, used[host.id])
            for host in HOSTS.values()
            if host.id in used and host.id not in known
        ],
    )
    for batch in result.batches:
        connection.execute(
            "INSERT OR REPLACE INTO sources(id, name, url, licence, retrieved, records) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                batch.info.id,
                batch.info.name,
                batch.info.url,
                batch.info.licence,
                batch.retrieved or retrieved,
                len(batch.records),
            ),
        )
    entries = {"licence": CATALOGUE_LICENCE, **(meta or {})}
    for batch in result.batches:
        entries[f"source:{batch.info.id}"] = batch.status
    entries["statistics"] = json.dumps(dict(sorted(stats.items())))
    connection.executemany(
        "INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)", sorted(entries.items())
    )
    for table in ("disk_fts", "disk_trigram", "entry_fts"):
        connection.execute(f"INSERT INTO {table}({table}) VALUES ('optimize')")
    stats["unmatched locations"] = result.unmatched_locations
    stats["hash merges"] = result.hash_merges
    stats["download merges"] = result.url_merges
    stats["download merges refused"] = result.url_refusals
    stats["series"] = len(series)
    return dict(stats)

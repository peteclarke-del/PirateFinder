"""Join the records of every source per disk and write the catalogue.

Rules, in the order they are applied:

1. Records with a key (series, number, part, version) describe the same disk
   whichever source they come from, and merge.
2. Records without a key merge into a disk that already owns one of their
   images, compared by MD5, SHA-1, SHA-512 or CRC32 with size. Otherwise they
   become a disk of their own, labelled from their title.
3. Records that carry only locations (no key, no images) attach each location
   to the disk owning an image of the same name (compared without case, with
   or without the extension) or with the same hash. Such a location never
   creates a disk: one that matches nothing is counted and logged. Only a
   record with a title whose locations name no image at all (an archive of a
   whole disk, known by its title) becomes a disk of its own.

The disk shows the contents of the best source that lists any (the lowest
``CONTENT_PRIORITY``), while the search index holds the titles from every
source. Other single values come from the first source, by priority, that
gives one; credits, notes and menu texts from all sources are kept.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import PurePosixPath

from piratefinder.catalogue import schema
from piratefinder.catalogue.naming import image_rank_key, normalise, sort_key, tidy_label

from .records import (
    ContentRecord,
    DiskRecord,
    ImageRecordIn,
    LocationRecord,
    SourceInfo,
    normalise_part,
    normalise_version,
)
from .series import GroupRegistry, SeriesDef, SeriesRegistry

DEFAULT_PRIORITY = 90
CATALOGUE_LICENCE = "CC BY-NC-SA 4.0"

_JOINED_WORD = re.compile(r"\w+(?:[-.'/]\w+)+")


@dataclass(slots=True)
class SourceBatch:
    """Everything one source produced, with what the merge needs to know about it."""

    info: SourceInfo
    records: list[DiskRecord]
    priority: int = DEFAULT_PRIORITY
    status: str = "ok"
    retrieved: str = ""


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
    id: int = 0


@dataclass(slots=True)
class MergeResult:
    disks: list[_Disk]
    series: dict[str, SeriesDef]
    batches: list[SourceBatch]
    unmatched_locations: int = 0
    hash_merges: int = 0


def search_text(text: str) -> str:
    """Normalised text for the search index.

    Words written with inner punctuation are indexed both split and joined,
    so "R-Type" is found by "r type" and by "rtype".
    """
    words = [normalise(text)]
    for found in _JOINED_WORD.findall(text):
        joined = normalise(re.sub(r"[-.'/]", "", found))
        if joined:
            words.append(joined)
    return " ".join(word for word in words if word)


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


def _name_keys(name: str) -> list[str]:
    base = PurePosixPath(name.replace("\\", "/")).name.lower()
    stem = base.rsplit(".", 1)[0] if "." in base else base
    return [base, stem] if stem != base else [base]


class _Merger:
    def __init__(self, registry: SeriesRegistry, log: Callable[[str], None]) -> None:
        self.registry = registry
        self.log = log
        self.keyed: dict[tuple, _Disk] = {}
        self.disks: list[_Disk] = []
        self.by_hash: dict[tuple, tuple[_Disk, _Image]] = {}
        self.shared_hashes: set[tuple] = set()
        self.hash_merges = 0
        self.unmatched_locations = 0
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
            names = set(_name_keys(image.name))
            for existing in disk.images:
                if names & set(_name_keys(existing.record.name)):
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

    # -- records -----------------------------------------------------------

    def add_record(self, disk: _Disk, record: DiskRecord, priority: int, sequence: int) -> None:
        disk.records.append((priority, sequence, record))
        for position, image in enumerate(record.images):
            self.add_image(disk, image, record.source, (priority, sequence, position))
        for location in record.locations:
            disk.locations.append((location, _image_for_location(disk, location)))

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
        for _index, batch in ordered:
            for record in batch.records:
                sequence += 1
                key = canonical_key(record)
                if key is not None:
                    keyed.append((batch.priority, sequence, record, key))
                elif record.images:
                    with_images.append((batch.priority, sequence, record))
                elif record.locations and not record.contents:
                    location_only.append((batch.priority, sequence, record))
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
                disk = self.new_disk(record, None)
            else:
                self.hash_merges += 1
            self.add_record(disk, record, priority, number)
        for priority, number, record in plain:
            self.add_record(self.new_disk(record, None), record, priority, number)
        self.attach_locations(location_only)
        for name, count in sorted(self.unknown_series.items()):
            self.log(
                f"merge: {count} records name series {name.split(':', 1)[1]!r} "
                f"that is not declared (from {name.split(':', 1)[0]})"
            )

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
        by_name: dict[str, list[tuple[_Disk, _Image]]] = defaultdict(list)
        by_value: dict[str, list[tuple[_Disk, _Image]]] = defaultdict(list)
        for disk in self.disks:
            for image in disk.images:
                for key in _name_keys(image.record.name):
                    by_name[key].append((disk, image))
                for value in (image.record.md5, image.record.sha1, image.record.sha512):
                    if value:
                        by_value[value.lower()].append((disk, image))
                if image.record.crc32:
                    by_value[f"crc:{image.record.crc32.lower()}"].append((disk, image))
        for priority, sequence, record in records:
            if record.title.strip() and not any(_names_an_image(loc) for loc in record.locations):
                self.add_record(self.new_disk(record, None), record, priority, sequence)
                continue
            owners = [_locate(location, by_name, by_value) for location in record.locations]
            for location, owner in zip(record.locations, owners, strict=True):
                if owner is None:
                    self.unmatched_locations += 1
                    continue
                owner[0].locations.append((location, owner[1]))
        if self.unmatched_locations:
            self.log(f"merge: {self.unmatched_locations} locations match no catalogue image")


def _names_an_image(location: LocationRecord) -> bool:
    return bool(location.image_name or location.member or location.hash_value)


def _locate(
    location: LocationRecord,
    by_name: dict[str, list[tuple[_Disk, _Image]]],
    by_value: dict[str, list[tuple[_Disk, _Image]]],
) -> tuple[_Disk, _Image] | None:
    value = location.hash_value.lower()
    if value:
        if location.hash_kind.lower() in ("crc32", "crc"):
            candidates = by_value.get(f"crc:{value}", [])
            if location.size is not None:
                candidates = [c for c in candidates if c[1].record.size == location.size]
        else:
            candidates = by_value.get(value, [])
        if candidates:
            return candidates[0]
    for key in _name_keys(location.image_name or location.member):
        if key and by_name.get(key):
            return by_name[key][0]
    return None


def _image_for_location(disk: _Disk, location: LocationRecord) -> _Image | None:
    value = location.hash_value.lower()
    names = set(_name_keys(location.image_name)) if location.image_name else set()
    for image in disk.images:
        record = image.record
        if value and value in (record.md5, record.sha1, record.sha512, record.crc32):
            return image
        if names and names & set(_name_keys(record.name)):
            return image
    return None


def _image_fields(image: ImageRecordIn) -> dict:
    return {name: getattr(image, name) for name in ImageRecordIn.__slots__}


def _fill_image(target: ImageRecordIn, other: ImageRecordIn) -> None:
    for name in ("crc32", "md5", "sha1", "sha512", "flags", "format"):
        if not getattr(target, name) and getattr(other, name):
            setattr(target, name, getattr(other, name))
    if target.size is None and other.size is not None:
        target.size = other.size


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
    )


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
    return (
        disk.platform,
        0 if series else 1,
        sort_key(series.name) if series else "",
        disk.number if disk.number is not None else -1,
        disk.part,
        disk.version,
        sort_key(label),
    )


def write_catalogue(
    connection: sqlite3.Connection,
    result: MergeResult,
    *,
    meta: dict[str, str] | None = None,
    groups: GroupRegistry | None = None,
    log: Callable[[str], None] = lambda message: None,
) -> dict[str, int]:
    """Create the catalogue tables in an empty database and fill them."""
    groups = groups or GroupRegistry.load()
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

    image_id = 0
    stats: Counter[str] = Counter()
    for disk, records, title, label in prepared:
        definition = series.get(disk.series_key)
        contents = _display_contents(disk.records)
        connection.execute(
            "INSERT INTO disks(id, series_id, number, part, version, label, title, date, "
            "platform, kind, publisher, cracker, condition, notes, credits, menu_text) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
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
                _first(records, "publisher"),
                _first(records, "cracker"),
                _first(records, "condition"),
                _joined(records, "notes"),
                _joined(records, "credits"),
                _joined(records, "menu_text"),
            ),
        )
        connection.executemany(
            "INSERT INTO contents(disk_id, position, title, kind, publisher, cracker, "
            "version, extra, source) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
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
                for position, (content, source) in enumerate(contents, start=1)
            ],
        )
        ranked = sorted(
            disk.images,
            key=lambda image: (
                max(image_rank_key(image.record.flags)[0], 2 if image.record.bad else 0),
                image_rank_key(image.record.flags)[1:],
                image.order,
            ),
        )
        for rank, image in enumerate(ranked):
            image_id += 1
            image.id = image_id
            item = image.record
            connection.execute(
                "INSERT INTO images(id, disk_id, name, format, flags, size, crc32, md5, sha1, "
                "sha512, bad, rank, source) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    image_id,
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
                ),
            )
        seen_locations: set[tuple[str, str, str]] = set()
        for location, image in disk.locations:
            identity = (location.provider, location.url, location.member)
            if identity in seen_locations:
                continue
            seen_locations.add(identity)
            connection.execute(
                "INSERT INTO locations(disk_id, image_id, provider, url, container, member, "
                "size, hash_kind, hash_value, page_url, priority) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    disk.id,
                    image.id if image is not None else None,
                    location.provider,
                    location.url,
                    location.container,
                    location.member,
                    location.size,
                    location.hash_kind,
                    location.hash_value.lower(),
                    location.page_url,
                    location.priority,
                ),
            )
        links = dict.fromkeys((label_, url) for record in records for label_, url in record.links)
        connection.executemany(
            "INSERT INTO links(disk_id, label, url) VALUES (?, ?, ?)",
            [(disk.id, link_label, url) for link_label, url in links],
        )
        _index_disk(connection, disk, label, title, definition, records, groups)

        stats["disks"] += 1
        stats[f"disks:{disk.platform}:{disk.kind}"] += 1
        stats["with contents"] += bool(contents)
        stats["with images"] += bool(disk.images)
        stats["with locations"] += bool(seen_locations)
        stats["images"] += len(disk.images)
        stats["contents"] += len(contents)
        stats["locations"] += len(seen_locations)

    retrieved = (meta or {}).get("built_at", "")[:10]
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
    connection.execute("INSERT INTO disk_fts(disk_fts) VALUES ('optimize')")
    connection.execute("INSERT INTO disk_trigram(disk_trigram) VALUES ('optimize')")
    stats["unmatched locations"] = result.unmatched_locations
    stats["hash merges"] = result.hash_merges
    stats["series"] = len(series)
    return dict(stats)


def _index_disk(
    connection: sqlite3.Connection,
    disk: _Disk,
    label: str,
    title: str,
    definition: SeriesDef | None,
    records: Iterable[DiskRecord],
    groups: GroupRegistry,
) -> None:
    records = list(records)
    titles: dict[str, None] = {}
    publishers: dict[str, None] = {}
    extras: dict[str, None] = {}
    people: dict[str, None] = {}
    for record in records:
        for content in record.contents:
            titles.setdefault(content.title, None)
            if content.extra:
                extras.setdefault(content.extra, None)
            if content.publisher:
                publishers.setdefault(content.publisher, None)
            if content.cracker:
                for spelling in groups.spellings(content.cracker):
                    people.setdefault(spelling, None)
        if record.cracker:
            for spelling in groups.spellings(record.cracker):
                people.setdefault(spelling, None)
        if record.credits:
            people.setdefault(record.credits, None)
    series_text = ""
    if definition is not None:
        series_text = " ".join([definition.name, definition.group, *definition.aliases])
    notes = " ".join(
        text for record in records for text in (record.notes, record.menu_text) if text
    )
    connection.execute(
        "INSERT INTO disk_fts(rowid, label, series, contents, people, notes) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (
            disk.id,
            search_text(f"{label} {title}"),
            search_text(series_text),
            search_text(" ".join([*titles, *publishers, *extras])),
            search_text(" ".join(people)),
            search_text(notes),
        ),
    )
    connection.execute(
        "INSERT INTO disk_trigram(rowid, text) VALUES (?, ?)",
        (disk.id, normalise(" ".join([label, *titles]))),
    )

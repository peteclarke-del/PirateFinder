"""Amiga pack disks in the amigascne archive, read from its scene.org mirror.

The amigascne archive keeps some 11,000 pack disks (game compacts, demo,
music and tool packs) under ``Packdisks/<group>/`` and publishes a daily index
of every file with its CRC32 and size::

    62D3BAA6<TAB>901120<TAB>Packdisks/Skid_Row/Skid_Row-Compact111.adf

The archive's own server, ftp.amigascne.org, forbids automated access (its robots.txt disallows
everything and its mirror.txt forbids mirroring over FTP or HTTP), so the
index is read from the same tree on the scene.org mirror, whose robots.txt
allows it, and every download location points at that mirror too.

Only the index is fetched. Each ``.adf``, ``.dms``, ``.adz`` or ``.zip`` pack
disk becomes a download location. An ADF file is the raw disk, so its CRC32
and size identify the image the way TOSEC does; the other containers are
compressed, so their file CRC says nothing about the image and is not used.

Names such as ``SKID_ROW-Compact130.DMS`` or ``Effect-PrevailPack147.adf`` are
matched against "<group folder>/<file name without extension>" with the rules
in ``data/series/match-amigascne.toml``. A name without a rule is also tried
against the names and aliases of the declared series ("Prevail Pack" for
``PrevailPack147``), accepted only when the series belongs to the same group.
Unrecognised pack disks are kept as pack disks of their own, titled from the
file name and the group, and merge with other sources by image hash.
"""

from __future__ import annotations

import re
import urllib.parse
from collections.abc import Iterable, Iterator
from dataclasses import dataclass

from ..context import BuildContext
from ..records import (
    NO_LICENCE_STATED,
    DiskRecord,
    ImageRecordIn,
    LocationRecord,
    SourceInfo,
)
from ..series import SeriesMatch, SeriesRegistry, normalise

BASE = "https://ftp.scene.org/mirrors/amigascne/"
INFO = SourceInfo(
    id="amigascne",
    name="amigascne archive (scene.org mirror)",
    url=BASE,
    licence=NO_LICENCE_STATED,
)
DEFAULT_ENABLED = True

INDEX = BASE + "amigascne-index.txt"
PACK_ROOT = "Packdisks/"
PLATFORM = "amiga"
LOCATION_PRIORITY = 50
MAX_AGE_DAYS = 30.0
# File suffix -> container type of the download ("" when the file is the image).
CONTAINERS = {".adf": "", ".dms": "", ".adz": "gz", ".zip": "zip"}
_SECTOR = 512

# Dump notes in a file name: "[b]", "[v]" anywhere, "(b corrupt file)" at the end.
_FLAG = re.compile(r"\[[^\]]*\]|\s*\([^()]*\)\s*$")
_NUMBERED = re.compile(r"^(?P<base>.*?[A-Za-z!)])[\s_#.-]*(?P<number>\d+)(?P<part>[A-Za-z])?$")
_CAMEL = re.compile(r"(?<=[a-z])(?=[A-Z0-9])|(?<=[0-9])(?=[A-Za-z])|(?<=[A-Z])(?=[A-Z][a-z])")


@dataclass(frozen=True, slots=True)
class IndexEntry:
    crc32: str
    size: int
    path: str


@dataclass(frozen=True, slots=True)
class PackFile:
    entry: IndexEntry
    folder: str  # path between the section root and the file, "Skid_Row"
    stem: str  # file name without extension or dump flags, "SKID_ROW-Compact130"
    suffix: str  # ".dms"
    flags: str  # "[b]" and similar TOSEC style dump flags

    @property
    def name(self) -> str:
        return self.entry.path.rsplit("/", 1)[-1]

    @property
    def match_text(self) -> str:
        return f"{self.folder}/{self.stem}"


def parse_index(text: str) -> Iterator[IndexEntry]:
    for line in text.splitlines():
        fields = line.split("\t")
        if len(fields) != 3 or not re.fullmatch(r"[0-9A-Fa-f]{8}", fields[0].strip()):
            continue
        try:
            size = int(fields[1])
        except ValueError:
            continue
        yield IndexEntry(fields[0].strip().lower(), size, fields[2].strip())


def pack_file(entry: IndexEntry, root: str = PACK_ROOT) -> PackFile | None:
    if not entry.path.startswith(root):
        return None
    relative = entry.path[len(root) :]
    folder, _, name = relative.rpartition("/")
    stem, dot, suffix = name.rpartition(".")
    if not dot or f".{suffix.lower()}" not in CONTAINERS:
        return None
    flags = "".join(found.group(0).strip() for found in _FLAG.finditer(stem))
    stem = _FLAG.sub("", stem).strip()
    return PackFile(entry, folder, stem, f".{suffix.lower()}", flags)


def file_url(path: str) -> str:
    return BASE + urllib.parse.quote(path)


def split_words(text: str) -> str:
    """Words of a file name: "PrevailPack147" gives "Prevail Pack 147"."""
    spaced = _CAMEL.sub(" ", text.replace("_", " "))
    return " ".join(spaced.split())


def group_and_name(folder: str, stem: str) -> tuple[str, str]:
    """The releasing group and the pack name of a file ("Effect", "PrevailPack147").

    The group is the file name's prefix before the first dash, or the folder
    when the name has none. A folder spelt like the prefix ("Skid_Row" for
    "SKID_ROW") is preferred for its case.
    """
    folder_group = folder.rsplit("/", 1)[-1].replace("_", " ").strip()
    prefix, dash, name = stem.partition("-")
    if not dash or not name:
        return folder_group, stem
    prefix = prefix.replace("_", " ").strip()
    return (folder_group if normalise(folder_group) == normalise(prefix) else prefix), name


def title_for(folder: str, stem: str) -> str:
    group, name = group_and_name(folder, stem)
    return f"{split_words(name)} ({group})"


def identify(series: SeriesRegistry, folder: str, stem: str) -> SeriesMatch | None:
    """Series, number and part of a pack disk file, or None if no series claims it."""
    found = series.match(INFO.id, f"{folder}/{stem}", PLATFORM)
    if found is not None:
        return found if found.number is not None else None
    group, name = group_and_name(folder, stem)
    numbered = _NUMBERED.match(name)
    if numbered is None:
        return None
    base = split_words(numbered.group("base"))
    folder_group = folder.rsplit("/", 1)[-1].replace("_", " ")
    candidate = None
    for owner in (group, split_words(group), folder_group):
        candidate = candidate or series.by_name(f"{owner} {base}", PLATFORM)
    if candidate is None:
        candidate = series.by_name(base, PLATFORM)
        owners = {normalise(group), normalise(folder_group), normalise(group).replace(" ", "")}
        if candidate is None or not candidate.group or normalise(candidate.group) not in owners:
            return None
    return SeriesMatch(
        candidate.id, int(numbered.group("number")), (numbered.group("part") or "").upper(), ""
    )


def _image_and_location(pack: PackFile) -> tuple[ImageRecordIn | None, LocationRecord]:
    container = CONTAINERS[pack.suffix]
    raw_adf = pack.suffix == ".adf" and pack.entry.size > 0 and pack.entry.size % _SECTOR == 0
    image = None
    location = LocationRecord(
        provider="amigascne",
        url=file_url(pack.entry.path),
        container=container,
        size=pack.entry.size,
        page_url=file_url(pack.entry.path.rsplit("/", 1)[0] + "/"),
        priority=LOCATION_PRIORITY,
    )
    if raw_adf:
        image = ImageRecordIn(
            name=pack.name,
            format="adf",
            flags=pack.flags,
            size=pack.entry.size,
            crc32=pack.entry.crc32,
            bad=bool(re.search(r"[\[(]b\b|bad|corrupt", pack.flags, re.IGNORECASE)),
        )
        location.hash_kind = "crc32"
        location.hash_value = pack.entry.crc32
        location.image_name = pack.name
    return image, location


def records_from_index(ctx: BuildContext, entries: Iterable[IndexEntry]) -> Iterator[DiskRecord]:
    keyed: dict[tuple, DiskRecord] = {}
    loose: dict[tuple[str, str], DiskRecord] = {}
    for entry in entries:
        pack = pack_file(entry)
        if pack is None:
            continue
        found = identify(ctx.series, pack.folder, pack.stem)
        image, location = _image_and_location(pack)
        if found is not None:
            key = (found.series_id, found.number, found.part, found.version)
            record = keyed.get(key)
            if record is None:
                definition = ctx.series.get(found.series_id)
                record = keyed[key] = DiskRecord(
                    source=INFO.id,
                    platform=PLATFORM,
                    kind=definition.kind if definition else "pack",
                    series_key=found.series_id,
                    number=found.number,
                    part=found.part,
                    version=found.version,
                )
        else:
            loose_key = (pack.folder.lower(), normalise(pack.stem))
            record = loose.get(loose_key)
            if record is None:
                record = loose[loose_key] = DiskRecord(
                    source=INFO.id,
                    platform=PLATFORM,
                    kind="pack",
                    title=title_for(pack.folder, pack.stem),
                )
        if image is not None and all(known.crc32 != image.crc32 for known in record.images):
            record.images.append(image)
        record.locations.append(location)
    ctx.log(f"amigascne: {len(keyed)} numbered pack disks, {len(loose)} other pack disks")
    yield from keyed.values()
    yield from loose.values()


def fetch_index(ctx: BuildContext) -> str:
    local = ctx.input(INFO.id)
    if local is not None:
        return local.read_bytes().decode("latin-1")
    return ctx.fetch_text(INDEX, encoding="latin-1", max_age_days=MAX_AGE_DAYS)


def collect(ctx: BuildContext) -> Iterator[DiskRecord]:
    yield from records_from_index(ctx, parse_index(fetch_index(ctx)))

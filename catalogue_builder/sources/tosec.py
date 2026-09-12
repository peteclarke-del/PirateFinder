"""Disk names and image hashes from the TOSEC DAT pack.

The importer finds the newest "DAT Pack - Complete" on the TOSEC downloads
page, keeps it in the build cache and reads the DATs listed in ``DAT_SETS``
straight from the zip. ``--input tosec=PATH`` replaces the download with a
local pack, or with a folder of DAT files.

Every DAT game becomes part of a ``DiskRecord``:

- In compilation and pack DATs, a name that a ``match-tosec.toml`` pattern
  recognises gives the series, number, part and version. A numbered name
  such as "Prevail Pack #037" that no pattern knows registers a series of
  its own when the DAT holds several numbers of it.
- Every other game is filed under its name. Alternates ([a], [a2]) and other
  dumps of the same release (same title, version, date, publisher, media
  fields and cracker) collapse into one record with several images.
- Single-game DATs give one content item per disk: the game, its publisher
  and the crack and trainer groups, with abbreviations expanded through
  data/groups.toml.
"""

from __future__ import annotations

import html
import re
import urllib.parse
import xml.etree.ElementTree as ElementTree
import zipfile
from collections import defaultdict
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO

from piratefinder.catalogue.naming import (
    TosecName,
    display_title,
    image_rank_key,
    normalise,
    parse_tosec_name,
    split_combined,
)

from ..context import BuildContext, OfflineError
from ..records import (
    ContentRecord,
    DiskRecord,
    ImageRecordIn,
    SourceInfo,
    normalise_part,
    normalise_version,
)
from ..series import SeriesDef, SeriesMatch, slug

INFO = SourceInfo(
    id="tosec",
    name="TOSEC DAT pack",
    url="https://www.tosecdev.org/",
    licence="TOSEC DAT files, freely distributable",
)
CONTENT_PRIORITY = 90
DEFAULT_ENABLED = True
RETRIEVED = ""  # the pack version, set by collect()

DOWNLOADS_URL = "https://www.tosecdev.org/downloads"
PACK_CACHE_NAME = "tosec-dat-pack-{version}.zip"

ATARI_ST = "atari-st"
AMIGA = "amiga"


@dataclass(frozen=True, slots=True)
class DatSet:
    """One TOSEC DAT and how its games are filed."""

    name: str  # the DAT name without its version, as in the DAT header
    platform: str
    kind: str  # disk kind for games that are not in a declared series
    content_kind: str  # kind of the content items read from game names
    compilation: bool  # multi-release disks: series patterns and generic numbering apply


DAT_SETS: tuple[DatSet, ...] = (
    DatSet("Atari ST - Compilations - Games - [ST]", ATARI_ST, "menu", "game", True),
    DatSet("Atari ST - Compilations - Games - [STX]", ATARI_ST, "menu", "game", True),
    DatSet("Atari ST - Compilations - Demos", ATARI_ST, "pack", "demo", True),
    DatSet("Atari ST - Compilations - Applications - [ST]", ATARI_ST, "pack", "utility", True),
    DatSet("Atari ST - Games - [ST]", ATARI_ST, "single", "game", False),
    DatSet("Atari ST - Games - [STX]", ATARI_ST, "single", "game", False),
    DatSet("Commodore Amiga - Compilations - Games", AMIGA, "menu", "game", True),
    DatSet("Commodore Amiga - Compilations - Various", AMIGA, "menu", "other", True),
    DatSet("Commodore Amiga - Compilations - Applications", AMIGA, "pack", "utility", True),
    DatSet("Commodore Amiga - Demos - Packs", AMIGA, "pack", "demo", True),
    DatSet("Commodore Amiga - Demos - Music", AMIGA, "pack", "music", True),
    DatSet("Commodore Amiga - Games - [ADF]", AMIGA, "single", "game", False),
)

# A numbered compilation name: "<name> <marker> <number>". The marker words
# are the generic ways TOSEC names a numbered disk; no crew is named here.
_NUMBERED = re.compile(
    r"^(?P<name>.*?[^\s#,:-])[\s,:-]*"
    r"(?P<marker>#|No\.|Nr\.|Vol\.|Volume|Issue|Number)?\s*(?P<number>\d{1,5})$",
    re.IGNORECASE,
)
MARKER_WORDS = frozenset(
    {
        "menu",
        "disk",
        "disc",
        "diskette",
        "compact",
        "compacted",
        "compack",
        "compackt",
        "pack",
        "pak",
        "packdisk",
        "megapack",
        "cd",
        "compil",
        "compile",
        "compilation",
        "collection",
        "demodisk",
        "musicdisk",
        "bundle",
        "issue",
        "volume",
        "utilities",
        "utils",
        "tools",
        "toolsdisk",
        "games",
    }
)
# Distinct numbers a name needs before it is taken for a series.
MARKED_MINIMUM = 2
UNMARKED_MINIMUM = 4

_FLAG_FIELD = re.compile(r"-?\[[^\]]*\]")
_CATEGORY = re.compile(r'href="(/downloads/category/\d+-(\d{4}-\d{2}-\d{2}))"')
_PACK_LINK = re.compile(r'href="([^"]*\?download=\d+:tosec-dat-pack-complete[^"]*)"', re.I)
_DAT_VERSION = re.compile(r"^(?P<name>.+?) \(TOSEC-v(?P<version>[\d-]+)(?:_CM)?\)\.dat$")
_PACK_VERSION = re.compile(r"(\d{4}-\d{2}-\d{2})")


@dataclass(slots=True)
class _Entry:
    name: str
    parsed: TosecName
    images: list[ImageRecordIn]
    match: SeriesMatch | None = None
    combined: bool = False


@dataclass(slots=True)
class _Candidate:
    name: str
    publisher: str
    numbers: set[int] = field(default_factory=set)
    marked: bool = False


def collect(ctx: BuildContext) -> Iterator[DiskRecord]:
    global RETRIEVED
    source = ctx.input(INFO.id) or _download_pack(ctx)
    found = _PACK_VERSION.search(source.name)
    RETRIEVED = found.group(1) if found else ""
    wanted = {dat_set.name: dat_set for dat_set in DAT_SETS}
    seen: set[str] = set()
    for dat_name, version, opener in _dat_files(source):
        dat_set = wanted.get(dat_name)
        if dat_set is None:
            continue
        seen.add(dat_name)
        if not found:
            RETRIEVED = max(RETRIEVED, version)
        with opener() as handle:
            records = list(read_dat(ctx, dat_set, handle))
        keyed = sum(1 for record in records if record.key is not None)
        ctx.log(f"tosec: {dat_name} (v{version}): {len(records)} disks, {keyed} in series")
        yield from records
    for missing in sorted(set(wanted) - seen):
        ctx.log(f"tosec: {missing} is not in {source.name}")


def _download_pack(ctx: BuildContext) -> Path:
    """The newest complete DAT pack, from the cache or the TOSEC site."""
    try:
        page = ctx.fetch_text(DOWNLOADS_URL, name="downloads.html", max_age_days=1)
        categories = _CATEGORY.findall(page)
        if not categories:
            raise RuntimeError(f"no DAT pack releases listed on {DOWNLOADS_URL}")
        path, version = max(categories, key=lambda item: item[1])
        category_url = urllib.parse.urljoin(DOWNLOADS_URL, path)
        listing = ctx.fetch_text(category_url, name=f"category-{version}.html", max_age_days=30)
        link = _PACK_LINK.search(listing)
        if link is None:
            raise RuntimeError(f"no complete DAT pack on {category_url}")
        pack_url = urllib.parse.urljoin(category_url, html.unescape(link.group(1)))
        ctx.log(f"tosec: DAT pack {version}")
        return ctx.fetch(
            pack_url,
            name=PACK_CACHE_NAME.format(version=version),
            min_interval=2.0,
            max_age_days=36500,
        )
    except OfflineError:
        cached = sorted(ctx.cache_dir.glob("*/*-" + PACK_CACHE_NAME.format(version="*")))
        if not cached:
            raise
        ctx.log(f"tosec: offline, using cached {cached[-1].name}")
        return cached[-1]


def _dat_files(source: Path) -> Iterator[tuple[str, str, object]]:
    """(DAT name, DAT version, opener) for every DAT in a pack or folder."""
    if source.is_dir():
        for path in sorted(source.rglob("*.dat")):
            found = _DAT_VERSION.match(path.name)
            name, version = (found["name"], found["version"]) if found else (path.stem, "")
            yield name, version, (lambda path=path: path.open("rb"))
        return
    with zipfile.ZipFile(source) as archive:
        for member in archive.namelist():
            base = member.rsplit("/", 1)[-1]
            if not base.endswith(".dat") or not member.startswith("TOSEC/"):
                continue
            found = _DAT_VERSION.match(base)
            name, version = (found["name"], found["version"]) if found else (base[:-4], "")
            yield name, version, (lambda member=member: archive.open(member))


def read_dat(ctx: BuildContext, dat_set: DatSet, handle: IO[bytes]) -> Iterator[DiskRecord]:
    """Turn one DAT into disk records, grouped as described in the module notes."""
    entries: list[_Entry] = []
    for _event, element in ElementTree.iterparse(handle, events=("end",)):
        if element.tag != "game":
            continue
        name = element.get("name", "")
        parsed = parse_tosec_name(name)
        images = [_image(rom, parsed) for rom in element.iter("rom")]
        element.clear()
        entry = _Entry(name, parsed, images, combined=len(split_combined(name)) > 1)
        if dat_set.compilation and not entry.combined:
            entry.match = ctx.series.match(INFO.id, name, dat_set.platform)
        entries.append(entry)
    if dat_set.compilation:
        _number_generic_series(ctx, dat_set, entries)
    yield from _records(ctx, dat_set, entries)


def _image(rom: ElementTree.Element, parsed: TosecName) -> ImageRecordIn:
    name = rom.get("name", "")
    size = rom.get("size", "")
    return ImageRecordIn(
        name=name,
        format=name.rsplit(".", 1)[-1].lower() if "." in name else "",
        flags="".join(f"[{flag}]" for flag in parsed.flags),
        size=int(size) if size.isdigit() else None,
        crc32=rom.get("crc", "").lower(),
        md5=rom.get("md5", "").lower(),
        sha1=rom.get("sha1", "").lower(),
        bad=parsed.bad,
    )


def numbered_name(title: str) -> tuple[str, int, bool] | None:
    """("Prevail Pack", 37, True) for "Prevail Pack #037"; None if unnumbered.

    The flag says whether the number is marked as a disk number, by "#",
    "No." and the like or by a word such as "Pack" or "Disk" in front of it.
    """
    found = _NUMBERED.match(display_title(title))
    if found is None:
        return None
    name = found["name"].strip()
    last_word = normalise(name).rsplit(" ", 1)[-1] if normalise(name) else ""
    marked = bool(found["marker"]) or last_word in MARKER_WORDS
    return name, int(found["number"]), marked


def _number_generic_series(ctx: BuildContext, dat_set: DatSet, entries: list[_Entry]) -> None:
    """Register series for numbered names that no declared pattern matched."""
    candidates: dict[tuple[str, str], _Candidate] = {}
    pending: list[tuple[_Entry, tuple[str, str], int]] = []
    for entry in entries:
        if entry.match is not None or entry.combined:
            continue
        numbered = numbered_name(entry.parsed.title)
        if numbered is None:
            continue
        name, number, marked = numbered
        publisher = entry.parsed.publisher.split(" - ")[0].strip()
        key = (normalise(name), normalise(publisher))
        candidate = candidates.setdefault(key, _Candidate(name, publisher))
        candidate.numbers.add(number)
        candidate.marked = candidate.marked or marked
        pending.append((entry, key, number))
    registered: dict[tuple[str, str], str] = {}
    for key, candidate in candidates.items():
        needed = MARKED_MINIMUM if candidate.marked else UNMARKED_MINIMUM
        if len(candidate.numbers) >= needed:
            registered[key] = _register(ctx, dat_set, candidate).id
    for entry, key, number in pending:
        series_id = registered.get(key)
        if series_id is not None:
            entry.match = SeriesMatch(series_id, number, "", "")


def _declared(ctx: BuildContext, dat_set: DatSet, candidate: _Candidate) -> SeriesDef | None:
    """A declared series this numbered name belongs to, found by name or alias.

    "Serenade PD Disk" by Serenade finds a series with that alias. A bare
    name ("Compact") only counts when the series is by the same publisher, so
    another crew's disks are not filed under it.
    """
    name, publisher = candidate.name, candidate.publisher
    if publisher:
        found = ctx.series.by_name(f"{publisher} {name}", dat_set.platform)
        if found is not None:
            return found
    found = ctx.series.by_name(name, dat_set.platform)
    if found is None:
        return None
    if found.group and publisher and normalise(found.group) != normalise(publisher):
        return None
    return found


def _register(ctx: BuildContext, dat_set: DatSet, candidate: _Candidate) -> SeriesDef:
    declared = _declared(ctx, dat_set, candidate)
    if declared is not None:
        return declared
    name, publisher = candidate.name, candidate.publisher
    anonymous = not normalise(publisher) or normalise(publisher) in normalise(name)
    series_id = slug(name if anonymous else f"{publisher} {name}")
    existing = ctx.series.get(series_id)
    if existing is not None and existing.platform != dat_set.platform:
        series_id = f"{series_id}-{dat_set.platform}"
    label = f"{_escape(name)} {{number}}{{part_suffix}}{{version_suffix}}"
    if not anonymous:
        label += f" ({_escape(publisher)})"
    definition = SeriesDef(
        id=series_id,
        name=name if anonymous else f"{name} ({publisher})",
        platform=dat_set.platform,
        kind=dat_set.kind,
        group="" if anonymous else publisher,
        label=label,
        aliases=[name],
        description="Recognised from numbered TOSEC names.",
    )
    return ctx.series.add(definition)


def _escape(text: str) -> str:
    """Protect braces in a name that becomes part of a label format."""
    return text.replace("{", "{{").replace("}", "}}")


def _flagless(name: str) -> str:
    return _FLAG_FIELD.sub("", name).strip()


def _records(ctx: BuildContext, dat_set: DatSet, entries: list[_Entry]) -> Iterator[DiskRecord]:
    groups: dict[tuple, list[_Entry]] = defaultdict(list)
    for entry in entries:
        if entry.match is not None and entry.match.number is not None:
            part = entry.match.part or entry.parsed.part
            version = entry.match.version or entry.parsed.version
            key: tuple = (
                "series",
                entry.match.series_id,
                entry.match.number,
                normalise_part(part),
                _version(version),
            )
        else:
            key = ("name", _flagless(entry.name), entry.parsed.cracker)
        groups[key].append(entry)
    for key, members in groups.items():
        members.sort(key=lambda entry: (image_rank_key(entry.parsed.flags), entry.name))
        if key[0] == "series":
            yield _series_record(ctx, dat_set, key, members)
        else:
            yield _named_record(ctx, dat_set, members)


def _version(version: str) -> str:
    """Keep one spelling of a version: "v2.0" and "v2" are the same release."""
    return re.sub(r"(?:\.0+)+$", "", normalise_version(version))


def _images(members: Iterable[_Entry]) -> list[ImageRecordIn]:
    images = [image for entry in members for image in entry.images]
    images.sort(key=lambda image: (image_rank_key(image.flags), image.name))
    return images


def _date(value: str) -> str:
    return "" if not value or value.lower().endswith("xx") and len(value) == 4 else value


def _series_record(
    ctx: BuildContext, dat_set: DatSet, key: tuple, members: list[_Entry]
) -> DiskRecord:
    _tag, series_id, number, part, version = key
    best = members[0]
    series = ctx.series.get(series_id)
    crackers = [entry.parsed.cracker for entry in members if entry.parsed.cracker]
    return DiskRecord(
        source=INFO.id,
        platform=dat_set.platform,
        kind=series.kind if series else dat_set.kind,
        series_key=series_id,
        number=number,
        part=part,
        version=version,
        title=_flagless(best.name),
        date=_date(best.parsed.date),
        publisher=best.parsed.publisher,
        cracker=ctx.groups.expand(" - ".join(dict.fromkeys(crackers))) if crackers else "",
        contents=_listed_contents(ctx, dat_set, best),
        images=_images(members),
    )


def _named_record(ctx: BuildContext, dat_set: DatSet, members: list[_Entry]) -> DiskRecord:
    best = members[0]
    parsed = best.parsed
    title = _flagless(best.name)
    if parsed.cracker:
        title = f"{title}[cr {parsed.cracker}]"
    if not dat_set.compilation:
        kind = dat_set.kind
        contents = [_content(ctx, dat_set, parsed)]
    elif best.combined:
        kind = "compilation"
        contents = [
            _content(ctx, dat_set, parse_tosec_name(part)) for part in split_combined(best.name)
        ]
    else:
        kind = dat_set.kind
        contents = _listed_contents(ctx, dat_set, best) or _joined_contents(dat_set, best)
    return DiskRecord(
        source=INFO.id,
        platform=dat_set.platform,
        kind=kind,
        part=_media_part(parsed),
        version=parsed.version,
        title=title,
        date=_date(parsed.date),
        publisher=parsed.publisher,
        cracker=ctx.groups.expand(parsed.cracker) if parsed.cracker else "",
        contents=contents,
        images=_images(members),
    )


def _media_part(parsed: TosecName) -> str:
    disk_of = parsed.disk_of
    return f"{disk_of[0]} of {disk_of[1]}" if disk_of else ""


def _content(ctx: BuildContext, dat_set: DatSet, parsed: TosecName) -> ContentRecord:
    notes = [field for field in parsed.extra if not field.lower().startswith(("disk ", "disc "))]
    if parsed.trained:
        trainer = f"{parsed.trainer_count} trainer" if parsed.trainer_count else "trainer"
        if parsed.trainer_group:
            trainer = f"{trainer} by {ctx.groups.expand(parsed.trainer_group)}"
        notes.append(trainer.strip())
    return ContentRecord(
        title=display_title(parsed.title),
        kind=dat_set.content_kind,
        publisher=parsed.publisher,
        cracker=ctx.groups.expand(parsed.cracker) if parsed.cracker else "",
        version=parsed.version,
        extra=", ".join(notes),
    )


# Words that make "A & B" one title ("Copy & Utility Disk", "Packer & Tools
# Disc 11") rather than two programs.
GENERIC_WORDS = frozenset(
    {"utility", "utilities", "utils", "tool", "tools", "disk", "disc", "system", "systemdisk"}
    | {"games", "programs", "demos", "board"}
)
_OTHERS = re.compile(r"^\d+ others?$", re.IGNORECASE)


def _joined_contents(dat_set: DatSet, entry: _Entry) -> list[ContentRecord]:
    """Titles joined by " & " in a compilation name.

    "Rick Dangerous & Cybernoid II & Arkanoid - Revenge of Doh" names three
    games. A lead-in that is the publisher ("Astronut & Wise Man - Spike &
    Dizzy Diamonds" by Astronut - Wise Man) or a numbered name ("Amiga Games
    9 - Missile & Cosmo") is dropped first. Two titles count only when
    neither contains a generic word, so "Copy & Utility Disk" stays whole.
    """
    title = entry.parsed.title
    head, separator, rest = title.partition(" - ")
    if separator and " & " in rest:
        crew = set(normalise(entry.parsed.publisher).split()) - {"and", "the"}
        lead = set(normalise(head).split()) - {"and", "the"}
        if (lead and lead <= crew) or numbered_name(head) is not None:
            title = rest
    parts = [part.strip() for part in title.split(" & ") if part.strip()]
    parts = [part for part in parts if not _OTHERS.match(part)]
    if len(parts) < 2:
        return []
    if len(parts) == 2 and any(GENERIC_WORDS & set(normalise(part).split()) for part in parts):
        return []
    return [ContentRecord(title=display_title(part), kind=dat_set.content_kind) for part in parts]


def _listed_contents(ctx: BuildContext, dat_set: DatSet, entry: _Entry) -> list[ContentRecord]:
    """Titles a compilation name lists after its own name.

    "A-Ha Menu - Eliminator - Nebulus" lists two games and "TMF Compact -
    Quadralien & Carrier Command" two more. The part before the first " - "
    must end in a word such as "Menu" or "Compact", so a subtitle such as
    "Mind Funk #118 - The Party" is not taken for contents.
    """
    title = entry.parsed.title
    head, separator, rest = title.partition(" - ")
    if not separator or not rest.strip():
        return []
    last_word = normalise(head).rsplit(" ", 1)[-1] if normalise(head) else ""
    if last_word not in MARKER_WORDS or numbered_name(rest) is not None:
        return []
    items = rest.split(" & ") if " & " in rest else rest.split(" - ")
    return [
        ContentRecord(title=display_title(item.strip()), kind=dat_set.content_kind)
        for item in items
        if item.strip()
    ]

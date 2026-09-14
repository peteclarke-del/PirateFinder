"""Intermediate records every source importer produces.

Importers know nothing about the catalogue database. Each one turns its source
into these records; ``catalogue_builder.merge`` joins records from different
sources that describe the same disk (by series, number, part and version) and
writes the catalogue.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import PurePosixPath

_PART_OF = re.compile(r"^(?:disk|side|part)?\s*(\d+)\s*(?:of\s*\d+)?$", re.IGNORECASE)


def normalise_part(part: str) -> str:
    """One spelling for disk parts across sources.

    "Disk 1 of 2", "1 of 2", "1" and "a" all become "A", so a TOSEC name, a
    D-Bug menu letter and an Atari Legend part number for the same half of a
    two-disk menu merge into one disk.
    """
    text = part.strip()
    if not text:
        return ""
    numbered = _PART_OF.match(text)
    if numbered:
        index = int(numbered.group(1))
        return chr(ord("A") + index - 1) if 1 <= index <= 26 else str(index)
    if len(text) == 1 and text.isalpha():
        return text.upper()
    return text.upper()


def base_name(name: str) -> str:
    """A file name without its folders, "/" or "\\" separated."""
    return PurePosixPath(name.replace("\\", "/")).name


def image_name_keys(name: str) -> list[str]:
    """The keys an image file name is compared by: without folders or case,
    with and without its extension. Merge ties locations and pictures to
    images this way, and importers that look names up in a DAT use it too."""
    base = base_name(name).lower()
    stem = base.rsplit(".", 1)[0] if "." in base else base
    return [base, stem] if stem != base else [base]


_LATIN_ORDINALS = {"bis": "2", "ter": "3", "quater": "4"}


def normalise_version(version: str) -> str:
    """Version spellings to one form: "Version 2", "v2", "V2.0", "bis" -> "v2".

    The first edition is unversioned: TOSEC leaves it without a version while
    other sources call it "1", so "v1" and "v1.0" normalise to "". No TOSEC
    disk has both an unversioned and a v1 entry, so nothing collides.
    """
    text = version.strip().lower()
    if text in _LATIN_ORDINALS:
        text = _LATIN_ORDINALS[text]
    found = re.search(r"(\d+)(?:\.(\d+))?", text)
    if not found:
        return text
    major, minor = found.group(1).lstrip("0") or "0", (found.group(2) or "").rstrip("0")
    number = f"{major}.{minor}" if minor else major
    return "" if number == "1" else f"v{number}"


@dataclass(slots=True)
class ContentRecord:
    title: str
    kind: str = "game"  # a piratefinder.models.ContentKind value
    publisher: str = ""
    cracker: str = ""
    version: str = ""
    extra: str = ""
    # Reference ids for this title: ("wikipedia", "Rick Dangerous"),
    # ("atari-legend-game", "329"), ("demozoo", "91700").
    links: list[tuple[str, str]] = field(default_factory=list)


@dataclass(slots=True)
class MediaRecordIn:
    """A picture of a disc or of one title on it, fetched by the app on demand.

    A media record inside a DiskRecord belongs to that disc, or to the title
    named by ``content_title``. A MEDIA-ONLY record (a DiskRecord with no key,
    images or contents) attaches instead by ``image_name`` (the disc owning an
    image of that file name, compared as for locations) or by ``title_key``
    (every title whose normalised title equals it, on ``platform``).
    """

    kind: str  # "menu", "intro", "snap", "title", "boxart", "demo"
    url: str
    source: str = ""  # defaults to the importer id
    thumb_url: str = ""
    width: int | None = None
    height: int | None = None
    credit: str = ""
    page_url: str = ""
    rank: int = 100
    content_title: str = ""
    image_name: str = ""
    title_key: str = ""


@dataclass(slots=True)
class TriviaRecordIn:
    """A fact, note or reference article for a disc or one of its titles.

    ``kind`` "wikipedia" means ``text`` is an English Wikipedia article title
    the app fetches a summary of; "fact" and "note" hold plain text.

    Inside a DiskRecord it belongs to the disc, or to the title named by
    ``content_title``. In a record with no key, images or contents, it
    attaches to every title whose normalised title equals ``content_title``
    on the record's platform.
    """

    kind: str
    text: str
    source: str = ""
    url: str = ""
    licence: str = ""
    content_title: str = ""


@dataclass(slots=True)
class CrewRecord:
    """History and members of one crew as one source knows it.

    ``name`` is the name disks carry in disks.crew. Different crews share
    names ("Awesome" on the ST and on the Amiga), so a record also says
    where the crew released: ``platforms`` maps each platform to the number
    of releases the source credits the crew with there (0 when the source
    knows the crew released there but credits it with nothing, as Atari
    Legend, which covers only the ST, does for some crews), and the merge
    never gives the record to a disk of another platform. ``id`` is the
    source's own id for the crew; a DiskRecord of the same source lists it
    in ``crew_ids`` when the source credits the crew with that disk. A
    record without ``platforms`` describes the crew of no disk.
    """

    name: str
    source: str
    notes: str = ""
    members: list[str] = field(default_factory=list)
    founded: str = ""
    url: str = ""
    wikipedia: str = ""
    id: str = ""
    platforms: dict[str, int] = field(default_factory=dict)


@dataclass(slots=True)
class ImageRecordIn:
    name: str  # file name as the source knows it
    format: str  # "st", "msa", "adf", "dms", "stx", "ipf"
    flags: str = ""
    size: int | None = None
    crc32: str = ""
    md5: str = ""
    sha1: str = ""
    sha512: str = ""
    bad: bool = False
    virus: str = ""  # TOSEC [v Name]
    virus_damage: bool = False  # TOSEC [b virus damage]
    antivirus: str = ""  # TOSEC [m ... antivirus]


@dataclass(slots=True)
class LocationRecord:
    provider: str
    url: str
    container: str = ""
    member: str = ""
    size: int | None = None
    hash_kind: str = ""
    hash_value: str = ""
    page_url: str = ""
    priority: int = 100
    image_name: str = ""  # ties the location to an ImageRecordIn by name


@dataclass(slots=True)
class DiskRecord:
    """Everything one source says about one disk.

    ``series_key`` is the canonical series id from ``data/series.toml`` when
    the importer recognised the series, otherwise "" and the merge step files
    the disk under its own title.
    """

    source: str  # importer id, for example "tosec" or "atari-legend"
    platform: str  # a piratefinder.models.Platform value
    kind: str  # a piratefinder.models.DiskKind value
    series_key: str = ""
    number: int | None = None
    part: str = ""
    version: str = ""
    title: str = ""
    date: str = ""
    publisher: str = ""
    cracker: str = ""
    condition: str = ""
    notes: str = ""
    credits: str = ""
    menu_text: str = ""
    contents: list[ContentRecord] = field(default_factory=list)
    images: list[ImageRecordIn] = field(default_factory=list)
    locations: list[LocationRecord] = field(default_factory=list)
    links: list[tuple[str, str]] = field(default_factory=list)  # label, url
    media: list[MediaRecordIn] = field(default_factory=list)
    trivia: list[TriviaRecordIn] = field(default_factory=list)
    # "YYYY", "YYYY-MM" or "YYYY-MM-DD"; the most precise source wins in merge.
    # ``date`` keeps the text a source gives; this is the parsed release date.
    release_date: str = ""
    # The ids (CrewRecord.id) of this source's crews credited with the disk.
    crew_ids: list[str] = field(default_factory=list)
    # A keyed record that only adds to a disc other sources describe: when no
    # other record has its key, the merge drops it (its locations count as
    # unmatched) instead of making a disc of it.
    attach_only: bool = False

    @property
    def key(self) -> tuple[str, int | None, str, str] | None:
        """Identity used to merge records across sources, None if unnumbered."""
        if not self.series_key or self.number is None:
            return None
        return (
            self.series_key,
            self.number,
            normalise_part(self.part),
            normalise_version(self.version),
        )


# The terms of a site that states no licence for its pages: the builder
# takes facts from them for reference and credits the site.
NO_LICENCE_STATED = "No licence stated; used under the site's terms, credit given"


@dataclass(frozen=True, slots=True)
class SourceInfo:
    """A source as the ``sources`` table lists it.

    ``licence`` is the licence of the source's data, or, where it states
    none, the terms it is used under; every source has one.
    """

    id: str
    name: str
    url: str
    licence: str = ""


# Sites that host downloads other sources name but that are no source of
# their own: the catalogue's sources table lists them, so the application can
# name them as providers, whenever a location uses them.
SCENE_ORG = SourceInfo(
    id="scene-org",
    name="scene.org",
    url="https://www.scene.org/",
    licence=NO_LICENCE_STATED,
)
FUJIOLOGY = SourceInfo(
    id="fujiology",
    name="Fujiology",
    url="https://fujiology.org/",
    licence=NO_LICENCE_STATED,
)
HOSTS = {host.id: host for host in (SCENE_ORG, FUJIOLOGY)}

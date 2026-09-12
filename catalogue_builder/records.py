"""Intermediate records every source importer produces.

Importers know nothing about the catalogue database. Each one turns its source
into these records; ``catalogue_builder.merge`` joins records from different
sources that describe the same disk (by series, number, part and version) and
writes the catalogue.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

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


@dataclass(frozen=True, slots=True)
class SourceInfo:
    id: str
    name: str
    url: str
    licence: str = ""

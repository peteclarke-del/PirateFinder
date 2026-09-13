"""The user's corrections to a disc's details, kept across catalogue updates.

The user may correct a disc's label, catalogue name, crew, release date,
publisher, cracker and notes, and the name of each title on it. Corrections
live in the user database, never in the catalogue, and the search results
and the details pane show them in place of the catalogue's values.

The catalogue numbers its discs and titles afresh with every build, so a
correction is not kept by those numbers alone. Each corrected disc is stored
with what identifies it in any build: the series, number, part and version
of a numbered disc, or the checksums of the dumps of any other disc. When
corrections are read against a catalogue they were not yet matched to, every
corrected disc is looked up in it again (``Corrections.relink``). A disc the
catalogue no longer has keeps its corrections, unused, in case a later
catalogue has it again. A title is known by its name in the catalogue and
its place among the titles of that name on the disc.
"""

from __future__ import annotations

import datetime
import re
import threading
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Any

from ..models import Content, Disk, ImageRecord
from .userdb import CorrectedDisc, UserDatabase

# The disc fields the user may correct, in the order the Edit Details dialog shows them.
EDITABLE_FIELDS = ("label", "title", "crew", "date", "publisher", "cracker", "notes")
# Fields that may not be left empty: every disc and title needs a name.
REQUIRED_FIELDS = frozenset({"label"})
DATE_FORMAT = (
    "Enter the date as YYYY, YYYY-MM or YYYY-MM-DD, for example 1990, 1990-06 or 1990-06-21."
)
_DATE = re.compile(r"(\d{4})(?:-(\d{2})(?:-(\d{2}))?)?")

TitleKey = tuple[str, int]  # (the title as the catalogue lists it, which one of that name)


class CorrectionError(ValueError):
    """A value the user entered cannot be stored; the message is for the user."""


@dataclass(frozen=True, slots=True)
class Correction:
    """What the user changed on one disc."""

    fields: Mapping[str, str] = field(default_factory=dict)  # EDITABLE_FIELDS name -> value
    titles: Mapping[TitleKey, str] = field(default_factory=dict)

    def __bool__(self) -> bool:
        return bool(self.fields or self.titles)


def parse_date(text: str) -> tuple[int | None, int | None, int | None]:
    """(year, month, day) from "YYYY", "YYYY-MM" or "YYYY-MM-DD"; all None for "".

    Raises CorrectionError with a sentence for the user when the text is not
    such a date or names a day the calendar does not have.
    """
    text = text.strip()
    if not text:
        return None, None, None
    found = _DATE.fullmatch(text)
    if found is None:
        raise CorrectionError(DATE_FORMAT)
    year = int(found[1])
    month = int(found[2]) if found[2] else None
    day = int(found[3]) if found[3] else None
    try:
        datetime.date(year, 1 if month is None else month, 1 if day is None else day)
    except ValueError as error:  # month 13, 30 February, year 0
        raise CorrectionError(f"{text} is not a date in the calendar.") from error
    return year, month, day


def date_text(disk: Disk) -> str:
    """A disc's release date as the dialog shows it: "1990", "1990-06", "1990-06-21" or ""."""
    if not disk.year:
        return ""
    text = f"{disk.year:04d}"
    if disk.month:
        text += f"-{disk.month:02d}"
        if disk.day:
            text += f"-{disk.day:02d}"
    return text


def field_value(disk: Disk, name: str) -> str:
    """The value of one editable field of a disc, as the dialog shows it."""
    return date_text(disk) if name == "date" else str(getattr(disk, name))


def title_keys(contents: Sequence[Content]) -> list[TitleKey]:
    """The key of each title, in the order given: its name and which one of that name it is."""
    seen: dict[str, int] = {}
    keys: list[TitleKey] = []
    for content in contents:
        seen[content.title] = seen.get(content.title, 0) + 1
        keys.append((content.title, seen[content.title]))
    return keys


def apply_disk(disk: Disk, correction: Correction | None) -> Disk:
    """``disk`` with the corrected fields in place of the catalogue's."""
    if not correction or not correction.fields:
        return disk
    changes: dict[str, Any] = {
        name: value
        for name, value in correction.fields.items()
        if name in EDITABLE_FIELDS and name != "date"
    }
    if "date" in correction.fields:
        text = correction.fields["date"]
        try:
            year, month, day = parse_date(text)
        except CorrectionError:  # stored by an older version or by hand; the catalogue wins
            year, month, day = disk.year, disk.month, disk.day
            text = disk.date
        changes.update(date=text, year=year, month=month, day=day)
    return replace(disk, **changes)


def apply_contents(
    contents: Sequence[Content], correction: Correction | None
) -> tuple[Content, ...]:
    """``contents`` with the corrected titles in place of the catalogue's, in the same order."""
    if not correction or not correction.titles:
        return tuple(contents)
    return tuple(
        replace(content, title=correction.titles[key]) if key in correction.titles else content
        for content, key in zip(contents, title_keys(contents), strict=True)
    )


def corrected_titles(
    contents: Sequence[Content], correction: Correction | None
) -> dict[int, tuple[str, str]]:
    """Content id -> (catalogue title, corrected title) for every corrected title."""
    if not correction or not correction.titles:
        return {}
    return {
        content.id: (content.title, correction.titles[key])
        for content, key in zip(contents, title_keys(contents), strict=True)
        if key in correction.titles
    }


def correction_from_form(
    disk: Disk,
    contents: Sequence[Content],
    values: Mapping[str, str],
    titles: Mapping[int, str] | None = None,
) -> Correction:
    """What differs from the catalogue in the Edit Details form, as a Correction.

    ``disk`` and ``contents`` are the catalogue's, uncorrected. ``values``
    holds editable fields by name and ``titles`` title names by content id;
    a field or title left out, or equal to the catalogue's value, is not a
    correction. Raises CorrectionError for a date that is not YYYY, YYYY-MM
    or YYYY-MM-DD, and for an empty label or title.
    """
    fields: dict[str, str] = {}
    for name in EDITABLE_FIELDS:
        if name not in values:
            continue
        value = values[name] if name == "notes" else " ".join(values[name].split())
        if name in REQUIRED_FIELDS and not value:
            raise CorrectionError("A disc needs a label.")
        if name == "date":
            year, month, day = parse_date(value)
            value = date_text(replace(disk, year=year, month=month, day=day))
        if value.strip() != field_value(disk, name).strip():
            fields[name] = value
    changed: dict[TitleKey, str] = {}
    by_id = dict(zip((content.id for content in contents), title_keys(contents), strict=True))
    for content in contents:
        if titles is None or content.id not in titles:
            continue
        value = " ".join(titles[content.id].split())
        if not value:
            raise CorrectionError(f"The title {content.title} needs a name.")
        if value != content.title:
            changed[by_id[content.id]] = value
    return Correction(fields, changed)


def identify(disk: Disk, images: Iterable[ImageRecord]) -> CorrectedDisc:
    """How a corrected disc is found again in another catalogue."""
    hashes: list[dict[str, Any]] = []
    for record in sorted(images, key=lambda item: (item.bad, item.rank, item.id)):
        for kind in ("md5", "sha1", "sha512"):
            value = getattr(record, kind)
            if value:
                hashes.append({kind: value.lower()})
        if record.crc32 and record.size:
            hashes.append({"crc32": record.crc32.lower(), "size": record.size})
    return CorrectedDisc(
        series_id=disk.series_id,
        number=disk.number,
        part=disk.part,
        version=disk.version,
        platform=str(disk.platform),
        title=disk.title or disk.label,
        hashes=tuple(hashes),
        disk_id=disk.id,
    )


def catalogue_stamp(catalogue: Any) -> str:
    """What names one catalogue build: its build time, else its path."""
    return str(getattr(catalogue, "built_at", "") or getattr(catalogue, "path", "") or "")


class Corrections:
    """Reads and stores corrections for the catalogue in use."""

    def __init__(self, userdb: UserDatabase) -> None:
        self.userdb = userdb
        self._lock = threading.Lock()
        self._linked = ""  # the catalogue stamp corrections were last matched to

    def load(self, catalogue: Any, disk_ids: Iterable[int]) -> dict[int, Correction]:
        """The corrections of these catalogue discs; discs without any are left out."""
        stamp = self.relink(catalogue)
        return {
            disk_id: Correction(fields, titles)
            for disk_id, (fields, titles) in self.userdb.corrections(disk_ids, stamp).items()
        }

    def every(self, catalogue: Any) -> dict[int, Correction]:
        """The corrections of every disc ``catalogue`` has, by its disc ids."""
        stamp = self.relink(catalogue)
        return {
            disk_id: Correction(fields, titles)
            for disk_id, (fields, titles) in self.userdb.every_correction(stamp).items()
        }

    def save(self, catalogue: Any, disk_id: int, correction: Correction) -> None:
        """Store ``correction`` for a disc, replacing its earlier one; an empty one reverts."""
        stamp = self.relink(catalogue)
        if not correction:
            self.userdb.delete_corrections(disk_id, stamp)
            return
        disk = catalogue.disk(disk_id)
        if disk is None:
            raise LookupError(f"Disk {disk_id} is not in the catalogue.")
        disc = replace(identify(disk, catalogue.images(disk_id)), catalogue=stamp)
        self.userdb.store_corrections(disc, correction.fields, correction.titles)

    def revert(self, catalogue: Any, disk_id: int) -> None:
        """Forget every correction of a disc, so the catalogue's values show again."""
        self.userdb.delete_corrections(disk_id, self.relink(catalogue))

    def relink(self, catalogue: Any) -> str:
        """Match every corrected disc to ``catalogue`` when not yet done; return its stamp."""
        stamp = catalogue_stamp(catalogue)
        with self._lock:
            if self._linked == stamp:
                return stamp
            changes = [
                (disc.id, find_disc(catalogue, disc), stamp)
                for disc in self.userdb.corrected_discs()
                if disc.catalogue != stamp
            ]
            if changes:
                self.userdb.relink_corrected_discs(changes)
            self._linked = stamp
        return stamp


def find_disc(catalogue: Any, disc: CorrectedDisc) -> int | None:
    """The id of a corrected disc in ``catalogue``, or None when it has no such disc.

    A numbered disc is found by its series, number, part and version. Any
    other disc, and a numbered one the catalogue no longer numbers that way,
    is the disc of the same platform that owns one of its dumps.
    """
    if disc.series_id:
        # The catalogue reader has no lookup by series and number, so ask it directly.
        rows = catalogue.query(
            "SELECT id FROM disks WHERE series_id = ? AND number IS ? AND part = ? AND version = ?",
            (disc.series_id, disc.number, disc.part, disc.version),
        )
        if rows:
            return int(rows[0][0])
    for hashes in disc.hashes:
        record = catalogue.match_image(**hashes)
        if record is None:
            continue
        disk = catalogue.disk(record.disk_id)
        if disk is not None and str(disk.platform) == disc.platform:
            return disk.id
    return None

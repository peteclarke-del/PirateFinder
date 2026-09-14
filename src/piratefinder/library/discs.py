"""A catalogue disc, described so that any catalogue build can find it again.

The catalogue numbers its discs afresh with every build. What the user keeps
about a disc (corrections, and the files that stay with it: a copy cleaned of
a virus, a download no checksum could check) stores it as a ``DiscIdentity``,
made by ``identify``: the series, number, part and version of a numbered
disc, and the checksums of its dumps. ``find_disc`` looks it up in another
build, and ``catalogue_stamp`` names the build a disk id belongs to.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from ..models import Disk, ImageRecord
from .userdb import DiscIdentity


def identify(disk: Disk, images: Iterable[ImageRecord]) -> DiscIdentity:
    """How a disc is found again in another catalogue."""
    hashes: list[dict[str, Any]] = []
    for record in sorted(images, key=lambda item: (item.bad, item.rank, item.id)):
        for kind in ("md5", "sha1", "sha512"):
            value = getattr(record, kind)
            if value:
                hashes.append({kind: value.lower()})
        if record.crc32 and record.size:
            hashes.append({"crc32": record.crc32.lower(), "size": record.size})
    return DiscIdentity(
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


def find_disc(catalogue: Any, disc: DiscIdentity) -> int | None:
    """The id of ``disc`` in ``catalogue``, or None when it has no such disc.

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


def identify_id(catalogue: Any, disk_id: int) -> DiscIdentity | None:
    """``identify`` for a disk id of ``catalogue``, None when it has no such disc."""
    disk = catalogue.disk(disk_id)
    return identify(disk, catalogue.images(disk_id)) if disk is not None else None

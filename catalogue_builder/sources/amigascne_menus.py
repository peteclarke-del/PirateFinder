"""Menu scroller texts of Amiga pack disks from the amigascne archive.

Like ``amigascne``, this importer reads the scene.org mirror of the archive,
not ftp.amigascne.org, which forbids automated access.

Besides the disks themselves the archive keeps the text of their menus and
scrollers, ripped to plain files such as
``Scrollers/S-Groupstext/Skid_Row/Skid_Row-Compact031-menu.txt``. The menu
text names everything on the disk, so it is stored as the disk's searchable
menu text.

The texts are found in the same daily index the ``amigascne`` importer reads.
A text is fetched only when it can be tied to a disk: its file name either
matches a series rule (the rules of ``amigascne`` are reused, so
``Skid_Row-Compact031`` is Skid Row Compact 31; such a record only joins a
disc another source describes) or equals the name of an ADF
pack disk in the archive, whose CRC32 then carries the text to that disk.
The archive holds some 3,600 menu texts, of which about 2,000 can be tied to
a disk. Each is one request, 1.5 seconds apart, and is cached for 30 days, so
a build with a warm cache (the weekly catalogue build keeps its cache) asks
for none and a cold one takes about 50 minutes. Set
``PIRATEFINDER_AMIGASCNE_MENUS_LIMIT`` to fetch only the first few for a
trial run.
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterator

from ..context import BuildContext
from ..records import DiskRecord, ImageRecordIn, SourceInfo
from ..series import normalise
from . import amigascne

SCROLLER_ROOT = "Scrollers/"
INFO = SourceInfo(
    id="amigascne-menus",
    name="amigascne menu texts (scene.org mirror)",
    url=amigascne.BASE + SCROLLER_ROOT,
    licence=amigascne.INFO.licence,
)
DEFAULT_ENABLED = True

MENU_SUFFIX = "-menu.txt"
LIMIT_VARIABLE = "PIRATEFINDER_AMIGASCNE_MENUS_LIMIT"
PLATFORM = "amiga"

_ANSI = re.compile(r"(?:\x1b\[|\x9b)[0-9;]*[ -/]*[@-~]")
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")


def clean_text(data: bytes) -> str:
    """Menu text as readable lines: Latin-1, no colour codes or control codes."""
    text = _ANSI.sub("", data.decode("latin-1")).replace("\r\n", "\n").replace("\r", "\n")
    lines = [_CONTROL.sub("", line).rstrip() for line in text.split("\n")]
    while lines and not lines[-1]:
        lines.pop()
    while lines and not lines[0]:
        lines.pop(0)
    return "\n".join(lines)


def menu_files(entries: list[amigascne.IndexEntry]) -> Iterator[tuple[str, str, str]]:
    """(path, group folder, pack name) for every menu text in the index."""
    for entry in entries:
        if entry.path.startswith(SCROLLER_ROOT) and entry.path.lower().endswith(MENU_SUFFIX):
            folder, _, name = entry.path.rpartition("/")
            yield entry.path, folder.rsplit("/", 1)[-1], name[: -len(MENU_SUFFIX)]


def _limit() -> int | None:
    value = os.environ.get(LIMIT_VARIABLE, "").strip()
    return int(value) if value.isdigit() else None


def plan(
    ctx: BuildContext, entries: list[amigascne.IndexEntry]
) -> Iterator[tuple[str, DiskRecord]]:
    """(menu text path, record waiting for that text) for every text tied to a disk."""
    adf_by_stem: dict[str, list[amigascne.PackFile]] = {}
    for entry in entries:
        pack = amigascne.pack_file(entry)
        if pack is not None and pack.suffix == ".adf" and not pack.flags:
            adf_by_stem.setdefault(normalise(pack.stem), []).append(pack)
    for path, folder, stem in menu_files(entries):
        found = amigascne.identify(ctx.series, folder, stem)
        if found is not None:
            definition = ctx.series.get(found.series_id)
            yield (
                path,
                DiskRecord(
                    source=INFO.id,
                    platform=PLATFORM,
                    kind=definition.kind if definition else "pack",
                    series_key=found.series_id,
                    number=found.number,
                    part=found.part,
                    version=found.version,
                    # A menu text alone is no disc to download or check.
                    attach_only=True,
                ),
            )
            continue
        packs = adf_by_stem.get(normalise(stem), [])
        if not packs:
            continue
        yield (
            path,
            DiskRecord(
                source=INFO.id,
                platform=PLATFORM,
                kind="pack",
                title=amigascne.title_for(folder, stem),
                images=[
                    ImageRecordIn(
                        name=pack.name, format="adf", size=pack.entry.size, crc32=pack.entry.crc32
                    )
                    for pack in packs
                ],
            ),
        )


def collect(ctx: BuildContext) -> Iterator[DiskRecord]:
    entries = list(amigascne.parse_index(amigascne.fetch_index(ctx)))
    limit = _limit()
    fetched = failed = 0
    for path, record in plan(ctx, entries):
        if limit is not None and fetched >= limit:
            break
        try:
            data = ctx.fetch(
                amigascne.file_url(path),
                max_age_days=amigascne.MAX_AGE_DAYS,
            ).read_bytes()
        except (OSError, RuntimeError) as error:
            failed += 1
            ctx.log(f"amigascne-menus: could not fetch {path}: {error}")
            continue
        fetched += 1
        record.menu_text = clean_text(data)
        if record.menu_text:
            yield record
    ctx.log(f"amigascne-menus: {fetched} menu texts, {failed} failed")

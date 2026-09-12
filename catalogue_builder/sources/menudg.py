"""The 8bitchip "Atari ST games on menu disks" index.

Bruno's list (last revised in 2004, now kept at atari.8bitchip.info) is a
reverse index: one row per game and disk, with the disk written as a crew
prefix and a number ("AU061", "DB031B", "PP013V2", "SD006"). Following rows
with an empty name belong to the game above. This importer turns the index
around into the contents of each disk. It is the only source for what is on
the Vectronix disks, and it covers SuperGAU, Fuzion and the doc disks as well.

The crew prefixes are declared as match rules in
``data/series/match-menudg.toml``; a prefix without a rule is reported and
its rows are left out.
"""

from __future__ import annotations

import html
import re
from collections.abc import Iterator

from ..context import BuildContext
from ..records import ContentRecord, DiskRecord, SourceInfo, normalise_part, normalise_version

INFO = SourceInfo(
    id="8bitchip",
    name="8bitchip Atari ST games on menu disks",
    url="https://atari.8bitchip.info/MenuDG.html",
    licence="",
)
CONTENT_PRIORITY = 60

PAGE = "https://atari.8bitchip.info/MenuDG.html"
PLATFORM = "atari-st"

_ROW = re.compile(r"<tr>(?P<row>.*?)(?=<tr>|</tbody>|</table>)", re.IGNORECASE | re.DOTALL)
_CELL = re.compile(r"<td>(?P<cell>.*?)(?=<td>|</tr>|$)", re.IGNORECASE | re.DOTALL)
_MARKUP = re.compile(r"<[^>]+>")
_DISK_ID = re.compile(r"^[A-Z]{2}\d+[A-Z]?(?:V\d+)?$")


def _text(fragment: str) -> str:
    return " ".join(html.unescape(_MARKUP.sub(" ", fragment)).split())


def content_kind(kind_text: str) -> str:
    """ContentKind for the list's "Type" column ("DOC Instructions", "Demo, Intro, etc.")."""
    lowered = kind_text.lower()
    if lowered.startswith("doc"):
        return "doc"
    if lowered.startswith(("demo", "intro")):
        return "demo"
    if lowered.startswith(("application", "editor", "utility")):
        return "utility"
    if lowered.startswith(("patch", "trainer")):
        return "other"
    return "game"


def parse_rows(page: str) -> Iterator[tuple[str, str, str, str]]:
    """(game, requirement, disk id, type) for every row of the games table.

    Rows with an empty name repeat the game above. A row with an empty type
    inherits the game's first type, so a game first listed as a doc stays a
    doc on its other disks.
    """
    game = kind = ""
    for found in _ROW.finditer(page):
        cells = [_text(cell.group("cell")) for cell in _CELL.finditer(found.group("row"))]
        if len(cells) != 5:
            continue
        name, requirement, _sequence, disk, kind_text = cells
        disk = disk.upper().replace(" ", "")
        if not _DISK_ID.match(disk):
            continue
        if name:
            game, kind = name, kind_text
        elif not game:
            continue
        yield game, requirement, disk, kind_text or kind


def records_from_page(ctx: BuildContext, page: str) -> Iterator[DiskRecord]:
    disks: dict[tuple[str, int, str, str], DiskRecord] = {}
    unknown: dict[str, int] = {}
    for game, requirement, disk, kind_text in parse_rows(page):
        found = ctx.series.match(INFO.id, disk, PLATFORM)
        if found is None or found.number is None:
            prefix = disk[:2]
            unknown[prefix] = unknown.get(prefix, 0) + 1
            continue
        version = normalise_version(found.version) if found.version else ""
        key = (found.series_id, found.number, normalise_part(found.part), version)
        record = disks.get(key)
        if record is None:
            series = ctx.series.get(found.series_id)
            record = disks[key] = DiskRecord(
                source=INFO.id,
                platform=PLATFORM,
                kind=series.kind if series else "menu",
                series_key=found.series_id,
                number=found.number,
                part=found.part,
                version=version,
            )
        kind = content_kind(kind_text)
        extras = [requirement] if requirement else []
        if kind == "doc":
            doc_type = re.sub(r"^docs?\s*", "", kind_text, flags=re.IGNORECASE).strip()
            if doc_type:
                extras.append(doc_type)
        if not any(content.title == game for content in record.contents):
            record.contents.append(ContentRecord(title=game, kind=kind, extra=", ".join(extras)))
    for prefix, count in sorted(unknown.items()):
        ctx.log(f"8bitchip: no series rule for disk prefix {prefix!r} ({count} rows)")
    yield from disks.values()


def collect(ctx: BuildContext) -> Iterator[DiskRecord]:
    page = ctx.fetch_text(PAGE, encoding="latin-1", min_interval=1.5, max_age_days=30)
    count = 0
    for record in records_from_page(ctx, page):
        count += 1
        yield record
    ctx.log(f"8bitchip: {count} disks")

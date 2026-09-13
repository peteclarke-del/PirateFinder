"""Screenshots, title screens and box art from the libretro-thumbnails repositories.

The libretro project keeps one GitHub repository of thumbnails per system
(github.com/libretro-thumbnails), with a folder per kind of picture. One
request per repository to the GitHub git trees API lists every file in it;
the listing is cached for 30 days. The pictures themselves are fetched by the
application from raw.githubusercontent.com when they are shown. The
repositories carry no licence file, so every picture is credited to the
project.

The repositories name pictures differently, and ``REPOSITORIES`` says how
each one is matched:

- Atari ST ("image"): a picture is named after the TOSEC name of the dump
  it shows, with the characters ``&*/:`<>?\\|"`` replaced by "_". Each
  becomes a media-only record attached by image name to the disc whose
  dump has that name; " _ " is read back as " & ", which is where TOSEC
  names use the character. Pictures not named in the TOSEC way (no date
  field) cannot match a dump and are left out.
- Amiga ("title"): pictures are named in the No-Intro way, "Rick Dangerous
  (Europe)" or "Chaos Engine, The". Each becomes a media-only record
  attached by ``title_key`` to every Amiga title with that normalised
  title, one picture of each kind per title: a name without bracketed
  fields first, then the shortest.
"""

from __future__ import annotations

import json
import re
import urllib.parse
from collections.abc import Iterable, Iterator
from dataclasses import dataclass

from piratefinder.catalogue.naming import parse_tosec_name, title_key

from ..context import BuildContext
from ..records import DiskRecord, MediaRecordIn, SourceInfo

INFO = SourceInfo(
    id="libretro-thumbnails",
    name="libretro-thumbnails",
    url="https://github.com/libretro-thumbnails",
    licence="No licence stated (images are game graphics)",
)
CONTENT_PRIORITY = 95

TREE_URL = "https://api.github.com/repos/libretro-thumbnails/{repo}/git/trees/{branch}?recursive=1"
RAW_URL = "https://raw.githubusercontent.com/libretro-thumbnails/{repo}/{branch}/{path}"
PAGE_URL = "https://github.com/libretro-thumbnails/{repo}"
BRANCH = "master"
MAX_AGE_DAYS = 30.0


@dataclass(frozen=True, slots=True)
class Repository:
    name: str
    platform: str
    match: str  # "image": by TOSEC image name, "title": by normalised title


REPOSITORIES = (
    Repository("Atari_-_ST", "atari-st", "image"),
    Repository("Commodore_-_Amiga", "amiga", "title"),
)
# Picture folders: media kind, rank and the word the credit starts with.
FOLDERS = {
    "Named_Snaps": ("snap", 40, "Snap"),
    "Named_Titles": ("title", 45, "Title screen"),
    "Named_Boxarts": ("boxart", 50, "Box art"),
}
CREDIT = "{what}: libretro-thumbnails"
# The extension TOSEC gives Atari ST dumps; merge also compares the bare name.
ST_EXTENSION = ".st"

_BRACKETED = re.compile(r"\s*[(\[].*$")
_PICTURE = re.compile(r"\.png$", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class Picture:
    folder: str
    name: str  # file name without the extension
    path: str  # path in the repository

    @property
    def kind(self) -> str:
        return FOLDERS[self.folder][0]


def pictures(tree: dict) -> list[Picture]:
    """The pictures in the wanted folders of a git trees API answer."""
    found = []
    for entry in tree.get("tree", []):
        path = entry.get("path", "")
        folder, _, file_name = path.partition("/")
        if entry.get("type") != "blob" or folder not in FOLDERS or "/" in file_name:
            continue
        if _PICTURE.search(file_name):
            found.append(Picture(folder, _PICTURE.sub("", file_name), path))
    return found


def _media(repo: Repository, picture: Picture, **attach: str) -> MediaRecordIn:
    kind, rank, what = FOLDERS[picture.folder]
    return MediaRecordIn(
        kind=kind,
        url=RAW_URL.format(repo=repo.name, branch=BRANCH, path=urllib.parse.quote(picture.path)),
        source=INFO.id,
        credit=CREDIT.format(what=what),
        page_url=PAGE_URL.format(repo=repo.name),
        rank=rank,
        **attach,
    )


def image_name(picture_name: str) -> str:
    """The TOSEC file name a picture of an Atari ST dump stands for."""
    return picture_name.replace(" _ ", " & ") + ST_EXTENSION


def by_image(repo: Repository, found: Iterable[Picture]) -> Iterator[DiskRecord]:
    """One media-only record per TOSEC name, with every kind of picture of it."""
    named: dict[str, list[Picture]] = {}
    for picture in found:
        if parse_tosec_name(picture.name).date:
            named.setdefault(picture.name, []).append(picture)
    for name in sorted(named):
        yield DiskRecord(
            source=INFO.id,
            platform=repo.platform,
            kind="single",
            media=[
                _media(repo, picture, image_name=image_name(name))
                for picture in sorted(named[name], key=lambda p: FOLDERS[p.folder][1])
            ],
        )


def _preference(picture: Picture) -> tuple[int, int, str]:
    return (1 if _BRACKETED.search(picture.name) else 0, len(picture.name), picture.name)


def by_title(repo: Repository, found: Iterable[Picture]) -> Iterator[DiskRecord]:
    """One media-only record per normalised title, one picture of each kind."""
    best: dict[str, dict[str, Picture]] = {}
    for picture in found:
        key = title_key(picture.name)
        if not key:
            continue
        kinds = best.setdefault(key, {})
        chosen = kinds.get(picture.kind)
        if chosen is None or _preference(picture) < _preference(chosen):
            kinds[picture.kind] = picture
    for key in sorted(best):
        yield DiskRecord(
            source=INFO.id,
            platform=repo.platform,
            kind="single",
            media=[
                _media(repo, picture, title_key=key)
                for picture in sorted(best[key].values(), key=lambda p: FOLDERS[p.folder][1])
            ],
        )


def records(repo: Repository, tree: dict, log=lambda message: None) -> list[DiskRecord]:
    if tree.get("truncated"):
        log(f"{INFO.id}: the {repo.name} listing is truncated; some pictures are missing")
    found = pictures(tree)
    made = list(by_image(repo, found) if repo.match == "image" else by_title(repo, found))
    counts: dict[str, int] = {}
    for record in made:
        for item in record.media:
            counts[item.kind] = counts.get(item.kind, 0) + 1
    summary = ", ".join(f"{counts.get(kind, 0)} {kind}" for kind, _r, _w in FOLDERS.values())
    target = "image names" if repo.match == "image" else "titles"
    log(f"{INFO.id}: {repo.name}: {len(found)} pictures, {summary} for {len(made)} {target}")
    return made


def collect(ctx: BuildContext) -> Iterator[DiskRecord]:
    for repo in REPOSITORIES:
        url = TREE_URL.format(repo=repo.name, branch=BRANCH)
        path = ctx.fetch(url, name=f"tree-{repo.name}.json", max_age_days=MAX_AGE_DAYS)
        with path.open("rb") as handle:
            yield from records(repo, json.load(handle), ctx.log)

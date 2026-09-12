"""The facade the interface talks to: search, disk detail and image sources.

``Finder`` joins the catalogue, the library and the settings. It answers a
search with catalogue disks and unmatched local files, says for each disk
whether an image is local, online or missing, and lists where the writer can
take an image from, best first.
"""

from __future__ import annotations

import contextlib
import sqlite3
import urllib.parse
from collections.abc import Callable, Iterable, Sequence
from dataclasses import fields, replace
from pathlib import Path
from typing import Any

from .archive_layout import DEFAULT_FOLDERS, archive_folders
from .catalogue.naming import display_title
from .models import (
    Availability,
    Content,
    Disk,
    DiskDetail,
    DiskKind,
    ImageRecord,
    ImageSource,
    LocalFile,
    Platform,
    QueueItem,
    SearchFilters,
    SearchResult,
)
from .settings import Settings

# Formats gw writes as they are, after at most a lossless decode.
WRITABLE_FORMATS = frozenset({"adf", "st", "msa", "dms", "adz"})
FORMAT_PLATFORMS = {
    "adf": Platform.AMIGA,
    "adf-ext": Platform.AMIGA,
    "adz": Platform.AMIGA,
    "dms": Platform.AMIGA,
    "st": Platform.ATARI_ST,
    "msa": Platform.ATARI_ST,
    "stx": Platform.ATARI_ST,
}
RESULT_LIMIT = 500
SUMMARY_TITLES = 6
_AVAILABILITY_ORDER = {Availability.LOCAL: 0, Availability.ONLINE: 1, Availability.MISSING: 2}
# Text fields of a disk the user may correct.
_CORRECTABLE = frozenset(item.name for item in fields(Disk) if item.type in ("str", str))

SearchFunction = Callable[..., Sequence[Any]]


def _default_search() -> SearchFunction:
    from .catalogue.search import search_catalogue

    return search_catalogue


def contents_summary(contents: Sequence[Content], matched: Iterable[str] = ()) -> str:
    """Up to six titles in menu order, matching ones first, then " and N more".

    A menu often lists a game again for its docs or cheats; the summary names
    each title once, with a trailing article moved to the front for reading.
    ``matched`` must already be in that display form, as ``Finder.search``
    gives it, so the interface can find and embolden each one.
    """
    ordered = sorted(contents, key=lambda item: item.position)
    titles = [display_title(title) for title in _distinct_casefold(c.title for c in ordered)]
    if len(titles) <= SUMMARY_TITLES:
        return ", ".join(titles)
    wanted = {title.casefold() for title in matched}
    chosen = [index for index, title in enumerate(titles) if title.casefold() in wanted]
    chosen = chosen[:SUMMARY_TITLES]
    for index in range(len(titles)):
        if len(chosen) >= SUMMARY_TITLES:
            break
        if index not in chosen:
            chosen.append(index)
    shown = [titles[index] for index in sorted(chosen)]
    return f"{', '.join(shown)} and {len(titles) - len(shown)} more"


def result_summary(disk: Disk, contents: Sequence[Content], matched: Iterable[str] = ()) -> str:
    """The second line of a result row.

    A menu disk lists its contents. A single-game disk would only repeat its
    own name, so it says who published and who cracked it instead.
    """
    if disk.kind is DiskKind.SINGLE and len(_distinct_casefold(c.title for c in contents)) <= 1:
        credits = [disk.publisher, f"cracked by {disk.cracker}" if disk.cracker else ""]
        text = ", ".join(part for part in credits if part)
        if text:
            return text
    return contents_summary(contents, matched)


def _distinct_casefold(titles: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    distinct = []
    for title in titles:
        if title.casefold() not in seen:
            seen.add(title.casefold())
            distinct.append(title)
    return distinct


def local_label(local: LocalFile) -> str:
    """How a local file is named in a session summary."""
    return f"{local.path}: {local.member}" if local.member else local.path


def _format_rank(image_format: str) -> int:
    return 0 if image_format.lower() in WRITABLE_FORMATS else 1


def local_platform(local: LocalFile) -> Platform | None:
    """The platform an image format belongs to, when the format says."""
    return FORMAT_PLATFORMS.get(local.format.lower())


class Finder:
    """Search, detail and source resolution over the catalogue and the library."""

    def __init__(
        self,
        catalogue: Any,
        library: Any,
        settings: Settings,
        *,
        search: SearchFunction | None = None,
    ) -> None:
        self.catalogue = catalogue
        self.library = library
        self.settings = settings
        self._search = search
        self._providers: list[str] | None = None
        self._groups: dict[str | None, str] | None = None

    def set_catalogue(self, catalogue: Any) -> None:
        """Switch to a newer catalogue and hand it to the library as well."""
        self.catalogue = catalogue
        self._providers = None
        self._groups = None
        set_catalogue = getattr(self.library, "set_catalogue", None)
        if set_catalogue is not None:
            set_catalogue(catalogue)

    # Providers ---------------------------------------------------------------

    def providers(self) -> list[str]:
        """Every provider id that has a location in the catalogue, sorted."""
        if self._providers is None:
            method = getattr(self.catalogue, "providers", None)
            query = getattr(self.catalogue, "query", None)
            if callable(method):
                found = [str(provider) for provider in method()]
            elif callable(query):
                rows = query("SELECT DISTINCT provider FROM locations")
                found = [str(provider) for (provider,) in rows if provider]
            else:
                found = _providers_from_file(getattr(self.catalogue, "path", None))
            self._providers = sorted(dict.fromkeys(found))
        return list(self._providers)

    def provider_names(self) -> dict[str, str]:
        """Provider id -> display name, from the catalogue's sources where it has one."""
        names: dict[str, str] = {}
        with contextlib.suppress(Exception):
            for source in self.catalogue.sources():
                if source.get("id") and source.get("name"):
                    names[str(source["id"])] = str(source["name"])
        return {provider: names.get(provider, provider) for provider in self.providers()}

    def enabled_providers(self) -> list[str]:
        """Providers the settings allow; empty when online use is off."""
        return self.settings.enabled_providers(self.providers())

    # Search ------------------------------------------------------------------

    def search(self, text: str, filters: SearchFilters) -> list[SearchResult]:
        """Catalogue disks and unmatched local files answering ``text``, best first."""
        search = self._search or _default_search()
        limit = RESULT_LIMIT * 4 if filters.available_only else RESULT_LIMIT
        hits = list(search(self.catalogue, text, filters, limit=limit))
        ids = [hit.disk_id for hit in hits]
        disks = self._corrected(self.catalogue.disks(ids))
        availability = self.library.availability(ids, self.enabled_providers())
        results: list[SearchResult] = []
        for hit in hits:
            disk = disks.get(hit.disk_id)
            if disk is None:
                continue
            state = availability.get(hit.disk_id, Availability.MISSING)
            if filters.available_only and state is Availability.MISSING:
                continue
            matched = tuple(display_title(title) for title in hit.matched)
            contents = self.catalogue.contents(hit.disk_id)
            summary = result_summary(disk, contents, matched)
            if summary != contents_summary(contents, matched):
                # A credits line: the title is already the row's label, and
                # the interface would otherwise put it back in front.
                matched = ()
            results.append(
                SearchResult(
                    availability=state,
                    disk=disk,
                    matched=matched,
                    summary=summary,
                    score=float(hit.score),
                )
            )
        if text.strip() and not filters.kinds:
            for local in self.library.search_unmatched(text):
                platform = local_platform(local)
                if filters.platform is not None and platform not in (None, filters.platform):
                    continue
                results.append(
                    SearchResult(
                        availability=Availability.LOCAL,
                        local=local,
                        summary=", ".join(local.listing[:SUMMARY_TITLES]),
                        score=0.0,
                    )
                )
        # Scores that round to the same whole number are ties, as in the catalogue
        # search's own order; the sort is stable, so its order is kept within them.
        results.sort(
            key=lambda result: (-round(result.score), _AVAILABILITY_ORDER[result.availability])
        )
        return results[:RESULT_LIMIT]

    def detail(self, disk_id: int) -> DiskDetail:
        """Everything known about one disk; raises LookupError when it is not catalogued."""
        disk = self.catalogue.disk(disk_id)
        if disk is None:
            raise LookupError(f"Disk {disk_id} is not in the catalogue.")
        disk = self._corrected({disk_id: disk})[disk_id]
        availability = self.library.availability([disk_id], self.enabled_providers())
        return DiskDetail(
            disk=disk,
            contents=tuple(self.catalogue.contents(disk_id)),
            images=tuple(self.catalogue.images(disk_id)),
            locations=tuple(self.catalogue.locations(disk_id)),
            links=tuple(self.catalogue.links(disk_id)),
            local_files=tuple(self.library.files_for_disk(disk_id)),
            availability=availability.get(disk_id, Availability.MISSING),
        )

    # Sources -----------------------------------------------------------------

    def sources_for(self, item: QueueItem) -> list[ImageSource]:
        """Where to take an image for ``item`` from: local files first, then downloads."""
        sources: list[ImageSource] = []
        seen: set[tuple[str, str]] = set()
        images: dict[int, ImageRecord | None] = {}

        def image(image_id: int | None) -> ImageRecord | None:
            if image_id is None:
                return None
            if image_id not in images:
                images[image_id] = self.catalogue.image(image_id)
            return images[image_id]

        disk = self.catalogue.disk(item.disk_id) if item.disk_id is not None else None
        platform = item.platform or (disk.platform if disk is not None else None)
        if item.local is not None:
            seen.add((item.local.path, item.local.member))
            sources.append(
                ImageSource(
                    label=local_label(item.local),
                    platform=platform or local_platform(item.local),
                    local=item.local,
                    image=image(item.local.image_id),
                )
            )
        if item.disk_id is None:
            return sources
        if item.image_id is not None:
            files = self.library.files_for_image(item.image_id)
        else:
            files = self.library.files_for_disk(item.disk_id)

        def local_key(local: LocalFile) -> tuple:
            record = image(local.image_id)
            # A writable alternate beats a better-ranked dump gw cannot take.
            return (
                record.bad if record else False,
                _format_rank(local.format),
                record.rank if record else 0,
                local.path,
                local.member,
            )

        for local in sorted(files, key=local_key):
            if (local.path, local.member) in seen:
                continue
            seen.add((local.path, local.member))
            sources.append(
                ImageSource(
                    label=local_label(local),
                    platform=platform,
                    local=local,
                    image=image(local.image_id),
                )
            )
        if not self.settings.online_enabled:
            return sources
        locations = [
            location
            for location in self.catalogue.locations(item.disk_id)
            if self.settings.provider_enabled(location.provider)
            and (item.image_id is None or location.image_id == item.image_id)
        ]

        def location_key(location: Any) -> tuple:
            record = image(location.image_id)
            # Bad dumps and formats gw cannot take come last whoever hosts them.
            return (
                record.bad if record else False,
                _format_rank(record.format) if record else 0,
                location.priority,
                record.rank if record else 0,
                location.id,
            )

        names = self.provider_names() if locations else {}
        for location in sorted(locations, key=location_key):
            host = urllib.parse.urlsplit(location.url).netloc
            name = names.get(location.provider, location.provider)
            sources.append(
                ImageSource(
                    label=f"{name} ({host})" if host else name,
                    platform=platform,
                    location=location,
                    image=image(location.image_id),
                )
            )
        return sources

    def archive_folders(self, disk_id: int) -> tuple[str, str]:
        """The type and crew folders a download of this disk is filed under."""
        disk = self.catalogue.disk(disk_id)
        if disk is None:
            return DEFAULT_FOLDERS
        return archive_folders(
            disk, self.catalogue.contents(disk_id), self._series_groups().get(disk.series_id, "")
        )

    def _series_groups(self) -> dict[str | None, str]:
        if self._groups is None:
            groups: dict[str | None, str] = {}
            with contextlib.suppress(Exception):
                groups = {series.id: series.group for series in self.catalogue.series()}
            self._groups = groups
        return self._groups

    # Counts ------------------------------------------------------------------

    def stats(self) -> dict[str, int]:
        """Catalogue counts prefixed "catalogue_" and library counts prefixed "library_"."""
        result = {f"catalogue_{key}": value for key, value in self.catalogue.stats().items()}
        result.update({f"library_{key}": value for key, value in self.library.stats().items()})
        return result

    def _corrected(self, disks: dict[int, Disk]) -> dict[int, Disk]:
        userdb = getattr(self.library, "userdb", None)
        if userdb is None or not disks:
            return disks
        try:
            corrections = userdb.corrections(disks.keys())
        except sqlite3.Error:
            return disks
        for disk_id, changes in corrections.items():
            allowed = {key: value for key, value in changes.items() if key in _CORRECTABLE}
            if allowed and disk_id in disks:
                disks[disk_id] = replace(disks[disk_id], **allowed)
        return disks


def _providers_from_file(path: Path | str | None) -> list[str]:
    """Distinct location providers read straight from a catalogue file."""
    if not path:
        return []
    try:
        connection = sqlite3.connect(f"{Path(path).resolve().as_uri()}?mode=ro", uri=True)
    except sqlite3.Error:
        return []
    try:
        rows = connection.execute("SELECT DISTINCT provider FROM locations").fetchall()
    except sqlite3.Error:
        return []
    finally:
        connection.close()
    return [str(provider) for (provider,) in rows if provider]

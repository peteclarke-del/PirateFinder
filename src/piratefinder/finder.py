"""The facade the interface talks to: search, disk detail and image sources.

``Finder`` joins the catalogue, the library and the settings. It answers a
search a page at a time (``search_page``), says for each disc whether an
image is local, online or missing and whether the dump the writer would use
carries a virus, fills the details pane (contents, dumps, pictures, facts,
crew history and the boot block report), and lists where the writer can take
an image from, best first: dumps without a virus before flagged ones.

Pictures and Wikipedia summaries are fetched on demand through
``online.media``, and never when the user has switched them off.

The user's corrections (``library.corrections``) replace the catalogue's
values wherever a disc or title is shown: in each result row, the details
pane, the download folder and the summaries looked up for it. The search
matches, filters and sorts by them too: ``search_overrides`` hands them to
``catalogue.search`` as ``Overrides``.
"""

from __future__ import annotations

import contextlib
import sqlite3
import threading
import urllib.parse
from collections.abc import Callable, Iterable, Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any

from .archive_layout import DEFAULT_FOLDERS, archive_folders
from .catalogue.naming import display_title
from .catalogue.search import Overrides
from .images.archives import image_file_name
from .images.inspect import inspect_bytes, local_platform
from .library.corrections import (
    EDITABLE_FIELDS,
    Correction,
    Corrections,
    apply_contents,
    apply_disk,
    corrected_titles,
    correction_from_form,
)
from .models import (
    Availability,
    Content,
    ContentKind,
    CrewInfo,
    Disk,
    DiskDetail,
    DiskKind,
    Facets,
    ImageRecord,
    ImageSource,
    LocalFile,
    Location,
    MediaItem,
    Platform,
    Query,
    QueueItem,
    ResultMode,
    ResultPage,
    ResultRow,
    TriviaItem,
    VirusReport,
    VirusStatus,
)
from .settings import Settings

# Formats gw writes as they are, after at most a lossless decode.
WRITABLE_FORMATS = frozenset({"adf", "st", "msa", "dms", "adz"})
SUMMARY_TITLES = 6

# Catalogue trivia rows of this kind name a Wikipedia article rather than
# hold text; their summaries are fetched by Finder.summaries.
WIKIPEDIA_TRIVIA = "wikipedia"

PageFunction = Callable[..., Any]
FacetsFunction = Callable[..., Facets]


def _default_search_page() -> PageFunction:
    from .catalogue.search import search_page

    return search_page


def _default_facets() -> FacetsFunction:
    from .catalogue.search import facets

    return facets


def contents_summary(contents: Sequence[Content], matched: Iterable[str] = ()) -> str:
    """Up to six titles in menu order, matching ones first, then " and N more".

    A menu often lists a game again for its docs or cheats; the summary names
    each title once, with a trailing article moved to the front for reading.
    ``matched`` must already be in that display form, as ``Finder.search_page``
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


def image_key(record: ImageRecord) -> tuple:
    """How catalogue dumps are ranked for writing: good, writable, virus-free, then rank."""
    return (record.bad, _format_rank(record.format), bool(record.virus), record.rank, record.id)


def _content_kind(value: Any) -> ContentKind | None:
    if value is None or value == "":
        return None
    try:
        return ContentKind(str(getattr(value, "value", value)))
    except ValueError:
        return None


class Finder:
    """Search, detail and source resolution over the catalogue and the library."""

    def __init__(
        self,
        catalogue: Any,
        library: Any,
        settings: Settings,
        *,
        search_page: PageFunction | None = None,
        facets: FacetsFunction | None = None,
        media: Any = None,
    ) -> None:
        self.catalogue = catalogue
        self.library = library
        self.settings = settings
        self._search_page = search_page
        self._facets = facets
        self._media = media
        self._providers: list[str] | None = None
        self._groups: dict[str | None, str] | None = None
        userdb = getattr(library, "userdb", None)
        self._edits = Corrections(userdb) if userdb is not None else None
        self._overrides: Overrides | None = None
        self._overrides_generation = 0
        self._overrides_lock = threading.Lock()

    def set_catalogue(self, catalogue: Any) -> None:
        """Switch to a newer catalogue and hand it to the library as well."""
        self.catalogue = catalogue
        self._providers = None
        self._groups = None
        self._forget_overrides()
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

    def search_page(self, query: Query) -> ResultPage:
        """One page of the Find list: titles or discs answering ``query``.

        The search matches, filters and sorts by the user's corrections, and
        gives the corrected titles; the rows show the corrected discs.
        """
        search = self._search_page or _default_search_page()
        providers = self.enabled_providers()
        page = search(
            self.catalogue,
            query,
            local_disks=self._local_disks(),
            providers=providers,
            overrides=self.search_overrides(),
        )
        rows_in = list(page.rows)
        ids = list(dict.fromkeys(row.disk_id for row in rows_in))
        edits = self.corrections(ids)
        disks = {
            disk_id: apply_disk(disk, edits.get(disk_id))
            for disk_id, disk in self.catalogue.disks(ids).items()
        }
        availability = self.library.availability(ids, providers)
        contents: dict[int, tuple[Content, ...]] = {}
        viruses: dict[int, str] = {}
        rows: list[ResultRow] = []
        for row in rows_in:
            disk = disks.get(row.disk_id)
            if disk is None:
                continue
            state = availability.get(row.disk_id, Availability.MISSING)
            matched = tuple(display_title(title) for title in row.matched)
            if row.disk_id not in viruses:
                viruses[row.disk_id] = self._row_virus(row.disk_id)
            if query.mode is ResultMode.DISCS:
                if row.disk_id not in contents:
                    contents[row.disk_id] = apply_contents(
                        self.catalogue.contents(row.disk_id), edits.get(row.disk_id)
                    )
                summary = result_summary(disk, contents[row.disk_id], matched)
                if summary != contents_summary(contents[row.disk_id], matched):
                    matched = ()  # a credits line; the title is already the row's label
                rows.append(
                    ResultRow(
                        disk=disk,
                        availability=state,
                        summary=summary,
                        matched=matched,
                        virus=viruses[row.disk_id],
                    )
                )
                continue
            content_id = row.content_id
            title = row.title
            rows.append(
                ResultRow(
                    disk=disk,
                    availability=state,
                    title=display_title(title) if title else "",
                    content_id=content_id,
                    content_kind=_content_kind(row.content_kind)
                    if content_id is not None
                    else None,
                    matched=matched,
                    virus=viruses[row.disk_id],
                )
            )
        return ResultPage(query=query, rows=tuple(rows), total=int(page.total))

    def facets(self) -> Facets:
        """Crews, years and types for the filter drop-downs, with disc counts.

        A disc the user gave another crew or year counts under the corrected one.
        """
        return (self._facets or _default_facets())(self.catalogue, self.search_overrides())

    def search_overrides(self) -> Overrides:
        """The user's corrections as the search matches, filters and sorts by them.

        Worked out again after a correction is saved or reverted and after
        the catalogue changes; a search in between uses the same object.
        """
        with self._overrides_lock:
            if self._overrides is not None:
                return self._overrides
            generation = self._overrides_generation
        overrides = self._build_overrides()
        with self._overrides_lock:
            if generation == self._overrides_generation:
                self._overrides = overrides
        return overrides

    def _build_overrides(self) -> Overrides:
        if self._edits is None:
            return Overrides()
        try:
            edits = self._edits.every(self.catalogue)
        except sqlite3.Error:
            return Overrides()
        found = self.catalogue.disks(disk_id for disk_id, edit in edits.items() if edit.fields)
        titles: dict[int, tuple[int, str]] = {}
        for disk_id, edit in edits.items():
            if edit.titles:
                for content_id, (_old, new) in corrected_titles(
                    self.catalogue.contents(disk_id), edit
                ).items():
                    titles[content_id] = (disk_id, new)
        return Overrides(
            {disk_id: apply_disk(disk, edits[disk_id]) for disk_id, disk in found.items()}, titles
        )

    def _forget_overrides(self) -> None:
        with self._overrides_lock:
            self._overrides = None
            self._overrides_generation += 1

    def _local_disks(self) -> set[int]:
        local_disks = getattr(self.library, "local_disks", None)
        if not callable(local_disks):
            return set()
        with contextlib.suppress(sqlite3.Error):
            return set(local_disks())
        return set()

    def _row_virus(self, disk_id: int) -> str:
        """The virus on the dump the writer would use for a disc, "" when none is known."""
        local, record = self._dump_to_write(disk_id)
        return (local.virus if local is not None else "") or (record.virus if record else "")

    # Detail ------------------------------------------------------------------

    def detail(self, disk_id: int) -> DiskDetail:
        """Everything known about one disk; raises LookupError when it is not catalogued.

        Pictures come disc first, then titles, in the catalogue's order.
        Trivia holds the catalogue's facts and notes; Wikipedia summaries are
        fetched separately with ``summaries``. ``write_local`` is the library
        file the writer would use, if any, and ``virus`` describes the dump the
        writer would use: its boot block when it is local, the catalogue's
        flag otherwise, or None when nothing is known.
        """
        original = self.catalogue.disk(disk_id)
        if original is None:
            raise LookupError(f"Disk {disk_id} is not in the catalogue.")
        edit = self.corrections([disk_id]).get(disk_id, Correction())
        disk = apply_disk(original, edit)
        contents = tuple(self.catalogue.contents(disk_id))
        availability = self.library.availability([disk_id], self.enabled_providers())
        images = tuple(self.catalogue.images(disk_id))
        media = sorted(
            self._optional("media", disk_id), key=lambda item: item.content_id is not None
        )
        trivia = [
            item for item in self._optional("trivia", disk_id) if item.kind != WIKIPEDIA_TRIVIA
        ]
        write_local, write_record = self._dump_to_write(disk_id)
        return DiskDetail(
            disk=disk,
            contents=apply_contents(contents, edit),
            images=images,
            locations=tuple(self.catalogue.locations(disk_id)),
            links=tuple(self.catalogue.links(disk_id)),
            local_files=tuple(self.library.files_for_disk(disk_id)),
            availability=availability.get(disk_id, Availability.MISSING),
            media=tuple(media),
            trivia=tuple(trivia),
            crew=self._crew(disk),
            virus=self._detail_virus(disk, write_local, write_record),
            write_local=write_local,
            edited=tuple(name for name in EDITABLE_FIELDS if name in edit.fields),
            original=original if edit else None,
            edited_titles=tuple(
                (content_id, old)
                for content_id, (old, _new) in corrected_titles(contents, edit).items()
            ),
        )

    def summaries(self, disk_id: int, content_id: int | None = None) -> list[TriviaItem]:
        """Wikipedia summaries for a disc and one of its titles; blocks on the network.

        The title's articles come first (with ``content_id`` set), then the
        disc's, then the crew's. Empty when media downloads are switched off.
        """
        media = self.media_cache()
        if not media.enabled:
            return []
        disk = self.catalogue.disk(disk_id)
        if disk is None:
            return []
        disk = apply_disk(disk, self.corrections([disk_id]).get(disk_id))
        wanted: list[tuple[str, int | None]] = []
        rows = [item for item in self._optional("trivia", disk_id) if item.kind == WIKIPEDIA_TRIVIA]
        if content_id is not None:
            wanted += [(item.text, content_id) for item in rows if item.content_id == content_id]
        wanted += [(item.text, None) for item in rows if item.content_id is None]
        crew = self._crew(disk)
        if crew is not None and crew.wikipedia:
            wanted.append((crew.wikipedia, None))
        seen: set[str] = set()
        found: list[TriviaItem] = []
        for article, owner in wanted:
            key = article.strip().casefold().replace("_", " ")
            if not key or key in seen:
                continue
            seen.add(key)
            item = media.wikipedia_summary(article)
            if item is not None:
                found.append(replace(item, content_id=owner))
        return found

    def media_file(self, item: MediaItem) -> Path | None:
        """The cached picture for ``item``, downloaded when needed; None when unavailable."""
        return self.media_cache().fetch(item)

    def picture_count(self, platforms: Sequence[Platform] = ()) -> Any:
        """How many of the catalogue's pictures of ``platforms`` are cached (Download All Pictures)."""
        from .online import prefetch

        return prefetch.count(self.media_cache(), self.catalogue.picture_addresses(platforms))

    def download_pictures(
        self, platforms: Sequence[Platform], progress=None, cancel: object | None = None
    ) -> Any:
        """Fetch every picture of ``platforms`` into the details pane's cache."""
        from .online import prefetch

        addresses = self.catalogue.picture_addresses(platforms)
        return prefetch.download(self.media_cache(), addresses, progress, cancel)

    def media_cache(self) -> Any:
        """The picture and summary cache, created on first use."""
        if self._media is None:
            from .online.media import MediaCache

            settings = self.settings
            self._media = MediaCache(
                enabled=lambda: bool(settings.fetch_media and settings.online_enabled)
            )
        return self._media

    def clean_alternates(self, disk_id: int) -> list[ImageRecord]:
        """Good dumps of the same disc the catalogue does not flag with a virus, best first."""
        return sorted(
            (
                record
                for record in self.catalogue.images(disk_id)
                if not record.virus and not record.bad
            ),
            key=image_key,
        )

    def link_file(self, local: LocalFile, disk_id: int) -> LocalFile:
        """Keep an unmatched library image with a disc; see ``Library.link_file``."""
        return self.library.link_file(local, disk_id)

    def unlink_file(self, local: LocalFile) -> LocalFile:
        """Stop keeping a library image with its disc; see ``Library.unlink_file``."""
        return self.library.unlink_file(local)

    def clean_file(self, local: LocalFile) -> LocalFile:
        """Remove the boot block virus from a library file; see ``Library.clean_file``.

        An image inside an archive is saved into the download folder, filed
        as a download of its disc would be.
        """
        folders = (
            self.archive_folders(local.disk_id) if local.disk_id is not None else DEFAULT_FOLDERS
        )
        return self.library.clean_file(
            local, download_folder=self.settings.download_folder, folders=folders
        )

    def _optional(self, method: str, *arguments: Any) -> list[Any]:
        """A catalogue list older catalogues do not have, or [] when missing."""
        function = getattr(self.catalogue, method, None)
        if not callable(function):
            return []
        try:
            return list(function(*arguments))
        except sqlite3.Error:
            return []

    def _crew(self, disk: Disk) -> CrewInfo | None:
        """The history of the crew that made ``disk``, when the disk still names it."""
        function = getattr(self.catalogue, "crew_for_disk", None)
        if not disk.crew or not callable(function):
            return None
        try:
            crew = function(disk.id)
        except sqlite3.Error:
            return None
        # A correction that names another crew leaves the catalogue's history out.
        return crew if crew is not None and crew.name == disk.crew else None

    def _detail_virus(
        self, disk: Disk, local: LocalFile | None, record: ImageRecord | None
    ) -> VirusReport | None:
        """The virus report for the dump the writer would use: ``local``, else ``record``."""
        from .images import virus

        flag = record.virus if record is not None else ""
        report = self.local_virus_report(local) if local is not None else None
        if report is None and flag:
            report = virus.flagged(flag, disk.platform)
        if report is None:
            return None
        if report.status is VirusStatus.FLAGGED or (
            report.status is VirusStatus.VIRUS and not report.removable
        ):
            alternates = [
                alt
                for alt in self.clean_alternates(disk.id)
                if record is None or alt.id != record.id
            ]
            if alternates:
                report = replace(
                    report,
                    explanation=f"{report.explanation} The catalogue lists a clean dump of this "
                    f"disc, {alternates[0].name}, which can be written instead.",
                )
        return report

    def local_virus_report(self, local: LocalFile) -> VirusReport | None:
        """What the boot block of a library file holds, read from the file now.

        The virus the catalogue names on the file's dump, if any, is folded
        in. A file that cannot be read now is described as the last scan
        found it. None when there is no boot block to check: the image does
        not decode to sectors, or it is unreadable and the scan found nothing.
        """
        from .images import virus

        record = None
        if self.catalogue is not None and local.image_id is not None:
            record = self.catalogue.image(local.image_id)
        flag = record.virus if record is not None else ""
        try:
            data = self.library.read_bytes(local)
            inspection = inspect_bytes(data, image_file_name(local.path, local.member))
        except Exception:  # an unreadable file falls back to what the scan recorded
            if local.virus:
                return VirusReport(
                    VirusStatus.VIRUS,
                    name=local.virus,
                    kind="boot",
                    explanation=(
                        f"The boot block held the {local.virus} virus when the library was "
                        "scanned. The file cannot be read now, so it was not checked again."
                    ),
                    source=virus.SOURCE_BUILT_IN,
                )
            return None
        if inspection.raw is None:
            return None
        return virus.detect(inspection.raw, inspection.platform, catalogue_virus=flag)

    # Sources -----------------------------------------------------------------

    def sources_for(self, item: QueueItem) -> list[ImageSource]:
        """Where to take an image for ``item`` from: local files first, then downloads.

        Each download carries every dump of the disc (``ImageSource.dumps``),
        which a download with no checksum of its own is checked against.
        """
        sources: list[ImageSource] = []
        disk = self.catalogue.disk(item.disk_id) if item.disk_id is not None else None
        dumps = tuple(self.catalogue.images(disk.id)) if disk is not None else ()
        image = self._image_lookup(dumps)
        platform = item.platform or (disk.platform if disk is not None else None)
        seen: set[tuple[str, str]] = set()
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
        for local in self._ranked_files(item.disk_id, item.image_id, image):
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
        locations = self._ranked_locations(item.disk_id, item.image_id, image)
        names = self.provider_names() if locations else {}
        for location in locations:
            host = urllib.parse.urlsplit(location.url).netloc
            name = names.get(location.provider, location.provider)
            sources.append(
                ImageSource(
                    label=f"{name} ({host})" if host else name,
                    platform=platform,
                    location=location,
                    image=image(location.image_id),
                    dumps=dumps,
                )
            )
        return sources

    def _dump_to_write(self, disk_id: int) -> tuple[LocalFile | None, ImageRecord | None]:
        """The dump the writer would use for a disc: (local file or None, catalogue dump or None).

        It is the first source ``sources_for`` gives for the whole disc: the
        best local copy, or else the best download from an enabled provider,
        which names no catalogue dump when the catalogue does not know which
        dump the provider hosts. A disc with neither gives its best-ranked
        catalogue dump, so the flag of a missing disc still shows. Row
        warnings and the details pane both describe this dump.
        """
        images = list(self.catalogue.images(disk_id))
        image = self._image_lookup(images)
        files = self._ranked_files(disk_id, None, image)
        if files:
            return files[0], image(files[0].image_id)
        locations = self._ranked_locations(disk_id, None, image)
        if locations:
            return None, image(locations[0].image_id)
        return None, (min(images, key=image_key) if images else None)

    def _image_lookup(
        self, known: Iterable[ImageRecord] = ()
    ) -> Callable[[int | None], ImageRecord | None]:
        """Catalogue dumps by id: ``known`` ones straight away, others read once each."""
        found: dict[int, ImageRecord | None] = {record.id: record for record in known}

        def image(image_id: int | None) -> ImageRecord | None:
            if image_id is None:
                return None
            if image_id not in found:
                found[image_id] = self.catalogue.image(image_id)
            return found[image_id]

        return image

    def _ranked_files(
        self, disk_id: int, image_id: int | None, image: Callable[[int | None], Any]
    ) -> list[LocalFile]:
        """Local copies of a disc, or of one dump of it, best first."""
        if image_id is not None:
            files = self.library.files_for_image(image_id)
        else:
            files = self.library.files_for_disk(disk_id)
        return sorted(files, key=lambda local: _local_key(local, image(local.image_id)))

    def _ranked_locations(
        self, disk_id: int, image_id: int | None, image: Callable[[int | None], Any]
    ) -> list[Location]:
        """Downloads of a disc, or of one dump of it, from enabled providers, best first."""
        if not self.settings.online_enabled:
            return []
        locations = [
            location
            for location in self.catalogue.locations(disk_id)
            if self.settings.provider_enabled(location.provider)
            and (image_id is None or location.image_id == image_id)
        ]
        return sorted(
            locations, key=lambda location: _location_key(location, image(location.image_id))
        )

    def archive_folders(self, disk_id: int) -> tuple[str, str]:
        """The type and crew folders a download of this disk is filed under.

        A crew the user corrected names the crew folder; a corrected cracker
        or publisher does for a single disk, as the catalogue's would.
        """
        disk = self.catalogue.disk(disk_id)
        if disk is None:
            return DEFAULT_FOLDERS
        edit = self.corrections([disk_id]).get(disk_id, Correction())
        disk = apply_disk(disk, edit)
        kind, crew = archive_folders(
            disk, self.catalogue.contents(disk_id), self._series_groups().get(disk.series_id, "")
        )
        if edit.fields.get("crew"):
            crew = disk.crew
        return kind, crew

    # Corrections -------------------------------------------------------------

    def corrections(self, disk_ids: Iterable[int]) -> dict[int, Correction]:
        """The user's corrections of these discs; empty without a user database."""
        if self._edits is None:
            return {}
        try:
            return self._edits.load(self.catalogue, disk_ids)
        except sqlite3.Error:
            return {}

    def save_details(
        self, disk_id: int, values: dict[str, str], titles: dict[int, str] | None = None
    ) -> None:
        """Store the Edit Details form for a disc: whatever differs from the catalogue.

        ``values`` holds editable fields by name, ``titles`` title names by
        content id. Raises ``CorrectionError`` with a sentence for the user
        for a date or name that cannot be stored, and LookupError for a disc
        the catalogue does not have.
        """
        disk = self.catalogue.disk(disk_id)
        if disk is None:
            raise LookupError(f"Disk {disk_id} is not in the catalogue.")
        edit = correction_from_form(disk, self.catalogue.contents(disk_id), values, titles)
        self._edit_store().save(self.catalogue, disk_id, edit)
        self._forget_overrides()

    def revert_details(self, disk_id: int) -> None:
        """Forget the user's corrections of a disc, so the catalogue's values show again."""
        self._edit_store().revert(self.catalogue, disk_id)
        self._forget_overrides()

    def _edit_store(self) -> Corrections:
        if self._edits is None:
            raise RuntimeError("Corrections need the user database, which is not open.")
        return self._edits

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


def _local_key(local: LocalFile, record: ImageRecord | None) -> tuple:
    """How local copies are ranked for writing.

    A writable alternate beats a better-ranked dump gw cannot take, and a
    copy without a virus (on its boot block or in the catalogue) beats one
    with a virus.
    """
    return (
        record.bad if record else False,
        _format_rank(local.format),
        bool(local.virus or (record.virus if record else "")),
        record.rank if record else 0,
        local.path,
        local.member,
    )


def _location_key(location: Location, record: ImageRecord | None) -> tuple:
    """How downloads are ranked for writing.

    Bad dumps and formats gw cannot take come last whoever hosts them, and a
    dump the catalogue flags with a virus comes after a clean one or one the
    catalogue cannot name.
    """
    return (
        record.bad if record else False,
        _format_rank(record.format) if record else 0,
        bool(record.virus) if record else False,
        location.priority,
        record.rank if record else 0,
        location.id,
    )


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

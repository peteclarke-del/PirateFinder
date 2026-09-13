"""Value types shared by the catalogue, library, writer and interface.

Every layer exchanges these frozen dataclasses rather than database rows or
dictionaries, so a change of storage never reaches the window code.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


class Platform(StrEnum):
    AMIGA = "amiga"
    ATARI_ST = "atari-st"


class DiskKind(StrEnum):
    """What sort of release a disk is, used for filtering search results."""

    MENU = "menu"  # numbered crew menu disks and game compacts
    PACK = "pack"  # demo, music, utility and trainer packs
    SINGLE = "single"  # one release per disk, usually a single crack
    COMPILATION = "compilation"  # commercial or other compilations


class ContentKind(StrEnum):
    GAME = "game"
    DEMO = "demo"
    INTRO = "intro"
    UTILITY = "utility"
    MUSIC = "music"
    DOC = "doc"
    CHEAT = "cheat"
    TRAINER = "trainer"
    OTHER = "other"


class Availability(StrEnum):
    LOCAL = "local"  # a matching image is in one of the library folders
    ONLINE = "online"  # an enabled provider hosts an image
    MISSING = "missing"  # known to the catalogue, no image anywhere


@dataclass(frozen=True, slots=True)
class Series:
    id: str
    name: str
    platform: Platform
    kind: DiskKind
    group: str = ""
    aliases: tuple[str, ...] = ()
    disk_count: int = 0


@dataclass(frozen=True, slots=True)
class Disk:
    id: int
    label: str  # short display name, for example "Automation 250"
    platform: Platform
    kind: DiskKind
    series_id: str | None = None
    series_name: str = ""
    number: int | None = None
    part: str = ""  # "A", "B", "1 of 2"
    version: str = ""  # "v2"
    title: str = ""  # full catalogue name, usually the TOSEC name
    date: str = ""
    publisher: str = ""
    cracker: str = ""
    condition: str = ""  # "", "intact", "damaged", "intro only", "missing"
    notes: str = ""
    credits: str = ""
    menu_text: str = ""  # scroller or menu text captured from the disk
    category: str = ""  # "Games", "Applications", "Demos" or "Music"
    crew: str = ""  # the crew the disc belongs to (see archive_layout)
    year: int | None = None
    month: int | None = None  # 1 to 12 when the source gives a month
    day: int | None = None  # 1 to 31 when the source gives a day


@dataclass(frozen=True, slots=True)
class Content:
    disk_id: int
    title: str
    kind: ContentKind
    position: int = 0
    publisher: str = ""
    cracker: str = ""
    version: str = ""
    extra: str = ""  # "+2 trainer", "[doc]", "STE only"
    source: str = ""
    id: int = 0  # contents.id in the catalogue; 0 for contents built in memory


@dataclass(frozen=True, slots=True)
class ImageRecord:
    """One known dump of a disk. A disk can have several alternates."""

    id: int
    disk_id: int
    name: str  # catalogue file name, for example a TOSEC name with extension
    format: str  # "st", "msa", "adf", "dms", "stx", "ipf"
    flags: str = ""  # TOSEC dump flags such as "[a2]" or "[b]"
    size: int | None = None
    crc32: str = ""
    md5: str = ""
    sha1: str = ""
    sha512: str = ""
    bad: bool = False
    rank: int = 0  # lower is preferred
    source: str = ""
    virus: str = ""  # virus the catalogue names on this dump (TOSEC [v ...]), "" if none


@dataclass(frozen=True, slots=True)
class Location:
    """Somewhere on the internet an image can be fetched from."""

    id: int
    disk_id: int
    provider: str  # "internet-archive", "atari-legend", "d-bug", ...
    url: str
    image_id: int | None = None
    container: str = ""  # "zip", "7z", "gz" or "" when the URL is the image
    member: str = ""  # file name inside the container, "" for the first image
    size: int | None = None
    hash_kind: str = ""  # "md5", "sha1", "sha512", "crc32" of the image
    hash_value: str = ""
    page_url: str = ""
    priority: int = 100  # lower is tried first


@dataclass(frozen=True, slots=True)
class Link:
    disk_id: int
    label: str
    url: str


@dataclass(frozen=True, slots=True)
class LocalFile:
    """A disk image found in a library folder, inside an archive or not."""

    path: str
    member: str = ""  # path inside a zip or 7z archive, "" for a plain file
    format: str = ""
    size: int = 0
    crc32: str = ""
    md5: str = ""
    sha1: str = ""
    sha512: str = ""
    image_id: int | None = None
    disk_id: int | None = None
    volume_label: str = ""
    listing: tuple[str, ...] = ()
    display_name: str = ""
    virus: str = ""  # virus found on the boot block when the file was scanned

    @property
    def matched(self) -> bool:
        return self.disk_id is not None


class ResultMode(StrEnum):
    """Whether the Find list has a row per title or a row per disc."""

    TITLES = "titles"
    DISCS = "discs"


class SortOrder(StrEnum):
    RELEVANCE = "relevance"  # best match first; disc order when there is no text
    TITLE = "title"
    TITLE_DESC = "title-desc"
    YEAR = "year"  # oldest first, undated last
    YEAR_DESC = "year-desc"  # newest first, undated last
    DISC = "disc"  # series name, then disc number, part and version
    CREW = "crew"
    PLATFORM = "platform"


@dataclass(frozen=True, slots=True)
class Query:
    """Everything the Find screen asks the catalogue for one page of results.

    ``text`` is matched against every field a row has: title, disc label,
    series, crew, cracker, publisher, platform, type, year, file names and
    notes. Empty text with filters set browses, so "every Automation disc by
    number" is a query with ``crew`` set and ``sort`` DISC.
    """

    text: str = ""
    platform: Platform | None = None
    category: str = ""  # "Games", "Applications", "Demos", "Music"; "" is any
    kinds: frozenset[DiskKind] = frozenset()  # empty means every kind
    crew: str = ""  # exact crew name from Facets.crews; "" is any
    year: int | None = None
    available_only: bool = False
    mode: ResultMode = ResultMode.TITLES
    sort: SortOrder = SortOrder.RELEVANCE
    page: int = 0  # counted from 0
    page_size: int = 100

    @property
    def browsing(self) -> bool:
        """True when there is anything to show: text or at least one filter."""
        return bool(
            self.text.strip()
            or self.platform
            or self.category
            or self.kinds
            or self.crew
            or self.year
            or self.available_only
        )


@dataclass(frozen=True, slots=True)
class ResultRow:
    """One row of the Find list: a title on a disc, or a whole disc."""

    disk: Disk
    availability: Availability
    title: str = ""  # the title in TITLES mode, "" in DISCS mode
    content_id: int | None = None
    content_kind: ContentKind | None = None
    summary: str = ""  # DISCS mode: the disc's contents; TITLES mode: "" or a note
    matched: tuple[str, ...] = ()  # display titles that matched the text
    virus: str = ""  # virus on the dump the writer would use (Finder), "" if none

    @property
    def key(self) -> str:
        if self.content_id is not None:
            return f"title:{self.content_id}"
        return f"disk:{self.disk.id}"


@dataclass(frozen=True, slots=True)
class ResultPage:
    query: Query
    rows: tuple[ResultRow, ...]
    total: int  # rows matching the query across every page

    @property
    def pages(self) -> int:
        return max(1, -(-self.total // max(1, self.query.page_size)))


@dataclass(frozen=True, slots=True)
class Facets:
    """Choices for the filter drop-downs, with how many discs each has."""

    crews: tuple[tuple[str, int], ...] = ()
    years: tuple[tuple[int, int], ...] = ()
    categories: tuple[tuple[str, int], ...] = ()


class VirusStatus(StrEnum):
    CLEAN = "clean"  # nothing on the boot block but standard or no boot code
    VIRUS = "virus"  # a known virus; ``removable`` says whether it can be cleaned
    ANTIVIRUS = "antivirus"  # a self-copying anti-virus or immuniser boot block
    KNOWN_BOOT = "known-boot"  # a named loader, intro or utility boot block
    UNKNOWN_BOOT = "unknown-boot"  # executable boot code nobody has identified
    FLAGGED = "flagged"  # the catalogue names a virus the boot block does not show


@dataclass(frozen=True, slots=True)
class VirusReport:
    """What the boot block of one image holds, and what can be done about it."""

    status: VirusStatus
    name: str = ""  # "SCA", "Byte Bandit 1", "Ghost A"
    kind: str = ""  # "boot", "file", "link", "system": where the virus lives
    removable: bool = False  # True when clean() can restore a standard boot block
    explanation: str = ""  # one or two sentences for the details pane
    source: str = ""  # "built-in", "Amiga Bootblock Reader", "TOSEC"

    @property
    def infected(self) -> bool:
        return self.status in (VirusStatus.VIRUS, VirusStatus.FLAGGED)


@dataclass(frozen=True, slots=True)
class MediaItem:
    """A picture for the details pane; the image is fetched and cached on demand."""

    kind: str  # "menu", "intro", "snap", "title", "boxart", "demo"
    url: str
    source: str
    credit: str = ""  # attribution shown under the picture
    page_url: str = ""
    thumb_url: str = ""
    width: int | None = None
    height: int | None = None
    content_id: int | None = None  # None when the picture is of the disc


@dataclass(frozen=True, slots=True)
class TriviaItem:
    kind: str  # "fact", "note", "summary"
    text: str
    source: str
    url: str = ""
    licence: str = ""  # shown with the text, for example "CC BY-SA 4.0"
    title: str = ""  # heading, for example the Wikipedia article title
    content_id: int | None = None


@dataclass(frozen=True, slots=True)
class CrewInfo:
    name: str
    notes: str = ""
    members: tuple[str, ...] = ()
    founded: str = ""
    source: str = ""
    url: str = ""
    wikipedia: str = ""


@dataclass(frozen=True, slots=True)
class DiskDetail:
    disk: Disk
    contents: tuple[Content, ...] = ()
    images: tuple[ImageRecord, ...] = ()
    locations: tuple[Location, ...] = ()
    links: tuple[Link, ...] = ()
    local_files: tuple[LocalFile, ...] = ()
    availability: Availability = Availability.MISSING
    media: tuple[MediaItem, ...] = ()
    trivia: tuple[TriviaItem, ...] = ()  # catalogue facts and notes; summaries load later
    crew: CrewInfo | None = None
    virus: VirusReport | None = None  # for the dump that would be written
    write_local: LocalFile | None = None  # the library file the writer would use, if any
    # What the user corrected (library/corrections.py): the names of the Disk
    # fields they changed, the catalogue's disk when there are any, and
    # (content id, catalogue title) for each title they renamed.
    edited: tuple[str, ...] = ()
    original: Disk | None = None
    edited_titles: tuple[tuple[int, str], ...] = ()


@dataclass(frozen=True, slots=True)
class ImageSource:
    """Where the writer should take an image from.

    Exactly one of ``local`` or ``location`` is set. ``image`` is the dump
    the source is known to hold, if any. ``dumps`` lists every dump the
    catalogue has for the disc; a download with no checksum of its own must
    be a copy of one of them.
    """

    label: str
    platform: Platform | None = None
    local: LocalFile | None = None
    location: Location | None = None
    image: ImageRecord | None = None
    dumps: tuple[ImageRecord, ...] = ()


@dataclass(frozen=True, slots=True)
class Geometry:
    cylinders: int
    heads: int
    sectors: int
    sector_size: int = 512

    @property
    def track_count(self) -> int:
        return self.cylinders * self.heads


@dataclass(frozen=True, slots=True)
class PreparedImage:
    """An image converted into something ``gw write`` accepts as it stands."""

    label: str
    platform: Platform
    write_path: str
    geometry: Geometry | None
    gw_format: str = ""  # "" lets gw infer the format from the suffix
    diskdefs_path: str = ""  # custom disk definition file, when needed
    tracks: str = ""  # explicit --tracks value, when needed
    verifiable: bool = True  # flux images are written without read-back
    notes: tuple[str, ...] = ()  # conversions made, shown to the user


@dataclass(frozen=True, slots=True)
class DeviceStatus:
    connected: bool
    message: str
    model: str = ""
    firmware: str = ""
    port: str = ""
    host_tools: str = ""


@dataclass(frozen=True, slots=True)
class WriteProgress:
    fraction: float
    cylinder: int
    head: int
    track_number: int
    track_count: int
    retry: int = 0
    message: str = ""


class WriteStatus(StrEnum):
    VERIFIED = "verified"
    WRITTEN = "written"  # written without read-back verification
    FAILED = "failed"
    WRITE_PROTECTED = "write-protected"
    NO_DISK = "no-disk"
    CANCELLED = "cancelled"
    SKIPPED = "skipped"
    UNAVAILABLE = "unavailable"  # no usable image could be found or prepared


@dataclass(frozen=True, slots=True)
class WriteOutcome:
    status: WriteStatus
    summary: str
    diagnostic: str = ""
    retries: int = 0
    failed_tracks: tuple[str, ...] = ()
    seconds: float = 0.0
    # What was done to the image on the way, one sentence each: a conversion,
    # a virus removed, a download that could not be checked.
    notes: tuple[str, ...] = ()

    @property
    def succeeded(self) -> bool:
        return self.status in (WriteStatus.VERIFIED, WriteStatus.WRITTEN)


@dataclass(slots=True)
class QueueItem:
    """One disk waiting to be written. Mutable while a session runs."""

    id: str
    label: str
    platform: Platform | None
    disk_id: int | None = None
    image_id: int | None = None  # a chosen alternate, None for the best one
    local: LocalFile | None = None  # set when queued from an unmatched file
    copies: int = 1
    clean_virus: bool = True  # remove a removable boot block virus before writing
    outcome: WriteOutcome | None = None
    source_used: str = ""
    notes: list[str] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class SessionSummary:
    started: str
    finished: str
    drive: str
    items: tuple[tuple[str, WriteOutcome, str], ...]  # label, outcome, source

    def count(self, *statuses: WriteStatus) -> int:
        return sum(1 for _label, outcome, _source in self.items if outcome.status in statuses)


@dataclass(frozen=True, slots=True)
class BootRecheck:
    """The library's boot blocks checked again because the virus data changed."""

    checked: int = 0  # images whose boot block was read again
    changed: int = 0  # of those, images whose boot block is now reported differently
    unreadable: int = 0  # images that could not be read now; the next scan reads them
    cancelled: bool = False


@dataclass(frozen=True, slots=True)
class ScanSummary:
    folders: tuple[str, ...]
    files_seen: int
    images_found: int
    matched: int
    unmatched: int
    new: int
    removed: int
    errors: tuple[str, ...] = ()
    seconds: float = 0.0
    cancelled: bool = False

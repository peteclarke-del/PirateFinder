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

    @property
    def matched(self) -> bool:
        return self.disk_id is not None


@dataclass(frozen=True, slots=True)
class SearchFilters:
    platform: Platform | None = None
    kinds: frozenset[DiskKind] = frozenset()  # empty means every kind
    available_only: bool = False


@dataclass(frozen=True, slots=True)
class SearchResult:
    """A catalogue disk, or an unmatched local file, that answers a search."""

    availability: Availability
    disk: Disk | None = None
    local: LocalFile | None = None
    matched: tuple[str, ...] = ()  # content titles that matched the query
    summary: str = ""  # short contents line for the result row
    score: float = 0.0

    @property
    def key(self) -> str:
        if self.disk is not None:
            return f"disk:{self.disk.id}"
        assert self.local is not None
        return f"file:{self.local.path}::{self.local.member}"


@dataclass(frozen=True, slots=True)
class DiskDetail:
    disk: Disk
    contents: tuple[Content, ...] = ()
    images: tuple[ImageRecord, ...] = ()
    locations: tuple[Location, ...] = ()
    links: tuple[Link, ...] = ()
    local_files: tuple[LocalFile, ...] = ()
    availability: Availability = Availability.MISSING


@dataclass(frozen=True, slots=True)
class ImageSource:
    """Where the writer should take an image from.

    Exactly one of ``local`` or ``location`` is set.
    """

    label: str
    platform: Platform | None = None
    local: LocalFile | None = None
    location: Location | None = None
    image: ImageRecord | None = None


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

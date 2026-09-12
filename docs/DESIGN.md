# PirateFinder design

PirateFinder is a native GNOME application for Linux. It keeps a catalogue of
what is on the numbered menu disks, compacts and packs released by Amiga and
Atari ST cracking crews (Automation, Pompey Pirates, Medway Boys, D-Bug, Skid
Row compacts and many more), together with single-game cracks. The user
searches for a game, a crew or a disk number, picks one or more disks from the
results, and PirateFinder finds the images in local or network folders or
downloads them, prepares them, and writes them to real floppies with a
Greaseweazle, reporting progress and a summary at the end.

The sections below explain how the parts fit together. They are kept up to
date with the code.

## Decisions

| Topic | Decision |
| --- | --- |
| Interface | GTK 4 and libadwaita through PyGObject, stock Adwaita styling, following Greaseweazle-GUI and PiStorm Imager. Libadwaita 1.5 is the floor (Ubuntu 24.04), so nothing newer than 1.5 is used. |
| Writing | PirateFinder runs the Greaseweazle host tool (`gw`) itself. Greaseweazle-GUI is not changed or launched. |
| Catalogue scope | Atari ST menu disks, Amiga game compacts, demo, music and tool packs on both machines, and single-game cracks on both machines. |
| Missing images | Downloaded on request from enabled online providers, checked against the catalogue hash, and kept in a download folder the user chooses (it may be on a NAS) so they are local next time. |
| Licence | Code GPL-3.0-or-later, matching Greaseweazle-GUI and PiStorm Imager. The catalogue data is CC BY-NC-SA 4.0 because it includes Atari Legend data under that licence. |
| Packaging | A Debian package that installs to `/usr/lib/piratefinder` with a private copy of the Greaseweazle host tools, like Greaseweazle-GUI. |

## Layout

```
src/piratefinder/            the application
  models.py                  value types shared by every layer
  archive_layout.py          download folder layout: platform, type, crew
  paths.py                   XDG locations and catalogue lookup order
  settings.py                user settings (JSON under ~/.config/piratefinder)
  finder.py                  facade the interface talks to: search, detail, sources
  catalogue/
    schema.py                catalogue database layout
    store.py                 read-only access to catalogue.sqlite
    naming.py                TOSEC name parsing and text normalisation
    search.py                query parsing and ranked catalogue search
    update.py                check for and install a newer catalogue snapshot
  images/
    vendor/                  code lifted from the File Forges and Greaseweazle-GUI
    archives.py              list and read members of zip, 7z and gzip files
    inspect.py               identify an image, decode it to raw sectors, hash it
    prepare.py               turn any supported image into something gw can write
  greaseweazle/
    runner.py                streaming subprocess with cancellation
    diskdefs.py              disk definitions for non-standard geometries
    client.py                probe the device and write prepared images
  library/
    userdb.py                user database: library index, history, corrections
    scanner.py               incremental scan of library folders
    library.py               matching, availability and unmatched-file search
  online/
    http.py                  throttled downloads with retry and resume
    fetch.py                 fetch a catalogue location into the download folder
  jobs/
    queue.py                 the write queue, persisted between runs
    session.py               runs a queue: resolve, download, prepare, prompt, write
    history.py               past sessions and printable summaries
  ui/                        GTK 4 / libadwaita windows and widgets
catalogue_builder/           builds catalogue.sqlite from public sources (not shipped)
  context.py                 cached, throttled fetcher handed to every source
  series.py                  series registry loaded from data/series/*.toml
  records.py                 intermediate records produced by sources
  merge.py                   joins records per disk and writes the database
  sources/*.py               one importer per source
data/series/*.toml           series definitions, aliases and per-source match rules
tests/                       unittest suites, synthetic disk images only
packaging/                   Debian package build, launcher, udev rule
```

## Data model

`catalogue.sqlite` is built by `python3 -m catalogue_builder` and never
written by the application. Its tables (see `catalogue/schema.py`):

- `series`: a numbered series such as Automation or Skid Row Compact, with
  platform, kind and aliases (`series_alias`) used by the query parser.
- `disks`: one row per disk, part and version. `label` is the short display
  name ("Automation 250", "Pompey Pirates 51", "D-Bug 100 B").
- `contents`: what is on each disk, in menu order, with crack and credit notes.
- `images`: every known dump of a disk with its hashes. TOSEC alternates
  (`[a]`, `[a2]`) are separate rows. `rank` orders them, bad dumps last.
- `locations`: URLs an image can be fetched from, with the container type,
  the member inside it and the hash the result must have.
- `links`: reference pages (Atari Legend, D-Bug, Demozoo).
- `disk_fts` and `disk_trigram`: full-text and substring indexes, one row
  per disk.
- `sources` and `meta`: provenance, licences and build date.

The user database (`~/.local/share/piratefinder/user.sqlite`) holds the
library index (one row per image file or archive member, with file and raw
hashes, the matched catalogue image and disk, volume label and file listing),
write history and user corrections. It survives catalogue replacement: after
an update, local files are matched again by hash.

### Hashes and matching

TOSEC lists hashes of raw sector images (`.st`, `.adf`). A local `.msa`,
`.dms` or `.adz` is decoded to raw sectors before hashing, so it matches the
TOSEC entry for the same disk whatever its container. Atari Legend lists the
SHA-512 of its `.msa` files, so the file hash is kept as well. Matching tries
raw MD5, raw SHA-1, raw CRC32 with size, then file SHA-512 and file MD5.
Unmatched images stay in the library and are searchable by file name, volume
label and the names of the files on the disk.

## Catalogue sources

| Source | Gives | Used as |
| --- | --- | --- |
| TOSEC DATs | Disk names and hashes for ST compilations, demos and games, Amiga compilations, packs and games | Disk list, image hashes |
| Atari Legend weekly SQL dump (CC BY-NC-SA 4.0) | Contents of 6,900 ST menu disks, condition, SHA-512, dump downloads | Contents, locations |
| D-Bug search (d-bug.me) | Contents of Automation 0-512 and D-Bug 1-200, credits, D-Bug MSA downloads | Contents, locations |
| Steem Automation list | Automation contents cross-check and part numbering | Contents |
| Period crew lists on the Pompey Pirates CD-R | Pompey, Medway, FOF, Superior, Cynix lists and a merged title index | Contents |
| 8bitchip menu index | Game to disk index for Vectronix, SuperGAU and others | Contents |
| exxos POV pages | Persistence of Vision contents and downloads | Contents, locations |
| Internet Archive (TOSEC mirrors, Software Library, atari-st-collection) | Downloadable images, including single files inside large archives | Locations |
| amigascne.org index and menu texts | Amiga pack disk files with CRC32, menu scroller texts | Locations, searchable menu text |
| Demozoo daily dump (optional) | Pack membership for demo packs | Contents |

Sites that deliberately block automated clients (Janeway, the English Amiga
Board, Hall of Light) are not scraped. Requests to small hobby sites are
throttled to one per second or slower and cached, so a rebuild fetches only
what changed.

## Runtime interfaces

The interface code talks to four objects: `Finder`, `Library`,
`WriteSession` and the Greaseweazle `client` module. Signatures:

```python
# catalogue/store.py
class Catalogue:
    @classmethod
    def open(cls, path: Path) -> Catalogue          # read-only
    built_at: str; path: Path
    def series(self) -> list[Series]
    def aliases(self) -> dict[str, tuple[str, ...]]  # normalised alias -> series ids
    def disk(self, disk_id: int) -> Disk | None
    def disks(self, ids: Iterable[int]) -> dict[int, Disk]
    def contents(self, disk_id: int) -> list[Content]
    def images(self, disk_id: int) -> list[ImageRecord]
    def image(self, image_id: int) -> ImageRecord | None
    def locations(self, disk_id: int) -> list[Location]
    def links(self, disk_id: int) -> list[Link]
    def match_image(self, *, md5="", sha1="", sha512="", crc32="", size=None) -> ImageRecord | None
    def disks_with_locations(self, providers: Iterable[str]) -> set[int]
    def stats(self) -> dict[str, int]
    def sources(self) -> list[dict[str, str]]
def locate_catalogue() -> Path | None

# catalogue/search.py
def parse_query(text: str, aliases: Mapping[str, tuple[str, ...]]) -> ParsedQuery
def search_catalogue(catalogue, text, filters: SearchFilters, limit=500) -> list[CatalogueHit]
    # CatalogueHit(disk_id, score, matched: tuple[str, ...])

# images/inspect.py
def inspect_bytes(data: bytes, name: str) -> Inspection
    # Inspection(format, platform, raw: bytes | None, geometry, volume_label, listing,
    #            hashes, raw_hashes, size, problem)
# images/archives.py
def is_archive(path) -> bool; def members(path) -> list[Member]
def read_member(path, name) -> bytes        # "" = first disk image; "outer/in.zip::image.st"
def iter_members(path, names=None, *, on_error=None) -> Iterator[tuple[Member, bytes]]
# images/prepare.py
def prepare(data: bytes, name: str, workdir: Path, *, label: str, platform: Platform | None) -> PreparedImage
    # raises PrepareError(message) with a sentence fit to show the user

# greaseweazle/client.py
def find_gw() -> str | None   # $PIRATEFINDER_GW, then PATH, then /usr/lib/piratefinder/bin/gw
def probe(timeout: float = 20, *, device: str = "") -> DeviceStatus
def write(prepared: PreparedImage, *, drive: str, device: str = "", retries: int = 3,
          pre_erase: bool = False, progress=None, controller=None, timeout: float = 1800) -> WriteOutcome

# library/library.py
class Library:
    def __init__(self, userdb: UserDatabase, catalogue: Catalogue)
    def scan(self, folders, progress, controller) -> ScanSummary
    def files_for_disk(self, disk_id: int) -> list[LocalFile]
    def availability(self, disk_ids, providers) -> dict[int, Availability]
    def search_unmatched(self, text: str, limit: int = 200) -> list[LocalFile]
    def add_file(self, path: Path) -> list[LocalFile]
    def rematch(self) -> int
    def stats(self) -> dict[str, int]

# finder.py
class Finder:
    def search(self, text: str, filters: SearchFilters) -> list[SearchResult]
    def detail(self, disk_id: int) -> DiskDetail
    def sources_for(self, item: QueueItem) -> list[ImageSource]   # best first

# jobs/session.py
class SessionEvents(Protocol):
    def on_stage(self, item, index, total, stage: str, message: str) -> None
    def on_download(self, item, done: int, total: int | None) -> None
    def on_write_progress(self, item, progress: WriteProgress) -> None
    def ask_insert(self, item, index, total, reason: str = "") -> str  # "write" | "skip" | "stop"
    def on_item_finished(self, item, outcome: WriteOutcome) -> None
class WriteSession:
    def __init__(self, finder, library, settings, items, events)
    def run(self) -> SessionSummary     # blocking; the interface runs it on a worker thread
    def cancel(self) -> None
```

Event callbacks are called on the worker thread. The interface forwards them
to the main loop with `GLib.idle_add`. `ask_insert` blocks the worker until
the user answers the insert-disk prompt. `index` counts disks from 0 and
`total` counts disks, not copies. `on_download` is not called for the
background download of the next disk, which runs while the current one is
written.

Hash meanings in `LocalFile`: `crc32`, `md5` and `sha1` are of the raw sector
image when the file can be decoded, `sha512` is of the file as stored, and
`size` is the raw size. `Location.size` is the size of the downloaded object
when the source gives it (an image for Atari Legend, a zip for the Internet
Archive) and is advisory only. `DiskDetail.locations` lists every provider,
including ones the user has switched off.

The interface reaches these objects only through `ui/backend.py`; the real
wiring is in `ui/real_backend.py`, and `ui/fake_backend.py` supplies synthetic
disks for tests and screenshots.

## Writing

`prepare()` turns every supported image into a file `gw write` accepts:

| Input | Handling |
| --- | --- |
| `.adf` 80 cylinders | Written as it is, `amiga.amigados` (or `amiga.amigados_hd` for 1.76 MB) |
| `.adf` with 81 to 84 cylinders | Custom disk definition with the real cylinder count |
| `.dms` | Decoded to ADF in memory (vendored decoder), then as ADF |
| `.adz`, `.gz` | Decompressed, then as ADF |
| `.st` | Geometry from the boot sector, checked against the file size. Standard geometries use `atarist.360` to `atarist.880`. Anything else (82 or 83 cylinders, 11 sectors single-sided and so on) gets a generated disk definition so no track is dropped. |
| `.msa` | Decoded to raw sectors, then as `.st`, so geometry handling is identical |
| `.stx` | Converted to `.st` only when the Pasti image carries no protection; otherwise refused with an explanation and an alternative dump offered where the catalogue has one |
| `.ipf` | Written directly when the CAPS library is installed; otherwise refused with an explanation |
| `.scp`, `.hfe` | Written directly; Greaseweazle cannot verify flux writes, and the summary says so. Without `--format`, gw writes cylinders 0 to 81 only, so images with more cylinders get an explicit `--tracks` range |
| Inside `.zip` or `.7z` | The disk image member is read, then handled as above |

The Greaseweazle host tool reports some hardware failures, such as a
write-protected disk, with `Command Failed` and an exit status of zero. The
client reads the output as well as the exit status, and reports those as
failures with a specific status (`write-protected`, `no-disk`).

## Interface

One `Adw.ApplicationWindow` with an `Adw.HeaderBar` holding an
`Adw.ViewSwitcher` over four pages, and the main menu on the right.

- **Find**: a search entry, platform and kind filters, an "available only"
  switch, and results split from a detail pane. Each result row shows the
  disk label, a line of its contents with the matching titles in bold, the
  platform, and whether it is local, online or missing. Rows have a check box
  for multiple selection; "Add to Queue" and "Write Now" act on the
  selection. Write Now writes the chosen disks straight away without adding
  them to the queue. The detail pane lists contents, credits, known dumps with
  their source and availability, and reference links.
- **Queue**: disks waiting to be written, reorderable, with the drive choice
  and a Start button. While writing, the page shows the disk in progress,
  track progress, retries and a Cancel button. Before each disk an alert
  asks for the floppy to be inserted (Write, Skip, Stop). At the end a summary
  lists every disk with its result, verification and source, and offers to
  retry the failures or copy a report.
- **Library**: the folders scanned, the download folder, scan progress and
  counts (images, matched, unmatched, duplicates), and a list of unmatched
  files.
- **History**: past write sessions with their summaries.

An `Adw.Banner` appears when no Greaseweazle is connected. Background events
(downloads finished, catalogue updated, scan complete) are announced with
`Adw.Toast`. Library folders are managed on the Library page; adding or
removing one starts a scan. Preferences use `Adw.PreferencesDialog`: download
folder, online downloads and each provider, Greaseweazle drive, device,
retries, pre-erase and whether to ask before each disk, and catalogue
updates. After a session, disks that were written leave the queue and the
rest stay for another attempt.

## Online behaviour

Nothing is downloaded until the user asks for a disk that is not local.
Each provider can be switched off in Preferences, and online use can be
switched off entirely. Every download is checked against the catalogue hash
before it is kept, and a mismatch is deleted. A few sources publish images
the catalogue has no hash for; those are kept, marked as unchecked, and the
session summary says so. The HTTP client identifies itself, keeps at most one
request a second per host, honours `Retry-After`, and backs off on 429 and
5xx responses. The Internet Archive serves single members from inside large
zip and 7z archives, which is how one disk is taken from a multi-gigabyte set.

The catalogue itself is updated by downloading a newer `catalogue.sqlite.gz`
published as a GitHub release asset, checked against its published SHA-256.

Downloads are filed for archiving as
`<download folder>/<platform>/<type>/<crew>/<image>`, decided in
`archive_layout.py`. The type comes from the disk's contents: games, cheats and
documents make `Games`, utilities `Applications`, demos and intros `Demos`, and
music `Music`; intros, documents and cheats count only when a disk holds
nothing else, and a disk with no listed contents is filed by its kind. The crew
is the series' crew (`series.group_name`), or for a single disk its cracker,
then its publisher.

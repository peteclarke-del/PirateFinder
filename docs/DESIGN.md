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
| Missing images | Downloaded on request from enabled online providers, checked against the catalogue hash (the location's own, or that of any dump of the disc), and kept in a download folder the user chooses (it may be on a NAS) so they are local next time. |
| Licence | Code GPL-3.0-or-later, matching Greaseweazle-GUI and PiStorm Imager. The catalogue data is CC BY-NC-SA 4.0 because it includes Atari Legend data under that licence. |
| Packaging | A Debian package that installs to `/usr/lib/piratefinder` with a private copy of the Greaseweazle host tools, like Greaseweazle-GUI. One package per distribution release and architecture (Ubuntu 24.04 and Debian 13; amd64, arm64 and armhf, listed in `packaging/targets.sh`), each built and install-tested in a container of its own release and architecture, because the bundled host tools include modules compiled for that release's Python. |

## Layout

```
src/piratefinder/            the application
  models.py                  value types shared by every layer
  archive_layout.py          download folder layout: platform, type, crew
  paths.py                   XDG locations and catalogue lookup order
  settings.py                user settings (JSON under ~/.config/piratefinder)
  finder.py                  facade the interface talks to: search, detail, sources
  app_update.py              check for, download and install a newer PirateFinder package
  catalogue/
    schema.py                catalogue database layout
    store.py                 read-only access to catalogue.sqlite
    naming.py                TOSEC name parsing and text normalisation
    search.py                query parsing and ranked search, with the user's corrections
    update.py                check for and install a newer catalogue snapshot
  images/
    vendor/                  code lifted from the File Forges and Greaseweazle-GUI
    archives.py              list and read members of zip, 7z and gzip files
    inspect.py               identify an image, decode it to raw sectors, hash it
    virus.py                 boot block virus detection and removal
    prepare.py               turn any supported image into something gw can write
  greaseweazle/
    runner.py                streaming subprocess with cancellation
    diskdefs.py              disk definitions for non-standard geometries
    caps.py                  the SPS Decoder Library gw needs for IPF: status, install, remove
    client.py                probe the device and write prepared images
  library/
    userdb.py                user database: library index, history, corrections, meta
    corrections.py           the user's corrections, kept across catalogue builds
    scanner.py               incremental scan of library folders
    library.py               matching, availability and unmatched-file search
  online/
    http.py                  throttled downloads with retry and resume
    releases.py              the repository's GitHub releases, for both updates
    fetch.py                 fetch a catalogue location into the download folder
    media.py                 cached pictures and Wikipedia summaries
  jobs/
    queue.py                 the write queue, persisted between runs; builds queue items
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
packaging/                   Debian package build and install test, target table, launcher, udev rule
```

## Data model

`catalogue.sqlite` is built by `python3 -m catalogue_builder` and never
written by the application. Its layout is 3 (`SCHEMA_VERSION` in
`catalogue/schema.py`, also in the `meta` table); version 0.1.0 read layout
1. Its tables:

- `series`: a numbered series such as Automation or Skid Row Compact, with
  platform, kind and aliases (`series_alias`) used by the query parser.
- `disks`: one row per disk, part and version. `label` is the short display
  name ("Automation 250", "Pompey Pirates 51", "D-Bug 100 B"). `category`,
  `crew`, `year`, `month`, `day` and `sort_title` serve the filters and sort
  orders, and `crew_id` points at the disc's row in `crews`.
- `contents`: what is on each disk, in menu order, with crack and credit notes.
- `images`: every known dump of a disk with its hashes and TOSEC virus flags.
  TOSEC alternates (`[a]`, `[a2]`) are separate rows. `rank` orders them, bad
  dumps last.
- `locations`: URLs an image can be fetched from, with the container type,
  the member inside it and the hash the result must have.
- `links`: reference pages (Atari Legend, D-Bug, Demozoo).
- `entries`: one row per title on a disc, and one per disc that lists none,
  for the Titles view.
- `entry_fts`, `disk_fts` and `disk_trigram`: full-text indexes per entry and
  per disk, and a substring index per disk.
- `crews`, `media` and `trivia`: crew histories, picture addresses, and
  facts, notes and Wikipedia article titles for the details pane.
- `address_prefix` and `media_credit`: the shared beginnings of addresses and
  the credit lines of pictures, each stored once.
- `sources` and `meta`: provenance, licences, build date and layout.

[Catalogue](CATALOGUE.md) describes every column the builder fills.

The user database (`~/.local/share/piratefinder/user.sqlite`) holds the
library index (one row per image file or archive member, with file and raw
hashes, the matched catalogue image and disk, volume label and file listing),
write history and user corrections, and in `meta` the fingerprint of the virus
data its boot blocks were last checked with (see Viruses). It survives
catalogue replacement: after an update, local files are matched again by
hash, and corrected discs are found again (see User corrections below).

### User corrections

The user may correct a disc's label, catalogue name (`title`), crew, release
date, publisher, cracker and notes, and the name of each title on it, with
Edit Details in the details pane (`ui/edit_details.py`). `library/corrections.py`
owns the rules: `correction_from_form` keeps only what differs from the
catalogue, checks the date (`YYYY`, `YYYY-MM` or `YYYY-MM-DD`, a real calendar
date; it sets `year`, `month` and `day` as well) and refuses an empty label or
title; `apply_disk` and `apply_contents` put the corrections in place.

The catalogue numbers discs and titles afresh with every build, so the user
database keeps each corrected disc in `corrected_discs` with what
identifies it in any build: series, number, part and version for a numbered
disc, and the `match_image` arguments of its dumps for every disc. The fields
are in `corrections` and the titles in `title_corrections`, a title keyed by
its catalogue name and its place among the titles of that name on the disc.
Each row records the disc id it has in one catalogue build (`catalogue`, the
build time). Before corrections are read against another build,
`Corrections.relink` looks every corrected disc up in it again: by series and
number, else by a dump that belongs to a disc of the same platform. A disc the
new build lacks keeps its corrections, unused, until a build has it again.
Reads ask for the current build only, so a search still running on the old
catalogue during an update never gets another disc's corrections.

`Finder` applies corrections to each result row (the corrected disc, and
the titles of a Discs row), to `detail()` (which also reports `edited`, the
catalogue's `original` disc and `edited_titles`), to the summaries looked up
for a disc and to `archive_folders`, where a corrected crew names the crew
folder.

The search sees the corrections too. `Finder.search_overrides()` hands
`catalogue/search.py` an `Overrides`: each disc whose own fields were
corrected, as corrected, and each renamed title by content id. It is built
from `Corrections.every()` once and kept until a correction is saved or
reverted or the catalogue changes. `search_page` and `facets` work out what
differs from the catalogue once per set of overrides, then:

- the crew and year filters take a corrected disc out of its catalogue value
  and add it to its corrected one by id (`d.crew = :crew AND d.id NOT IN
  (...)`, `OR d.id IN (...)`), so the catalogue's indexes still serve them;
  `facets` counts a corrected disc under its corrected crew and year;
- the title, year and crew sort keys use the corrected value where one
  differs, joined from a temporary table on the catalogue connection
  (`Catalogue.temp_table`); without such a correction the SQL is the
  catalogue's alone;
- for text, a row whose corrected text (only the fields and titles a
  correction changed) holds some query word is looked at again: when every
  other word is in its catalogue text, asked of the full-text index for that
  disc's run of entries, the row is added to the result set. Words match by
  the same rule as the index (`search.term_matches`, a word start, or whole
  for one character or a number) over the same words
  (`naming.search_text`, which the builder indexes with);
- a row found by text a correction replaced is still found, because the
  catalogue's index holds that text, but the rows' titles and `matched` are
  the corrected ones, so it is not shown as matching.

### Hashes and matching

TOSEC lists hashes of raw sector images (`.st`, `.adf`). A local `.msa`,
`.dms` or `.adz` is decoded to raw sectors before hashing, so it matches the
TOSEC entry for the same disk whatever its container. Atari Legend lists the
SHA-512 of its `.msa` files, so the file hash is kept as well. Matching tries
raw MD5, raw SHA-1, raw CRC32 with size, then file SHA-512 and file MD5
(`library.match_entry`). The same function checks a download that has no hash
of its own against the dumps of its disc.
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
| amigascne archive index and menu texts, from the scene.org mirror (the archive's own server forbids automated access) | Amiga pack disk files with CRC32, menu scroller texts | Locations, searchable menu text |
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
    def temp_table(self, columns: str, rows) -> str   # a TEMP table on this connection
    def stats(self) -> dict[str, int]
    def sources(self) -> list[dict[str, str]]
def locate_catalogue() -> Path | None

# catalogue/search.py
def parse_query(text: str, aliases: Mapping[str, tuple[str, ...]]) -> ParsedQuery
def search_page(catalogue, query: Query, *, local_disks=(), providers=(),
                overrides: Overrides | None = None) -> CataloguePage
def facets(catalogue, overrides: Overrides | None = None) -> Facets
    # Overrides(disks: {disk id: corrected Disk}, titles: {content id: (disk id, title)})
def term_matches(term: str, words) -> bool   # the full-text index's word rule

# images/inspect.py
def inspect_bytes(data: bytes, name: str) -> Inspection
    # Inspection(format, platform, raw: bytes | None, geometry, volume_label, listing,
    #            hashes, raw_hashes, size, problem, virus)
def boot_block(data: bytes, name: str) -> tuple[bytes, Platform] | None
    # the first kilobyte of the sectors, decoded as inspect_bytes decodes, nothing hashed
def local_platform(local: LocalFile) -> Platform | None
    # from the format (or the suffix); None for ipf, scp, hfe and gz, which hold either
# images/archives.py
def is_archive(path) -> bool; def members(path) -> list[Member]
def read_member(path, name) -> bytes        # "" = first disk image; "outer/in.zip::image.st"
def iter_members(path, names=None, *, on_error=None) -> Iterator[tuple[Member, bytes]]
# images/prepare.py
def prepare(data: bytes, name: str, workdir: Path, *, label: str, platform: Platform | None) -> PreparedImage
    # raises PrepareError(message) with a sentence fit to show the user

# greaseweazle/client.py
def find_gw() -> str | None   # $PIRATEFINDER_GW, then PATH, then /usr/lib/piratefinder/bin/gw
def device_present(device: str = "") -> bool   # files only: sysfs USB ids, /dev links, the port
def probe(timeout: float = 20, *, device: str = "", online: bool = True) -> DeviceStatus
    # runs gw info; online False keeps its firmware lookup on the computer
def write(prepared: PreparedImage, *, drive: str, device: str = "", retries: int = 3,
          pre_erase: bool = False, progress=None, controller=None, timeout: float = 1800) -> WriteOutcome
def gw_environment() -> dict[str, str]   # every gw run: minimal environment + LD_LIBRARY_PATH

# greaseweazle/caps.py
def status() -> CapsStatus   # CapsStatus(state, version, path, machine, build); .usable, .problem
    # state: SYSTEM (the loader finds one), INSTALLED (by PirateFinder), MISSING, UNSUPPORTED
def install(build=None, downloader=None, progress=None, cancel=None) -> CapsStatus
    # raises CapsError(sentence); DownloadCancelled passes through
def remove() -> CapsStatus
def library_path(existing: str = "") -> str   # LD_LIBRARY_PATH: caps.folder() first when installed
def machine() -> str; def build_for_machine(name=None) -> CapsBuild | None; def licence_text() -> str

# library/library.py
class Library:
    def __init__(self, userdb: UserDatabase, catalogue: Catalogue)
    def scan(self, folders, progress, controller) -> ScanSummary   # rechecks boot blocks first
    def recheck_boot_blocks(self, progress=None, cancel=None) -> BootRecheck | None
    def files_for_disk(self, disk_id: int) -> list[LocalFile]
    def availability(self, disk_ids, providers) -> dict[int, Availability]
    def search_unmatched(self, text: str, limit: int = 200) -> list[LocalFile]
    def infected_files(self, limit: int = 200) -> list[LocalFile]
    def add_file(self, path: Path) -> list[LocalFile]
    def rematch(self) -> int
    def stats(self) -> dict[str, int]

# finder.py
class Finder:
    def search_page(self, query: Query) -> ResultPage
    def facets(self) -> Facets
    def search_overrides(self) -> Overrides   # the corrections, as the search takes them
    def detail(self, disk_id: int) -> DiskDetail
    def sources_for(self, item: QueueItem) -> list[ImageSource]   # best first
    def corrections(self, disk_ids) -> dict[int, Correction]
    def save_details(self, disk_id, values: dict[str, str], titles: dict[int, str]) -> None
        # raises CorrectionError with a sentence for the user
    def revert_details(self, disk_id) -> None

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
def with_notes(outcome: WriteOutcome, notes) -> WriteOutcome   # the item's notes, into the result
```

Each result carries the notes made for its disk (`WriteOutcome.notes`): the
conversions `prepare()` made, a boot block virus removed or left in place, and
a download that could not be checked or was already in the download folder.
The notes are cleared at the start of each disk, so they describe the current
session only, and they reach the summary, the history (the `notes` column of
`session_items`) and the copied report. Download Only returns its notes as
well (`Backend.download` gives `Downloaded(path, notes)`), and the window shows
them in an alert rather than a passing notification.

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
| `.ipf` | Written directly when gw can load the SPS Decoder Library (`caps.status().usable`); otherwise refused with `CapsStatus.problem`, which points to IPF Support in Preferences or says no build exists for this processor |
| `.scp`, `.hfe` | Written directly; Greaseweazle cannot verify flux writes, and the summary says so. Without `--format`, gw writes cylinders 0 to 81 only, so images with more cylinders get an explicit `--tracks` range |
| Inside `.zip` or `.7z` | The disk image member is read, then handled as above |

The Greaseweazle host tool reports some hardware failures, such as a
write-protected disk, with `Command Failed` and an exit status of zero. The
client reads the output as well as the exit status, and reports those as
failures with a specific status (`write-protected`, `no-disk`).

Cancel sends SIGINT to gw, which stops at the end of the current track, and
kills it if it has not stopped after ten seconds. A process started in the
background from a shell has SIGINT ignored, and its children inherit that, so
in that case the runner starts gw through a short Python step that restores
the default action first (`greaseweazle/runner.py`).

### IPF and the SPS Decoder Library

gw 1.23 reads IPF images through the SPS Decoder Library (CAPSImg), which it
opens with ctypes by its soname, `libcapsimage.so.5` first. The library's
licence allows only non-commercial use and redistribution, so it is not
shipped. `greaseweazle/caps.py` is the one place that decides where it comes
from:

- A copy the dynamic loader finds without help (`ctypes.util.find_library`,
  or one of gw's names in the loader's default folders or in the
  `LD_LIBRARY_PATH` PirateFinder was started with) is used as it is.
- Otherwise Preferences offers to install FS-UAE's build of CAPSImg 5.1.3
  from fs-uae.net, pinned in `data/caps/capsimg.toml` (URL, size, SHA-256,
  the member path inside the tar.xz, the library's SHA-256, and the
  `platform.machine()` names it runs on). There is an x86-64 build and a
  32-bit ARM hard-float build for ARMv8 processors (`armv7l`, and `armv8l`,
  which is what `caps.machine()` calls a 32-bit Python on an `aarch64`
  kernel); FS-UAE has no Linux build for 64-bit ARM, so there the status is
  `UNSUPPORTED`.
- The install downloads with `online/http.py` into a temporary folder inside
  `~/.local/share/piratefinder/caps/`, checks the archive's size and SHA-256
  before opening it, reads only the pinned member (no archive path is used on
  disk), checks the library's SHA-256 and that its ELF class, byte order,
  machine and ARM float convention match the running Python, loads it and
  calls `CAPSInit` in a separate Python, and then renames it to
  `libcapsimage.so.5` (mode 0644) beside an `install.json` recording the
  release.
- The loader reads `LD_LIBRARY_PATH` only when a process starts, so the folder
  has to be in gw's own environment. Every gw run (`client.probe`,
  `client.write`) takes its environment from `client.gw_environment()`: the
  runner's minimal environment, plus `LD_LIBRARY_PATH` with the PirateFinder
  folder first when it holds the library, followed by the caller's value. The
  packaged `gw` launcher (`packaging/gw`) and the runner's interrupt step both
  exec with the environment they were given, so the variable reaches gw.
- `prepare()` refuses an IPF with `CapsStatus.problem` unless the status is
  `SYSTEM` or `INSTALLED`.

## Interface

One `Adw.ApplicationWindow` with an `Adw.HeaderBar` holding an
`Adw.ViewSwitcher` over four pages (Find, Queue, Library, History), and the
main menu on the right. Below 600 sp the switcher moves to a bar at the
bottom of the window.

### Find

The Find page (`ui/find_page.py`, with its parts in `ui/find_widgets.py`)
asks the backend for one page of results (`Backend.search_page`) on a worker
thread after every change of text, filter, sort, mode or page, and drops an
answer that arrives after a newer question. The table never sorts rows
itself.

- **Search entry**: searches 250 ms after typing stops. Every word must
  match some field of a row (title, disc, series, crew, people, platform,
  type, year, file names or notes), and a series with a number such as
  "a250" names a disc. Enter opens the first result, Escape clears the
  entry and Ctrl+F focuses it.
- **Titles and Discs**: two linked toggle buttons beside the entry. Titles
  lists one row for each game, demo or program on a disc, and one for a
  disc that lists no titles; Discs lists one row for each disc.
- **Sort**: a drop-down with Relevance, Title A to Z, Title Z to A, Year
  Oldest First, Year Newest First, Disc Number, Crew and Platform. The
  Title, Disc, Crew, Year and Platform column headers sort as well: a
  second click on Title or Year reverses the order, and on the others goes
  back to Relevance. The drop-down and the headers always show the same
  order.
- **Filter bar**, which wraps onto a second line in a narrow window:
  Platform (Amiga or Atari ST), Type (Games, Applications, Demos or Music),
  Disc Kind (Menu Disks, Packs, Single Disks or Compilations), Crew (with a
  search field), Year, and Available Only, which keeps discs that are in the
  library or can be downloaded from an enabled provider. Type, Crew and Year
  offer the values the catalogue has, each with its number of discs
  (`Finder.facets`). Clear Filters appears in the bar while any filter is
  on, and on the No Results page.
- **Results table**: a `Gtk.ColumnView` with a tick box on each row. In
  Titles mode the columns are Title, Disc, Crew, Type (the title's kind),
  Year and Platform. In Discs mode they are Disc, Contents (up to six
  titles with the matching ones in bold, or the publisher and cracker of a
  single-game disc), Crew, Type (the disc's category), Year and Platform.
  Availability (Local, Online or Missing) comes last, and a warning icon
  column appears when a row on the page names a virus. The part of a word
  that a query word matches is in bold. Below 900 sp the Type column is
  hidden; below 600 sp Platform, Crew and Availability are hidden too.
- **Pager**: "101 to 200 of 1,234 titles", buttons for the first,
  previous, next and last pages, a page number entry, and Rows per Page
  (50, 100, 200 or 500; 100 to start with). Ctrl+Page Down and Ctrl+Page Up
  change page.
- **Ticks across pages**: a ticked row (by its tick box, or Space on the
  selected row) stays ticked when the page, sort, filters or text change.
  The action bar at the bottom counts the ticks ("3 titles selected on 2
  discs across 2 pages") and offers Clear, Add to Queue and Write Now.
  Ticked titles of one disc make one queue item, for the best dump of the
  disc. Write Now writes the discs straight away without adding them to the
  queue. Ctrl+Return and Ctrl+plus do the same, and act on the disc in the
  details pane when nothing is ticked.
- **Unmatched files bar**: after a search with text, an `Adw.Banner` above
  the results counts the library files that match no catalogue disc but
  match the search by file name, volume label or the names of the files on
  the disk. Show Files lists them in a dialog. Each can be added to the
  queue, and choosing one shows it in the details pane with its location,
  archive member, volume label, virus, checksums and the files on the disk.

With no text and no filter the page shows the size of the catalogue and up
to four example searches that give results, the last of them every disc of
the largest menu crew in disc order. Without a catalogue it offers Update
Catalogue.

### Details pane

Selecting a row opens the details pane (`ui/detail_pane.py`) on the right of
an `Adw.OverlaySplitView`. The pane is shown only while a row is selected,
and its left edge can be dragged to make it between 300 and 720 pixels wide.
Below 900 sp it lies over the results, and below 600 sp it covers the whole
width. Selecting another title of the same disc changes the pane without
loading the disc again. From the top:

- **Pictures** (`ui/media_view.py`): the selected title's pictures, then the
  disc's, with previous and next buttons, a caption such as "Menu screen of
  Automation 250, 1 of 3" and a credit that links to the source page. Each
  picture is downloaded when it is first shown and kept in the media cache;
  one narrower than 800 pixels is scaled up without smoothing. When
  pictures are switched off the space says so and offers Open Preferences.
- **Heading and buttons**: the title and its disc, or the disc label with
  its series, date, platform and kind; a warning when the catalogue lists
  the disc as damaged, intro only or missing; Write Now, Add to Queue, and a
  menu with Download Only, Show in Files, Copy Label Text, Edit Details and
  Revert to Catalogue (see User corrections). Write Now is off when no image
  of the disc is known. A download shows a progress bar
  with a Cancel button.
- **Virus card**: shown when the dump the writer would use carries a virus.
  It names the virus ("Virus Found: SCA" or "Dump Flagged with Saddam"),
  explains it and says who identified it. Remove Before Writing (on by
  default) is offered for a removable boot block virus, and Clean the Stored
  Image when that dump is a library file. When the catalogue lists a clean
  dump of the disc, Use Clean Dump chooses it in the Dumps list. The card,
  the file Clean the Stored Image cleans and the warning icon in the
  results all follow the same dump (`Finder._dump_to_write`, handed to the
  pane as `DiskDetail.write_local`).
- **Details**: platform, crew, disc, catalogue name, type, disc kind,
  release date (to the day when the catalogue knows it), publisher,
  cracker, the title's version and extras, condition, a Boot Block row, and
  where an image can be found. A fact the user corrected has a small Edited
  note whose tooltip gives the catalogue's value; so do a renamed title in On
  This Disc and corrected notes. Facts the catalogue does not have are left
  out, except the release date, which says Unknown. The Boot Block row shows,
  for information and without warning styling, a boot block on the dump that
  would be written that is not a virus: an anti-virus or immuniser, a named
  loader, or boot code nobody has identified (`fmt.boot_block_text`). For an
  unmatched library file the row appears once its boot block has been read
  on a worker thread.
- **On This Disc**: every title with its kind, cracker, publisher, version
  and extras. The title shown is marked; choosing another shows it and
  selects its row when that row is on the current page.
- **About the Crew**: the crew's history, founding date, members, source
  and Wikipedia article, when the catalogue has any.
- **Trivia**: the catalogue's facts and notes for the title and the disc,
  each with its source and licence, then Wikipedia summaries for the title,
  the disc and the crew, fetched in the background and credited under
  CC BY-SA 4.0. Text laid out in columns or drawn with symbols (a menu screen
  typed into a note, ASCII art, a FILE_ID.DIZ; `fmt.laid_out`) keeps its lines
  in a monospace font and scrolls sideways when it is wider than the pane.
- **Dumps**: every known dump with its format, flags, source and any virus,
  and where it is (in the library, or online at a provider). With more than
  one dump, radio buttons choose Best Available (the default) or one dump.
- **Notes and Credits** and **Scroll Text** (the menu's scroller text,
  `disks.menu_text`, in a monospace font), each in an expander, and **Links**
  to reference pages.

### Queue, Library and History

- **Queue**: disks waiting to be written, reorderable, with the drive choice
  and a Start button. While writing, the page shows the disk in progress,
  track progress, retries and a Cancel button. Before each disk an alert
  asks for the floppy to be inserted (Write, Skip, Stop). At the end a summary
  lists every disk with its result, verification, source and notes, and
  offers to retry the failures or copy a report. A disk queued with its boot
  block virus left in place says so.
- **Library**: the folders scanned, the download folder, the progress of a
  scan or of a boot block check (see Viruses), and counts (images, matched, not matched, duplicates, with a virus, last
  scan), a Viruses Found list and a list of unmatched files. Viruses Found
  shows up to 50 files whose boot block held a virus when it was last checked
  (`Library.infected_files`), each with the virus, where the file is and
  whether the virus can be removed; Clean removes a removable one after
  asking, and keeps the original (see Viruses below).
- **History**: past write sessions with their summaries.

An `Adw.Banner` appears when no Greaseweazle is connected. The window follows
the device every two seconds with `client.device_present`, which reads
`/sys/bus/usb/devices` (the Greaseweazle's USB id 1209:4d69, or a product name
gw also accepts), `/dev/greaseweazle`, `/dev/serial/by-id` or the port in the
Device setting; it starts no program and makes no network request. `gw info`
(`client.probe`) runs only when the device appears (once, and once more at the
next check if the first run found nothing, in case the port was not ready),
when the Device setting changes to a port that exists, and on Retry, Check
Connection and Check Again; never while a session is writing. A device that
goes away is shown as disconnected without running gw. Starting a session uses
the last answer, waiting for a check in progress. Each `gw info` that finds a
device also asks the GitHub API for the newest firmware; gw 1.23 has no option
to skip that, so while online use is switched off `client.probe` gives gw an
HTTPS proxy on 127.0.0.1 that refuses every connection (a port bound and never
listened on, held for the run), and the lookup fails without leaving the
computer (see [Privacy](PRIVACY.md)). Background events
(downloads finished, catalogue updated, scan complete) are announced with
`Adw.Toast`. Library folders are managed on the Library page; adding or
removing one starts a scan. Preferences use `Adw.PreferencesDialog` with
three pages. General: download folder, online downloads, Download
Screenshots and Background Information (which switches every picture and
summary download off, and is greyed out while online downloads are off),
and each provider. Greaseweazle: drive, device, retries, pre-erase, whether
to ask before each disk, a connection check, and IPF Support, which shows
whether gw can load the SPS Decoder Library (found on the system, installed by
PirateFinder with its version, not installed, or no build for this processor).
When it is not installed and a build exists, Install shows the licence in
an `Adw.AlertDialog` and downloads only after Accept and Install, with
progress and Cancel; Remove appears for a copy PirateFinder installed. A copy
found on the system offers neither. It sits on the Greaseweazle page because
the library is gw's dependency and decides what gw can write. Catalogue: the installed
catalogue, catalogue updates, and Virus Detection, which shows the Amiga
Bootblock Reader brainfile's version and downloads or updates it; the
library's boot blocks are then checked again. After a
session, disks that were written leave the queue and the rest stay for
another attempt.

## Online behaviour

Nothing is downloaded until the user asks for a disk that is not local.
Each provider can be switched off in Preferences, and online use can be
switched off entirely. The SPS Decoder Library for IPF is downloaded from
fs-uae.net only when the user accepts its licence under IPF Support, and is
checked against its pinned SHA-256 (see Writing). Every download is checked before it is kept
(`online/fetch.py`): against the location's own hash, else against the dump
the location is tied to, else against every dump the catalogue lists for the
disc (`ImageSource.dumps`, filled by `Finder.sources_for`). The last case
covers D-Bug and crew-list MSAs, exxos zips, amigascne files and
short-named Internet Archive menu zips, which carry no hash and name no dump;
the download is compared as the library matches its files
(`library.match_entry` over a `DumpSet` of the disc's dumps), saved under the
name of the dump it matched, and a note names that dump. A download that
fails its check is deleted and the next source is tried. Only when no dump of
the disc has a hash is the download kept unchecked; a note says so on the
Queue page while the disk is written, in the summary, the history and the
report, and after Download Only. The HTTP client identifies itself, keeps at
most one request a second per host, honours `Retry-After`, and backs off on 429 and
5xx responses. The Internet Archive serves single members from inside large
zip and 7z archives, which is how one disk is taken from a multi-gigabyte set.

The catalogue itself is updated by downloading a newer catalogue published as
a GitHub release asset named by its layout, `catalogue-layout<N>.sqlite.gz`
with `N` from `catalogue/schema.py` `SCHEMA_VERSION`, checked against its
published SHA-256 and its layout before it replaces the installed one. The
check (`catalogue/update.py`) reads the public release list and looks only for
the asset of its own layout. When the list cannot be fetched or read it raises
`UpdateError` with the reason, and the window says "Could not check for a newer
catalogue:" and the reason; "up to date" means the check worked. A catalogue
of another layout is refused with "made for a different PirateFinder version
(layout X; this version reads layout Y)" (`store.layout_problem`).

The application updates itself only when the user presses Check for
Application Updates in the About window (`app_update.py`, driven by
`ui/app_updater.py`). The check reads `releases/latest`, which is always the
newest application release because catalogue releases are published with
`--latest=false`, and compares its `vX.Y.Z` tag with `__version__`; a latest
release with any other tag is reported as a publishing mistake, never read as
"newest". `build-deb.sh` writes `/usr/lib/piratefinder/package-target` with
the package's distribution token and architecture, so the update downloads
`PirateFinder_<version>_<distro>_<arch>.deb` from the release, checks it
against the release's `SHA256SUMS`, and runs `pkexec apt-get install --yes`
on it. pkexec's status 126 (prompt dismissed) leaves the update offered; any
other failure gives the `sudo apt install` command to run by hand. A copy run
from the source tree has no `package-target` and is sent to the release page.
After the install, Restart PirateFinder sets `restart_requested` on the
application, and `__main__.main()` replaces the process with
`python3 -m piratefinder` once the window has closed, keeping the launcher's
environment. Neither an install nor a restart is allowed while disks are
being written, because the package replaces the bundled `gw`. `Adw.AboutDialog`
has no place for extra widgets, so `attach_to_about` finds the version button
by its `app-version` style class and adds the controls after it; a window
test fails if a libadwaita release moves it. Both updates share
`online/releases.py`, which turns every failure into `UpdateError` with the
reason.

Downloads are filed for archiving as
`<download folder>/<platform>/<type>/<crew>/<image>`, decided in
`archive_layout.py`. The type comes from the disk's contents: games, cheats and
documents make `Games`, utilities `Applications`, demos and intros `Demos`, and
music `Music`; intros, documents and cheats count only when a disk holds
nothing else, and a disk with no listed contents is filed by its kind. The crew
is the series' crew (`series.group_name`), or for a single disk its cracker,
then its publisher.

## Find screen, details pane and virus handling (catalogue layout 3)

The Find screen lists titles (one row per title on a disc) or discs, filtered
by platform, type, disc kind, crew, year and availability, searched with free
text across every field, sorted by any column and shown a page at a time. The
details pane on the right shows the disc of the selected row with its
pictures, facts, the other titles on it, crew history, trivia and any virus.

### Catalogue

Layout 3 (`catalogue/schema.py`) adds to layout 1, all filled by
`catalogue_builder`:

- `disks.category`, `disks.crew` (both from `archive_layout`, so the filters
  and the download folders agree), `disks.year`, `month`, `day` (the most
  precise release date any source gives; Demozoo and full TOSEC dates first)
  and `disks.sort_title`.
- `images.virus`, `virus_damage`, `antivirus` from the TOSEC `[v Name]`,
  `[b virus damage]` and `[m ... antivirus]` flags.
- `entries`: one row per title and one per disc without titles, and
  `entry_fts` over title, disc (label, series), crew, people (crackers,
  publishers, credits), facets (platform, type, disc kind and year as words),
  files (image file names) and notes. `disk_fts` gains the crew, facets and
  files columns.
- `crews` (history, members, founding date, Wikipedia article), with
  `disks.crew_id` naming the crew that made each disc, `media` (picture URLs
  with credit, per disc or per title) and `trivia` (facts, notes and
  Wikipedia article titles), all read by the app; pictures and summaries are
  fetched from the network only on demand.
- `address_prefix` and `media_credit`: location and picture addresses are
  stored as a shared beginning and the rest, and a picture's source and
  credit once per pair, which `Catalogue.locations` and `Catalogue.media`
  put back together.

Builder records gain `MediaRecordIn`, `TriviaRecordIn`, `CrewRecord`,
`ContentRecord.links`, `DiskRecord.media`, `trivia` and `release_date`, and
the image virus fields (`catalogue_builder/records.py`). A source module may
also expose `collect_crews(ctx) -> Iterable[CrewRecord]`. Media-only records
attach to discs by image name or to titles by normalised title.

### Runtime interfaces

```python
# catalogue/search.py
def search_page(catalogue, query: Query, *, local_disks=(), providers=(),
                overrides=None) -> CataloguePage
    # CataloguePage(rows: list[CatalogueRow], total: int)
    # CatalogueRow(disk_id, content_id, title, content_kind, score, matched)
    # title and matched are the corrected titles where the user renamed one
def facets(catalogue, overrides=None) -> Facets
# catalogue/store.py
Catalogue.media(disk_id) -> list[MediaItem]; Catalogue.trivia(disk_id) -> list[TriviaItem]
Catalogue.crew_for_disk(disk_id) -> CrewInfo | None   # via disks.crew_id: the crew that made the disc
# finder.py
Finder.search_page(query: Query) -> ResultPage
Finder.facets() -> Facets
Finder.detail(disk_id) -> DiskDetail            # with media, trivia, crew, virus, write_local
Finder.local_virus_report(local) -> VirusReport | None  # a library file's boot block, read now
Finder.summaries(disk_id, content_id=None) -> list[TriviaItem]   # Wikipedia, network
Finder.media_file(item: MediaItem) -> Path | None                # cached download
Finder.clean_alternates(disk_id) -> list[ImageRecord]  # dumps with no virus flag, best first
Finder.clean_file(local) -> LocalFile            # Library.clean_file with the download folder
# images/virus.py
def detect(raw: bytes, platform, *, catalogue_virus="") -> VirusReport   # never raises
def clean(raw: bytes, platform) -> bytes         # raises VirusError when not removable
def flagged(name, platform) -> VirusReport       # a catalogue flag on a dump that is not local
def install_brainfile(downloader=None, progress=None, cancel=None) -> Path
def brainfile_status() -> tuple[bool, str, int]
def fingerprint() -> str    # DETECTION_VERSION, the data/virus files and the brainfile
# images/prepare.py
def prepare(data, name, workdir, *, label, platform, clean_virus=False) -> PreparedImage
# library/library.py
Library.clean_file(local, *, download_folder=None, folders=DEFAULT_FOLDERS) -> LocalFile
Library.local_disks() -> set[int]
Library.recheck_boot_blocks(progress=None, cancel=None) -> BootRecheck | None
    # BootRecheck(checked, changed, unreadable, cancelled); None when nothing changed
# online/media.py
MediaCache.fetch(item: MediaItem, *, cancel=None) -> Path | None
MediaCache.wikipedia_summary(title, *, cancel=None) -> TriviaItem | None
```

Files and settings added since version 0.1.0:

- The user database is at version 5. Version 2: `library_files` gains
  `boot_status` and `boot_name`, and a `cleaned_files` table remembers which
  disc a cleaned file came from, because a cleaned image no longer matches
  any catalogue checksum. That upgrade makes the next scan read every file
  once, so boot blocks get checked; on a large network library that first
  scan is slow. Version 3: `session_items.notes` keeps the notes made while
  writing each disk, as a JSON list; older sessions read back with none.
  Version 4: `corrected_discs`, `corrections` and `title_corrections` replace
  the first `corrections` table, which was keyed by catalogue disc ids and
  was never written to (see User corrections). Version 5: a `meta` table,
  which holds the virus data fingerprint (`boot_fingerprint`); a database
  without one has its boot blocks checked again (see Viruses).
- The Amiga Bootblock Reader brainfile, when the user downloads it, lives in
  `~/.local/share/piratefinder/virus/abr/` with a `release.json` recording
  the release. Installing it changes the virus data fingerprint, so the
  library's boot blocks are checked again (see Viruses).
- Pictures and Wikipedia summaries are cached under
  `~/.cache/piratefinder/media/<source>/`, kept for 30 days and revalidated
  after that; missing pictures are remembered for 7 days.
- `Settings.fetch_media` ("Download screenshots and background
  information") switches all picture and summary downloads off. Online use as
  a whole switched off does the same.
- `Library.stats()` has an `infected` count for the Library page.
- `Finder.summaries()` returns the title's articles first, then the disc's,
  then the crew's.

`Query.text` is split into words; each word must match some field of the row
(FTS prefix match), so "automation necron 1990" finds Necron on Automation
250. A leading series alias with a number ("a250", "pp 51") becomes a series
and number filter as before. With no text, rows are ordered by the chosen
sort, relevance falling back to disc order. `total` counts every matching row
so the pager can show "101 to 200 of 1,234".

### Viruses

Detection runs on the raw sectors of every image the library scans and on
every image prepared for writing:

- Amiga: the boot block is checked for a valid checksum and compared with the
  standard Kickstart 1.3 and 2.0 install code (ignoring the unused tail).
  Any other block is looked up first in the Amiga Bootblock Reader brainfile
  when the user has downloaded it (by CRC, byte strings and its seven-byte
  recognisers); its classes decide the status: `v` is a virus, `oldav` a
  self-copying anti-virus, `s` a standard block, the rest named boot blocks.
  The brainfile has no licence, so it is not shipped; Preferences offers to
  download it from its GitHub release into the user's data folder, as WinUAE
  does. A block the brainfile does not name, or every block without it, is
  matched against the built-in data: 34 signatures in
  `data/virus/amiga-signatures.toml` (the SHA-1 of a window of virus code at
  a fixed offset, derived from samples; about 90 viruses, clones included),
  then the 83 marker rules in `amiga-markers.toml` (values at fixed offsets:
  25 from VirusX 4.0, 58 from AntiCicloVir 2.4; some need code patterns as
  well).
- Atari ST: an executable boot sector (word sum 0x1234) is matched against
  the signatures of 8 virus families in `data/virus/st-signatures.toml`
  (offset, length and hash of the virus code, never the whole sector).
  Immunisers and TOS boot loaders are recognised and left alone. A sector no
  signature matches that carries the marker of exactly one of the 7 viruses
  in `st-markers.toml` (numbers from the Ultimate Virus Killer book) and
  shows code patterns of a boot virus is reported as "probably" that virus
  and never cleaned; other unidentified boot code is reported for
  information only.
- Catalogue: a TOSEC `[v Name]` flag on the matched dump is shown even when
  the boot block is clean, because most flagged Amiga dumps carry file or
  link viruses (Saddam, BGS9) that live outside the boot block.

Removal is offered only for a known boot virus on a disk whose file system is
intact (an AmigaDOS root block at 880; an ST BPB that matches the image).
Amiga cleaning writes the standard install boot block for the disk's DOS
type, restores the root pointer and checksum. ST cleaning keeps the BPB,
clears the code area and makes the sector non-executable. The user chooses
"Remove before writing" (the default; the stored image is not changed) or
"Clean the stored image" (the file is rewritten and the original kept as a
backup). When the catalogue has a clean alternate dump of the same disc, the
details pane offers it instead. File and link viruses are flagged, never
"cleaned".

A library file's boot block is checked when it is scanned, and the status
and name are stored with it (`library_files.boot_status`, `boot_name`).
`images.virus.fingerprint()` hashes what else detection depends on:
`DETECTION_VERSION` (raised with any change to the detection code that
changes a result), the files in `data/virus` and the installed brainfile.
The user database keeps the fingerprint the boot blocks were checked with
(`meta.boot_fingerprint`). `Library.recheck_boot_blocks` compares them and,
when they differ, reads the boot block of every entry that has a status again
(the first kilobyte of a plain ADF or ST file; a packed image or archive
member is decoded with `inspect.boot_block`; nothing is hashed or matched),
stores the statuses that changed and then the new fingerprint. A cancelled
check keeps the old fingerprint, so it is done again; a file that cannot be
read keeps its status and is marked for a full read at the next scan. It is
the one mechanism for every change of virus data: the window runs it at
start and after the brainfile is installed (the Library page shows it as
Checking Boot Blocks, and a scan asked for meanwhile follows it), and
`Library.scan` runs it before scanning.

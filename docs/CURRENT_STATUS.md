# Current implementation status

Version 0.2.4, released on 14 September 2026, is the current release. It
adds Link to Disc, which counts a library file that matches no dump, such as
a menu disk downloaded by hand, as the disc the user chooses, and lists such
files on the disc's pane under Your Copies. The filter lists on the Find page
show whole names. Version 0.2.3, released the same day, finds a title under
either spelling of its sequel number and with or without the space between
two words, lists scene.org and Fujiology as download providers, reads the LZH
files of the Vectronix CD, keeps a download no checksum could check with its
disc, and bundles a catalogue that locates thousands more discs.
Version 0.2.2, released the same day, asks again for pictures a busy site
said it did not have, instead of leaving them out for a week. Version 0.2.1,
released on 13 September, added Download
All Pictures and lets an application update wait for the password prompt
however long it takes. Version 0.2.0, released on 13 September too,
brought the paged Find screen with filters and sorting, the details pane's
pictures, crew histories and trivia, virus detection, the illustrated User
Guide and Check for Application Updates. Version 0.1.0, released the day
before, was the first. This page
describes the source tree; [DESIGN.md](DESIGN.md) describes how the parts fit
together.

## Implemented

### Catalogue

- Catalogue builder (`python3 -m catalogue_builder`) with thirteen sources,
  twelve of them on by default (Demozoo is off); see
  [Data sources](DATA_SOURCES.md)
- Cached, throttled fetching with retry, offline builds and local inputs
- Series registry with aliases and per-source match rules in
  `data/series/*.toml`
- Merging of records from every source into one row per disk, part and
  version, with contents taken from the highest-priority source that has them
- Layout 3 (`SCHEMA_VERSION` 3 in `catalogue/schema.py`): a row per title
  for the Find screen, disc type, crew and release date to the day where a
  source gives it, TOSEC virus, virus damage and anti-virus flags on each
  dump, picture addresses, facts, notes, Wikipedia article titles and crew
  histories for the details pane, each disc pointing at its crew's history
  (`disks.crew_id`), and location and picture addresses and credits stored
  once each (`address_prefix`, `media_credit`)
- Full-text and substring indexes for search
- Crew histories chosen per platform and per crediting source, with pins in
  `data/crew-pins.toml`; a crew name that several groups share gives no
  history rather than another group's (the Atari ST menu crew Awesome and the
  Amiga demo group of that name each get their own)
- The amigascne archive's index and menu texts in every build, read from its
  scene.org mirror (`ftp.scene.org/mirrors/amigascne/`), since the archive's
  own server forbids automated access; the menu texts are cached for 30 days.
  The Demozoo export is read by the weekly Catalogue workflow
- Internet Archive files that carry older TOSEC names placed by the hashes of
  older TOSEC DATs (`data/old-tosec-dats.toml`), so their downloads are
  checked like any other; see [Data sources](DATA_SOURCES.md)
- Weekly catalogue build and publication by GitHub Actions, as a release asset
  named by its layout (`catalogue-layout3.sqlite.gz`, with its `.sha256` file,
  for the layout this source reads)
- In-app catalogue update from the newest published snapshot of the layout
  this version reads, from the public release list without an account,
  checked against its SHA-256 and layout before it replaces the installed
  catalogue; a check that cannot reach or read the release list says "Could
  not check for a newer catalogue" and why, never that the catalogue is up to
  date
- A catalogue of another layout refused with a message naming both layouts

The build of 14 September 2026 holds 61,792 discs in 1,395 series (12,396 menu
disks and compacts, 25,825 packs, 23,232 single disks and 339 compilations),
117,611 titles, 82,623 known dumps, 226,887 download locations, 207,232
picture addresses, 20,811 facts, notes and article titles, and 2,196 crew
histories. It takes 329 MB installed and 88 MB compressed. It has fewer discs
than the build of 13 September because packs that two sources listed under
different names are now one disc. It is the first build with the Vectronix
CD's locations.

### Finding discs

- Search across titles, discs, series, crews, credits, platform, type, year,
  image file names and notes, every word matching as a word start, with
  series aliases and disc numbers (`a250`, `pp 51`, `d-bug 100b`)
- Titles and Discs views, with the matching part of each word in bold
- Filters for platform, type, disc kind, crew and year, each listing its
  number of discs, and Available Only
- Sorting by relevance, title, year, disc number, crew and platform, from a
  list or the column headers
- The user's corrections in the search: text matches a corrected label,
  catalogue name, crew, date, publisher, cracker, notes or title name, the
  crew and year filters and their counts use the corrected crew and year,
  and the sort orders the corrected values
- Pages of 50, 100, 200 or 500 rows, with ticks kept across pages, sorts,
  filters and searches
- Library files that match no disc offered for a search that matches them
- Example searches on the welcome page, taken from the installed catalogue

### Details pane

- Pictures of the title and the disc, downloaded when shown, cached, credited
  and linked to their source, with small screens enlarged as square pixels
- Facts with the release date as precise as the sources give it, every title
  on the disc, the crew's history and members, trivia with its source and
  licence, and Wikipedia summaries fetched in the background
- A Boot Block fact for a boot block on the dump that would be written that
  is not a virus (anti-virus, named loader or unidentified code), shown for
  information; for an unmatched library file once the file has been read
- The menu's scroll text, and notes laid out like a menu screen or drawn with
  symbols, in a fixed-width font
- Every known dump with its source and location, and a choice of which to
  write
- Write Now, Add to Queue, Download Only (with an alert for its notes, such as
  a download that could not be checked), Show in Files and Copy Label Text
- Edit Details: the user's own label, catalogue name, crew, release date
  (`YYYY`, `YYYY-MM` or `YYYY-MM-DD`, checked as it is typed), publisher,
  cracker and notes for a disc, and names for its titles, kept in the user
  database and shown in the result rows, the details pane (each corrected
  fact marked Edited, with the catalogue's value in its tooltip) and the crew
  folder of a download; Revert to Catalogue forgets them
- Corrections that stay through catalogue updates: each corrected disc is
  found again in a new catalogue by its series and number or by the checksums
  of its dumps, since disc and title ids change with every build
- A setting that switches every picture and summary download off
- Download All Pictures in Preferences: every picture of the Atari ST, the
  Amiga or both fetched ahead of time into the same cache, one worker per
  site at the pane's pace of one request a second, with a count of what is
  already there, time and size estimates (about 12 hours and 3 GB for every
  picture), Stop, and a next run that starts from the cached pictures

### Viruses

- Amiga boot blocks checked against the standard Kickstart 1.3 and 2.0 boot
  blocks, then the Amiga Bootblock Reader brainfile, asked first once the
  user has downloaded it, then 34 built-in signatures of virus code that
  recognise about 90 boot block viruses (`data/virus/amiga-signatures.toml`),
  then 25 VirusX 4.0 and 58 AntiCicloVir 2.4 checks (`amiga-markers.toml`)
- Atari ST boot sectors checked against built-in signatures of 8 virus
  families (`data/virus/st-signatures.toml`), and the markers of 7 more
  viruses from the Ultimate Virus Killer book (`st-markers.toml`), reported
  as "probably" that virus and never cleaned, with immunisers and TOS boot
  loaders recognised
- TOSEC virus flags shown for the dump that would be written, in the results
  and the details pane
- Removal of an identified boot block virus from the written copy (the
  default), or from the stored file with the original kept as a backup
- A clean dump of the same disc offered when the catalogue lists one
- A list of the library files with a boot block virus, each with Clean
- The brainfile downloaded on request from its GitHub release
- The library's boot blocks checked again when the virus data changes: the
  user database keeps a fingerprint of the built-in virus data, the
  detection code's version and the installed brainfile, and when it differs
  (at start, before a scan, and after the brainfile is installed) every
  checked boot block is read again, without hashing the file; a file that
  cannot be read then is read in full by the next scan

### Library

- Incremental scans of library folders and the download folder, including
  local and mounted network folders, with cancellation
- Images inside zip, 7z, gzip and LZH archives, with one level of nesting
- Matching by raw sector hashes after decoding MSA, DMS, ADZ and unprotected
  STX, then by file hashes
- Boot block checks during the scan
- Unmatched files searchable by file name, volume label and the names of the
  files on Atari TOS and AmigaDOS disks
- A copy cleaned of a boot block virus, and a download of a disc whose dumps
  have no checksum, kept with their disc for as long as the file is
  unchanged, and found again after a catalogue update like corrections
- Link to Disc for a library file that matches no disc, such as a menu disk
  downloaded by hand, including an image inside an archive, with the disc
  chosen from a search; the disc's Your Copies list shows such files, each
  with Unlink
- Counts of images, matched and unmatched files, duplicates and files with a
  virus

### Downloads

- Downloads only on request, from providers that can each be switched off,
  with online use switchable as a whole
- An HTTP client that sends an identifying User-Agent, makes at most one
  request a second per host, honours `Retry-After`, backs off on 429 and 5xx,
  and resumes partial downloads
- Single members taken from large Internet Archive zip and 7z sets, and the
  Vectronix disks from the LZH files inside the Vectronix CD image
- Every download checked against the catalogue hash before it is kept, and
  deleted when it does not match, after which the next source is tried
- A download with no hash of its own and no dump tied to it (D-Bug and
  crew-list MSAs, exxos zips, amigascne files, short-named
  Internet Archive menu zips) checked against every dump the catalogue lists
  for its disc, with the library's matching; it is saved under the name of
  the dump it matched and a note names that dump. It is kept unchecked, with a
  note, only when no dump of the disc has a hash
- Downloads stored as `<platform>/<type>/<crew>/<image>` in a download folder
  the user chooses, which may be on a NAS

### Writing

- Preparation of ADF (80 to 84 cylinders, DD and HD), DMS, ADZ, gzip, ST (any
  layout the boot sector describes), MSA, unprotected STX, IPF (with the SPS
  Decoder Library), SCP and HFE, with generated disk definitions for
  non-standard layouts; see [Format support](FORMAT_SUPPORT.md)
- IPF Support in Preferences, Greaseweazle: says whether the SPS Decoder
  Library is found on the system, installed by PirateFinder or missing, and
  installs FS-UAE's CAPSImg 5.1.3 from fs-uae.net after the user accepts its
  licence, checked against a pinned SHA-256, as
  `~/.local/share/piratefinder/caps/libcapsimage.so.5`, or removes it. gw is
  started with that folder in `LD_LIBRARY_PATH`, and image preparation finds
  the library there. Builds exist for x86_64 and 32-bit ARM (the armhf
  package); arm64 has none, and the page says so
- A persistent write queue with reordering and copies, an insert-disk prompt
  before each disk (Write, Skip, Stop), and background download of the next
  online disk while one is written
- Live track progress, retries and verification from `gw write`
- Results read from the output of `gw`, so write-protected disks and empty
  drives are reported as such even when `gw` exits with status zero
- A session summary with retry of failed disks and a copyable report, and a
  history of past sessions, each listing the notes made while writing
  (conversions, a virus removed or left, a download that could not be checked)
- A banner when no Greaseweazle is connected, with a retry button
- The Greaseweazle followed every two seconds from the system's device list,
  without running a program or using the network; `gw info` runs only when a
  device appears, on Retry, Check Connection and Check Again, and never while
  writing
- The Device setting used for writing, for `gw info` and for the device check
- The firmware lookup `gw info` makes kept on the computer while online use is
  switched off: gw is given an HTTPS proxy on 127.0.0.1 that refuses every
  connection, since the host tools have no option to skip it; see
  [Privacy](PRIVACY.md)

### Help

- An in-app User Guide of fourteen topics with screenshots, opened from the
  main menu or with F1, and the same text published as
  [USER_GUIDE.md](USER_GUIDE.md), generated from one source and checked by a
  test
- A Keyboard Shortcuts window and a Diagnostic Log that can be copied or saved
- Screenshots drawn from the real window by `piratefinder.ui.screenshot`

### Distribution

- Ubuntu 24.04 (and Linux Mint 22) and Debian 13 `.deb` packages for amd64,
  arm64 and armhf, named `PirateFinder_<version>_<distro>_<arch>.deb`, each
  with the catalogue, a private copy of Greaseweazle Host Tools 1.23 whose
  compiled modules are built for that release's Python (3.12 or 3.13) and
  architecture, and the Greaseweazle udev rules; `packaging/build-deb.sh
  --distro ... --arch ... --container` builds any of them in a container of
  the target release and architecture, and `packaging/install-test.sh`
  installs one in a fresh container and starts it
- Release workflow that verifies the tag, bundles the newest published
  catalogue of the layout the source reads in all six packages (arm64 built
  on GitHub's Arm runners, armhf under qemu), install-tests each in a
  container of its own release and architecture, and publishes them with one
  `SHA256SUMS`; CI builds and install-tests Ubuntu 24.04 amd64 and Debian 13
  arm64 on every pull request
- A public repository: the package, its checksums and the catalogue releases
  download without a GitHub account
- Check for Application Updates in the About window, on request only: reads
  the latest release, downloads the package built for the same distribution
  and architecture (recorded in `/usr/lib/piratefinder/package-target`),
  checks it against `SHA256SUMS`, installs it with `pkexec apt-get install`
  and restarts PirateFinder; a copy run from source is sent to the release
  page

## Not yet done

- One layout has been written to a real floppy so far. On 14 September 2026
  PirateFinder 0.2.4's write session wrote Vectronix 834, an Atari ST disk of
  81 cylinders, 2 sides and 9 sectors, to a floppy in drive A (300 rpm) of a
  Greaseweazle F1 (firmware 1.6, host tools 1.23): it downloaded
  the disc, checked it against its TOSEC dump, generated the disk definition
  and wrote it, and gw verified every track with no retries. Read back with
  the packaged gw, all 1,458 sectors matched the TOSEC dump byte for byte.
  The recorded output is in `tests/fixtures/greaseweazle`. The other layouts
  and formats (80 cylinders with 9, 10 or 11 sectors, 82 and 83 cylinders,
  Amiga DD and HD, IPF, SCP and HFE) have been checked only against the real
  `gw` 1.23 tool without a Greaseweazle attached: generated disk definitions
  produce the same flux as gw's own formats, and every track of 82 and 83
  cylinder disks survives.
- Version 0.1.0 reads layout 1 and looks for any catalogue file in the
  releases, not only its own layout. The Catalogue workflow now publishes
  layout 3, so a 0.1.0 installation offers a new catalogue, downloads it and
  refuses it with "The new catalogue needs a newer version of PirateFinder."
  Its own catalogue stays in use. Released code cannot be changed; version
  0.2.0 looks only for its own layout, so upgrading ends the offers.
- Check for Application Updates first appears in 0.2.0. The owner's machine
  went from 0.2.0 to 0.2.1 with the installed 0.2.0's own check, download,
  checksum and pkexec code, run from a terminal rather than the About
  window; the button itself has been driven only in the window tests. 0.2.0
  was installed the same way from the source tree, which found the install
  time limit that 0.2.1 removes.
- IPF on arm64: the SPS Decoder Library, which cannot be bundled, has no
  Linux aarch64 build anywhere (fs-uae.net, the CAPSImg GitHub releases or
  FS-UAE's own arm64 package), so IPF Support says that there is no build for
  the processor and IPF images cannot be written there until one is
  published.

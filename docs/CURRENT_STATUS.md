# Current implementation status

The state of version 0.1.0, the first release. [DESIGN.md](DESIGN.md)
describes how the parts fit together.

## Implemented

### Catalogue

- Catalogue builder (`python3 -m catalogue_builder`) with eleven sources, nine
  of them on by default; see [Data sources](DATA_SOURCES.md)
- Cached, throttled fetching with retry, offline builds and local inputs
- Series registry with aliases and per-source match rules in
  `data/series/*.toml`
- Merging of records from every source into one row per disk, part and
  version, with contents taken from the highest-priority source that has them
- Full-text and substring indexes for search
- Weekly catalogue build and publication by GitHub Actions
- In-app catalogue update from the newest published snapshot, checked against
  its SHA-256 and schema version before it replaces the installed catalogue

### Finding disks

- Search by title, crew or series, disk number, or a combination, using series
  aliases, with platform and kind filters and an "available only" switch
- Result rows with the disk label, contents with matching titles in bold,
  platform and availability (local, online or missing), and multiple
  selection
- A detail pane with contents in menu order, credits, known dumps with their
  source and availability, and reference links

### Library

- Incremental scans of library folders and the download folder, including
  local and mounted network folders, with cancellation
- Images inside zip, 7z and gzip archives, with one level of nesting
- Matching by raw sector hashes after decoding MSA, DMS, ADZ and unprotected
  STX, then by file hashes
- Unmatched files searchable by file name, volume label and the names of the
  files on Atari TOS and AmigaDOS disks
- Counts of images, matched and unmatched files and duplicates

### Downloads

- Downloads only on request, from providers that can each be switched off,
  with online use switchable as a whole
- An HTTP client that sends an identifying User-Agent, makes at most one
  request a second per host, honours `Retry-After`, backs off on 429 and 5xx,
  and resumes partial downloads
- Single members taken from large Internet Archive zip and 7z sets
- Every download checked against the catalogue hash before it is kept, and
  deleted when it does not match; images with no known hash are kept and
  reported as unchecked
- Downloads stored as `<platform>/<type>/<crew>/<image>` in a download folder
  the user chooses, which may be on a NAS

### Writing

- Preparation of ADF (80 to 84 cylinders, DD and HD), DMS, ADZ, gzip, ST (any
  layout the boot sector describes), MSA, unprotected STX, IPF (with the CAPS
  library), SCP and HFE, with generated disk definitions for non-standard
  layouts; see [Format support](FORMAT_SUPPORT.md)
- A persistent write queue with reordering and copies, an insert-disk prompt
  before each disk (Write, Skip, Stop), and background download of the next
  online disk while one is written
- Live track progress, retries and verification from `gw write`
- Results read from the output of `gw`, so write-protected disks and empty
  drives are reported as such even when `gw` exits with status zero
- A session summary with retry of failed disks and a copyable report, and a
  history of past sessions
- A banner when no Greaseweazle is connected, with a retry button

### Distribution

- Ubuntu 24.04 and Linux Mint 22 amd64 `.deb` with the catalogue, a private
  copy of Greaseweazle Host Tools 1.23 and the Greaseweazle udev rules
- Release workflow that verifies the tag, bundles the newest published
  catalogue, install-tests the package and publishes it with SHA-256 checksums

## Not yet done

- The user database stores corrections to a disk's details and the search
  applies them, but the interface has no way to enter one yet.
- The Catalogue workflow has to publish a catalogue release before the first
  application release can be built.
- The Demozoo export and the amigascne.org menu texts are off by default
  because of their size and request count. The Catalogue workflow turns
  Demozoo on, so published catalogues list the contents of Amiga and ST demo
  packs; the menu texts (about 2,000 requests) are not yet included anywhere.
- About 9,500 Internet Archive files carry older TOSEC names that match no
  current catalogue image, so the builder drops them. Most of those disks have
  another download location.
- The kind buttons filter and count only the best 500 results of a search. A
  broad search (a single common word) can have more matches of one kind than
  the list shows.
- Writing has been checked against the real `gw` 1.23 tool without a
  Greaseweazle attached: generated disk definitions produce the same flux as
  gw's own formats, and every track of 82 and 83 cylinder disks survives. No
  write to a real floppy has been made yet.
- Only the amd64 Ubuntu 24.04 package is built. Other architectures and
  distribution families are planned; see [ROADMAP.md](../ROADMAP.md).
- IPF writing depends on the SPS CAPS library, which cannot be bundled and
  must be installed by the user.

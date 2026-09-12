# PirateFinder

## menu disk finder

A native GNOME application that keeps a searchable catalogue of what is on
Amiga and Atari ST menu disks, compacts and packs, finds the disk images in
your own folders or downloads them when you ask, and writes them to real
floppies with a Greaseweazle.

Crews on both machines released numbered disks that carried several cracked
games, demos or tools each: Automation, Pompey Pirates, Medway Boys and D-Bug
on the Atari ST, Skid Row and many others on the Amiga. Finding the one disk
that held a particular game means knowing which crew and which number.
PirateFinder looks that up in its catalogue, then gets the image and writes
it.

The current application:

- searches the catalogue by game title, crew or series, disk number, or any
  combination ("automation 250", "a250", "pp51", "d-bug 100b", "dungeon
  master"), with platform and kind filters and an "available only" switch;
- shows each disk's contents in menu order, with the matching titles in bold,
  credits, the known dumps of the disk and where each one can be found, and
  reference links, and lets you pick a particular dump to write or leave the
  choice to PirateFinder;
- writes one disk at once with **Write Now**, or several selected disks
  through the queue;
- scans local and network folders for disk images, including images inside
  zip, 7z and gzip archives, and matches them to catalogue disks by hash,
  whatever container they are stored in;
- keeps files it cannot match searchable by file name, volume label and the
  names of the files on the disk;
- downloads a missing disk from an enabled online provider only when you ask
  for it, checks it against the catalogue hash, and keeps it in a download
  folder you choose, which may be on a NAS;
- queues any number of disks, asks for each floppy to be inserted, writes it
  with the bundled Greaseweazle host tools, and reports track progress,
  retries and verification as it goes;
- prepares every supported image format for writing, including ST images with
  non-standard geometry, MSA, DMS, ADZ and unprotected STX, and refuses an
  image that cannot be written faithfully, saying why;
- ends each session with a summary of every disk, its result and its source,
  offers to retry the failures, and keeps the summaries in a history;
- updates the catalogue from published snapshots, checked against their
  SHA-256 before they replace the installed one.

## Screenshots

![Searching the catalogue on the Find page](docs/images/find.png)

![Disks waiting in the write queue](docs/images/queue.png)

![The summary at the end of a write session](docs/images/summary.png)

## Requirements

- Linux with Python 3.12 or newer (the release package uses Ubuntu's 3.12)
- GTK 4 and libadwaita 1.5 or newer, with PyGObject
- A Greaseweazle and a floppy drive to write disks (searching and the library
  work without one)
- `7z` from the 7zip package to read `.7z` archives

The release package supplies everything else, including the Greaseweazle host
tools. On Debian or Ubuntu the GNOME dependencies are `python3-gi`,
`gir1.2-gtk-4.0` and `gir1.2-adw-1`.

## Install a release

GitHub Releases provide a native `.deb` installer for 64-bit Ubuntu 24.04 and
Linux Mint 22. It contains PirateFinder, the newest catalogue at the time of
the release, Greaseweazle Host Tools 1.23 and the Greaseweazle device-access
rules. Python, GTK and libadwaita come from the distribution.

1. Download `PirateFinder_0.1.0_ubuntu24.04_amd64.deb` and `SHA256SUMS` from
   the matching GitHub Release.
2. From the download folder, run
   `sha256sum --check --ignore-missing SHA256SUMS`.
3. Install with `sudo apt install ./PirateFinder_0.1.0_ubuntu24.04_amd64.deb`.
4. Unplug and reconnect the Greaseweazle so the new device rule takes effect.
5. Open **PirateFinder** from the GNOME application grid, or run
   `piratefinder` from a terminal.

The package installs its device rule as `49-piratefinder-greaseweazle.rules`,
so it can be installed alongside Greaseweazle-GUI. Remove it with
`sudo apt remove piratefinder`. Your disk images, library index, history and
settings are not removed. See [Installation](docs/INSTALLATION.md) for
details.

## Run from the source tree

```sh
./piratefinder
```

The launcher adds `src` to `PYTHONPATH` and runs `python3 -m piratefinder`.
It needs the distribution's GTK 4, libadwaita and PyGObject packages, and the
Greaseweazle host tools to write disks: PirateFinder runs `PIRATEFINDER_GW`
when it is set, otherwise the first `gw` on `PATH`, otherwise the packaged copy
in `/usr/lib/piratefinder/bin`. From a source tree the catalogue is
read from `build/catalogue.sqlite`; build one as described below, or set
`PIRATEFINDER_CATALOGUE` to the path of a downloaded catalogue.

To work on the interface without a catalogue or hardware, start it with a
simulated back end:

```sh
PIRATEFINDER_FAKE_BACKEND=1 ./piratefinder
```

## Build the catalogue

```sh
PYTHONPATH=src python3 -m catalogue_builder
```

The builder reads the sources listed in [Data sources](docs/DATA_SOURCES.md),
joins their records per disk and writes `build/catalogue.sqlite`, together
with `catalogue.sqlite.gz` and a `.sha256` file for publishing. Downloads are
cached in `~/.cache/piratefinder-build`, so a second build fetches only what
has gone stale, and requests to each host are throttled. A source that fails
is logged and left out of the build.

Useful options:

| Option | Effect |
| --- | --- |
| `--list-sources` | List the sources, their priority and whether they run by default |
| `--with demozoo,amigascne-menus` | Also run sources that are off by default |
| `--only tosec,atari-legend` | Run only the named sources |
| `--skip exxos` | Leave a source out |
| `--offline` | Use cached downloads only |
| `--input tosec=PATH` | Read a source from a local file instead of downloading it |
| `--cache DIR`, `--output FILE` | Change the cache folder or the output file |
| `--strict` | Stop when a source fails |

Published catalogues are built the same way by a weekly GitHub Actions
workflow and attached to releases tagged `catalogue-YYYY-MM-DD`. See
[Catalogue](docs/CATALOGUE.md) for how records are merged into disks.

## Where images come from

**Library folders.** Add any number of folders on the Library page: local
directories, or network shares mounted through GNOME Files (SMB or NFS) or
`/etc/fstab`. PirateFinder scans them, and the download folder, for disk
images and archives, reads only files that changed since the last scan, and
never writes to them. Each image is decoded to raw sectors and hashed, so an
`.msa` matches the TOSEC entry for the same `.st` disk, and a `.dms` or `.adz`
matches its `.adf`. Network metadata folders such as `@eaDir` and `#recycle`
are skipped. See [Installation](docs/INSTALLATION.md) for mounting a NAS
share.

**Downloads.** When you write a disk that is not in your folders, PirateFinder
tries its known sources best first: local files, then each enabled provider
that has the disk. A download is taken out of its zip or 7z and checked
against the catalogue hash. An image that does not match is deleted and the
next source is tried. A kept image is stored as
`<download folder>/<platform>/<type>/<crew>/<image>` and added to the library
index at once, so the disk is local next time. The platform is `Amiga` or
`Atari ST`; the type is `Games`, `Applications`, `Demos` or `Music`, from what
the disk holds; the crew is the crew behind the series (Automation, Pompey
Pirates, Skid Row) or, for a single disk, whoever cracked it. For example:
`Atari ST/Games/Automation/Automation Menu Disk 250 (1990)(Automation).st`.
Keeping crews apart this way makes the download folder usable as an archive. When the catalogue
knows no hash for an image, it is kept and the session summary says that it
could not be checked. While one disk is being written, the next one in the
queue is downloaded in the background if it is only available online.

Each provider can be switched off in Preferences, and online use can be
switched off entirely. Nothing is downloaded until you ask for a disk that is
not local. The HTTP client names itself, keeps to one request a second per
host, and backs off when a server asks it to.

## In-app help

Choose **User Guide** from the main menu or press **F1**. The guide opens in a
window of its own, so it can stay open while you work. Its topics are Getting
Started, Searching, Queueing and Writing, Library Folders, Downloads and
Sources, Formats and Conversions, and Troubleshooting. **Keyboard Shortcuts**
lists the shortcuts. **Diagnostic Log** holds the output of the Greaseweazle
host tools and other messages from the current session, and can be copied
into a bug report.

## Supported image formats

| Format | Written as |
| --- | --- |
| `.adf` | Amiga DD or HD, including 81 to 84 cylinder images |
| `.dms`, `.adz`, `.gz` | Decoded to ADF, then as ADF |
| `.st` | Geometry from the boot sector, including 82 and 83 cylinder and 11 sector disks |
| `.msa` | Decoded to ST, then as ST |
| `.stx` | Converted to ST when the image carries no protection, otherwise refused |
| `.ipf` | Written when the CAPS library is installed, otherwise refused |
| `.scp`, `.hfe` | Written as flux; Greaseweazle cannot verify these writes |
| Inside `.zip`, `.7z` | The disk image member is read, then as above |

See [Format support](docs/FORMAT_SUPPORT.md) for how each format is prepared
and why some are refused.

## Data sources and licences

The catalogue is built from the TOSEC DAT pack (disk names and hashes),
Atari Legend's weekly database export (Atari ST menu disk contents, under
CC BY-NC-SA 4.0), the D-Bug search engine, the Steem Automation list, the crew
lists on the 1997 Pompey Pirates CD-R, the 8bitchip menu disk index, exxos's
Persistence of Vision pages, the amigascne.org pack disk index and Internet
Archive listings. The Demozoo export and the amigascne.org menu texts can be
switched on for a fuller build. Sites that block automated clients, such as
Janeway, the English Amiga Board and Hall of Light, are not read. Each source,
what is taken from it, its terms and how the builder limits its requests are
in [Data sources](docs/DATA_SOURCES.md).

## Copyright

The disks PirateFinder catalogues contain copyrighted software. PirateFinder
hosts no disk images. It downloads an image only when you ask for it, from
third-party archives that it does not operate. You are responsible for
complying with the law where you live.

## Documentation

- [Installation](docs/INSTALLATION.md)
- [Format support](docs/FORMAT_SUPPORT.md)
- [Data sources](docs/DATA_SOURCES.md)
- [Catalogue](docs/CATALOGUE.md)
- [Design](docs/DESIGN.md)
- [Current implementation status](docs/CURRENT_STATUS.md)
- [Maintainer release process](docs/RELEASING.md)
- [Contributing](CONTRIBUTING.md), [Security](SECURITY.md),
  [Support](SUPPORT.md), [Governance](GOVERNANCE.md) and
  [Code of conduct](CODE_OF_CONDUCT.md)
- [Third-party notices](THIRD_PARTY_NOTICES.md)

## Roadmap

See [ROADMAP.md](ROADMAP.md) for the planned catalogue, library, writing and
distribution work.

## Tests

```sh
PYTHONPATH=src:. python3 -m unittest discover -s tests -v
```

The tests build synthetic disk images at run time. No real disk image is
stored in the repository.

## Licence

The PirateFinder source code is licensed under GPL-3.0-or-later; see
[LICENSE](LICENSE). The catalogue data is licensed under CC BY-NC-SA 4.0,
because it includes Atari Legend data under that licence. The bundled
Greaseweazle host tools are released under the Unlicense. See
[NOTICE](NOTICE) and [Third-party notices](THIRD_PARTY_NOTICES.md).

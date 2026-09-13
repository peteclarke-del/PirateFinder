# Roadmap

PirateFinder is developed in stages. Each stage below ends with the condition
for calling it complete. No stage is complete while it reports a failure as
anything but a plain sentence, keeps an unchecked download, or can write to a
floppy the user has not inserted and confirmed.
[Current status](docs/CURRENT_STATUS.md) lists what works now and what does
not yet.

## 1. Catalogue

- [Complete] Build the catalogue from TOSEC, Atari Legend, D-Bug, Steem, the
  Pompey Pirates CD-R crew lists, 8bitchip, exxos, the amigascne archive
  (through its scene.org mirror) and the Internet Archive, with cached and
  throttled fetching.
- [Complete] Publish a new catalogue every week and update it from inside the
  application, checked against its SHA-256.
- [Complete] Demozoo pack membership in published catalogues. The weekly
  Catalogue workflow reads the Demozoo export.
- [Complete] Layout 3: a row per title, disc types and crews, release dates,
  TOSEC virus flags, and picture addresses, facts, Wikipedia article titles
  and crew histories from Atari Legend, D-Bug, Demozoo, libretro-thumbnails
  and Wikidata, with addresses and credits stored compactly.
- [Complete] amigascne menu texts in published catalogues, read from the
  scene.org mirror and cached for 30 days so the weekly build fetches them
  about once a month.
- [Complete] Name the layout in each catalogue release
  (`catalogue-layout<N>.sqlite.gz`), so an application that cannot read it
  does not download it.
- [Complete] Join crew histories to discs by platform and crediting source,
  so two groups of the same name are not confused.
- Add further crew lists and menu indexes as sources that permit automated
  access are found.

Complete when every numbered series in `data/series/series.toml` has contents
for most of its disks and at least one dump with a known hash.

## 2. Finding and reading

- [Complete] A Find screen that lists titles or discs a page at a time, with
  filters for platform, type, disc kind, crew, year and availability, and
  sorting by any column.
- [Complete] A details pane with pictures, facts, the titles on the disc, crew
  histories, trivia and Wikipedia summaries, each credited to its source.
- [Complete] An illustrated User Guide in the application and in
  [docs/USER_GUIDE.md](docs/USER_GUIDE.md), from one source.
- [Complete] Show boot blocks that are not viruses (anti-virus blocks,
  loaders and unidentified code) in the details pane for information.
- [Complete] Show menu scroll texts, and notes laid out like a menu screen or
  drawn with symbols, in a fixed-width font.

Complete when every fact the catalogue holds about a disc can be seen and
traced to its source from the details pane.

## 3. Library

- [Complete] Scan local and network folders incrementally, including zip, 7z
  and gzip archives with one level of nesting.
- [Complete] Match images by raw sector hashes whatever their container, and
  keep unmatched files searchable.
- [Complete] Check every boot block for viruses while scanning, list the files
  with one, and clean a stored image with the original kept as a backup.
- [Complete] Correct a disc's label, catalogue name, crew, release date,
  publisher, cracker, notes and title names with Edit Details, shown in the
  results and the details pane, used by the search, the filters and the sort
  orders, and kept through catalogue updates.
- [Complete] Built-in Atari ST and Amiga boot virus signatures and marker
  rules beyond Ghost, in `data/virus`; more are added as samples are found.
- [Complete] Check the library's boot blocks again when the built-in virus
  data, the detection code or the brainfile changes, reading only each boot
  block.

Complete when a large NAS collection can be scanned once, rescanned quickly,
and corrected where the catalogue is wrong.

## 4. Writing

- [Complete] Prepare every supported image format, with generated disk
  definitions for non-standard layouts.
- [Complete] Queue disks, prompt for each floppy, report progress, results and
  a summary, and keep a history.
- [Complete] Read write results from the output of `gw`, including hardware
  failures reported with a zero exit status.
- [Complete] Remove an identified boot block virus from the written copy.
- [Complete] Keep the notes made while writing (conversions, viruses removed,
  unchecked downloads) in the summary, the history and the report.
- [Complete] Check a download that has no hash of its own against every dump
  the catalogue lists for its disc, so only a disc with no known hash at all
  gives an unchecked download.
- [Complete] Use the Device setting for the connection check as well as for
  writing.
- [Complete] Follow the Greaseweazle from the system's device list, running
  `gw info` only when a device appears or the user asks, never while writing.
- [Complete] Keep the firmware lookup of `gw info` on the computer while online
  use is switched off.
- [Complete] Install the SPS Decoder Library (FS-UAE's CAPSImg 5.1.3) from
  Preferences after the user accepts its licence, checked against a pinned
  SHA-256, so IPF images are written on amd64 and armhf.
- IPF on arm64, once a Linux aarch64 build of the SPS Decoder Library is
  published; watch fs-uae.net for newer CAPSImg releases.
- Record real-hardware results for every format and layout in the tests'
  fixtures, starting with 82-cylinder ST disks and Amiga HD disks.

Complete when every format in [Format support](docs/FORMAT_SUPPORT.md) has
been written and verified on real hardware.

## 5. Distribution

- [Complete] Build an Ubuntu 24.04 and Linux Mint 22 amd64 `.deb` containing
  the application, the catalogue, pinned Greaseweazle host tools, desktop
  metadata and udev rules.
- [Complete] Verify the release tag, install-test the package and publish it
  with SHA-256 checksums through GitHub Actions.
- [Complete] Make the repository public, so releases can be downloaded
  without an account and the in-app catalogue update can see them.
- [Complete] Build and install-test Ubuntu 24.04 (and Linux Mint 22) and
  Debian 13 packages for amd64, arm64 and armhf, each in a container of its
  own release and architecture, named
  `PirateFinder_<version>_<distro>_<arch>.deb` and published with one
  `SHA256SUMS`. The next tag is the first release built this way.
- Packages for further distribution families, such as Fedora.

Complete when a tagged release produces a tested installer on every supported
system without a Python development environment.

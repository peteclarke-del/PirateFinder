# Roadmap

PirateFinder is developed in stages. Each stage below ends with the condition
for calling it complete. No stage is complete while it reports a failure as
anything but a plain sentence, keeps an unchecked download, or can write to a
floppy the user has not inserted and confirmed.

## 1. Catalogue

- [Complete] Build the catalogue from TOSEC, Atari Legend, D-Bug, Steem, the
  Pompey Pirates CD-R crew lists, 8bitchip, exxos, amigascne.org and the
  Internet Archive, with cached and throttled fetching.
- [Complete] Publish a new catalogue every week and update it from inside the
  application, checked against its SHA-256.
- [Complete] Demozoo pack membership in published catalogues. The weekly
  Catalogue workflow reads the Demozoo export.
- [In progress] amigascne.org menu texts are implemented and off by default.
  Decide how to include them in published catalogues without about 2,000
  requests every week.
- Add further crew lists and menu indexes as sources that permit automated
  access are found.

Complete when every numbered series in `data/series/series.toml` has contents
for most of its disks and at least one dump with a known hash.

## 2. Library

- [Complete] Scan local and network folders incrementally, including zip, 7z
  and gzip archives with one level of nesting.
- [Complete] Match images by raw sector hashes whatever their container, and
  keep unmatched files searchable.
- [In progress] User corrections are stored and applied to search results; the
  interface to enter them is still to come.

Complete when a large NAS collection can be scanned once, rescanned quickly,
and corrected where the catalogue is wrong.

## 3. Writing

- [Complete] Prepare every supported image format, with generated disk
  definitions for non-standard layouts.
- [Complete] Queue disks, prompt for each floppy, report progress, results and
  a summary, and keep a history.
- [Complete] Read write results from the output of `gw`, including hardware
  failures reported with a zero exit status.
- Record real-hardware results for every format and layout in the tests'
  fixtures, starting with 82-cylinder ST disks and Amiga HD disks.

Complete when every format in [Format support](docs/FORMAT_SUPPORT.md) has
been written and verified on real hardware.

## 4. Distribution

- [Complete] Build an Ubuntu 24.04 and Linux Mint 22 amd64 `.deb` containing
  the application, the catalogue, pinned Greaseweazle host tools, desktop
  metadata and udev rules.
- [Complete] Verify the release tag, install-test the package and publish it
  with SHA-256 checksums through GitHub Actions.
- Build arm64 packages and packages for further distribution families once
  they can be built and install-tested on their native runners.

Complete when a tagged release produces a tested installer on every supported
system without a Python development environment.

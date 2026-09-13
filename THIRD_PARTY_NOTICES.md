# Third-party notices

PirateFinder source is distributed under the GNU General Public License,
version 3 or later, in [LICENSE](LICENSE). The application also bundles,
copies or reads software and data governed by separate terms. This inventory
records those boundaries; it does not replace the licence text supplied by
each copyright holder.

## Greaseweazle host tools

The Debian package bundles a private copy of the Greaseweazle host tools under
`/usr/lib/piratefinder`, run through the wrapper
`/usr/lib/piratefinder/bin/gw`. The copy is built from the tagged upstream
source archive, which the package build rejects unless its SHA-256 matches the
reviewed value in `packaging/greaseweazle-source.sha256`.

| Component | Version | Licence | Project |
| --- | --- | --- | --- |
| Greaseweazle host tools | 1.23 | Unlicense | <https://github.com/keirf/greaseweazle> |

The package installs the upstream licence as
`/usr/share/doc/piratefinder/COPYING.greaseweazle`. The udev rules in
`packaging/49-piratefinder-greaseweazle.rules` are copied from the same
release.

The host tools need these Python packages, installed into the same private
library at the versions pinned in `packaging/runtime-requirements.txt`:

| Package | Version | Licence |
| --- | --- | --- |
| bitarray | 3.10.1 | PSF-2.0 |
| certifi | 2026.7.22 | MPL-2.0 |
| charset-normalizer | 3.5.1 | MIT |
| crcmod | 1.7 | MIT |
| idna | 3.19 | BSD-3-Clause |
| pyserial | 3.5 | BSD-3-Clause |
| requests | 2.34.2 | Apache-2.0 |
| urllib3 | 2.7.0 | MIT |

Each package's own licence file is kept in its `.dist-info` directory under
`/usr/lib/piratefinder`.

## Code copied from sibling projects

`src/piratefinder/images/vendor` holds modules copied from other projects by
the same author. Each file names its origin and licence in its first lines,
and the MIT licence text is reproduced in full in `vendor/__init__.py`.

| Files | Origin | Licence |
| --- | --- | --- |
| `floppy_geometry.py`, `msa.py`, `stx.py` | Atari File Forge | MIT, Copyright (c) 2026 Pete Clarke |
| `dms.py`, `dms_codec.py`, `checksum.py`, `errors.py` | Amiga File Forge | MIT, Copyright (c) 2026 Pete Clarke |
| `filesystems.py` | Greaseweazle-GUI | GPL-3.0-or-later |

The DMS decompressors in `dms_codec.py` are ports of xDMS 1.3 by Andre
Rodrigues de la Rocha, which its author placed in the public domain.

## Distribution packages

GTK 4, libadwaita, PyGObject and Python come from the distribution and keep
their own copyright records under `/usr/share/doc`. The package recommends
the distribution's `7zip` package, used to read `.7z` archives; it is not
bundled.

## Catalogue data

The catalogue (`catalogue.sqlite`, published as `catalogue-layout3.sqlite.gz`
for the layout this source reads) is
licensed under the Creative Commons Attribution-NonCommercial-ShareAlike 4.0
International licence (CC BY-NC-SA 4.0), because it includes data from Atari
Legend under that licence. Commercial redistribution of the catalogue is
therefore not permitted. The PirateFinder source code is not affected by this
and remains GPL-3.0-or-later.

Every source the catalogue builder reads, what is taken from it and its terms
are listed in [docs/DATA_SOURCES.md](docs/DATA_SOURCES.md). The catalogue also
records the sources and their licences in its `sources` table, which the
application shows.

## Boot virus recognition data

The files in `src/piratefinder/data/virus` hold facts about boot block
viruses: offsets, values and SHA-1 hashes of stretches of virus code. No
virus code, and no text from the sources below, is copied into them.

| File | Source | Terms |
| --- | --- | --- |
| `st-signatures.toml` | Windows of 8 virus families taken from the reference sectors in The Killer 2.0 and Xtermine 0.2 / The Exorcist, and from boot sectors kept with the Gen&Wax Visual Assembler sources | Facts only |
| `st-markers.toml` | Marker values of 7 viruses from appendix A of the Ultimate Virus Killer book by Richard Karsmakers | Facts only; the book's text is all rights reserved and is not reproduced |
| `amiga-signatures.toml` | 34 windows derived by PirateFinder from virus samples | GPL-3.0-or-later, as the rest of PirateFinder |
| `amiga-markers.toml` | 58 boot block checks from AntiCicloVir 2.4 by Matthias Gutt | Public domain |
| `amiga-markers.toml` | 25 boot block checks from VirusX 4.0 by Steve Tibbett and Dan James | "Copyrighted, but freely redistributable"; facts only |
| `virus-kinds.toml` | Where well-known Amiga viruses live, from the Virus Help Team's Amiga Virus Encyclopedia | Facts only |
| `indicators.toml` | Code patterns reported for information, after the virus probability factors in the Ultimate Virus Killer book | Facts only |

The Amiga Bootblock Reader brainfile, by Jason and Jordan Smith, has no
licence and is not shipped; Preferences downloads it from its GitHub release
into the user's data folder when the user asks.

## SPS Decoder Library (CAPSImg)

The Greaseweazle host tools read IPF images through the SPS Decoder Library,
Copyright (c) 2001-2014 István Fábián under exclusive licence to KryoFlux
Products & Services Ltd. Its licence (version 1.02, based on the MAME licence)
permits use and redistribution only free of charge and outside commercial
products and activities, requires redistributions to reproduce its notice,
and requires modified versions to come with their complete source.

PirateFinder does not ship the library, in the repository or in the package.
When the user accepts the licence under IPF Support in Preferences,
PirateFinder downloads the build that Frode Solheim makes for FS-UAE
(CAPSImg 5.1.3, from <https://fs-uae.net/plugins/>, source at
<https://github.com/FrodeSolheim/capsimg>) directly from fs-uae.net into the
user's data folder. The addresses and SHA-256 checksums of those builds are
recorded in `src/piratefinder/data/caps/capsimg.toml`.

The licence text is shipped, unchanged apart from its character encoding and
line endings, as `src/piratefinder/data/caps/CAPSImg-LICENCE.txt`, so that
it can be shown before the download. It is the text FS-UAE distributes with
the library and publishes in its repository; the licence asks for the notice
to be reproduced and places no restriction on copying the text itself.

## Disk images

PirateFinder contains no disk images and the repository never stores one;
tests build synthetic images at run time. Images found in the user's folders
or downloaded at the user's request from third-party archives keep the rights
of their authors and publishers. PirateFinder does not relicense them.

## Maintaining this inventory

A dependency update must record the new version, the upstream source, the
licence and any notice or source-offer obligation here and in the pinned
packaging files. A new catalogue source must be added to
[docs/DATA_SOURCES.md](docs/DATA_SOURCES.md) with its terms before its data is
published.

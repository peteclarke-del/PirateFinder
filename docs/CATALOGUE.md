# Catalogue

The catalogue is the SQLite file `catalogue.sqlite` that PirateFinder
searches. It lists every known disk (menu disks, compacts, packs,
compilations and single cracks for the Atari ST and the Amiga), what is on
each disk, every known dump of it with its hashes, and where a dump can be
downloaded. The application opens it read-only and never changes it.

Each source, its terms and its request limits are described in
[Data sources](DATA_SOURCES.md).

## Building

The builder is a separate program in `catalogue_builder/`. It needs Python
3.12 and nothing outside the standard library.

```
PYTHONPATH=src:. python3 -m catalogue_builder
```

writes `build/catalogue.sqlite`, then `build/catalogue.sqlite.gz` and
`build/catalogue.sqlite.gz.sha256` for publishing. The options are:

| Option | Effect |
| --- | --- |
| `--cache DIR` | Download cache, `~/.cache/piratefinder-build` by default |
| `--output FILE` | Catalogue file, `build/catalogue.sqlite` by default |
| `--offline` | Use cached downloads only; a missing download fails that source |
| `--only a,b` | Run only these sources |
| `--skip a,b` | Leave these sources out |
| `--with a,b` | Also run sources that are off by default |
| `--input id=PATH` | Read a source from a local file instead of downloading it |
| `--list-sources` | List the sources with their priority and default state |
| `--strict` | Stop at the first source that fails |

Sources are named by their id (`tosec`, `atari-legend`) or their module name
(`atarilegend`). A source that fails is logged, recorded in the catalogue's
`meta` table as failed, and left out; the rest of the catalogue is still
built unless `--strict` is given.

The catalogue is written to `<output>.tmp`, analysed and vacuumed, and then
renamed over the old file, so a failed build never leaves a half-written
catalogue behind.

A full build with a warm cache takes a few minutes. The first build downloads
the TOSEC DAT pack (about 100 MB) and the source dumps described in
[Data sources](DATA_SOURCES.md).

To try a catalogue in the application without installing it:

```
PIRATEFINDER_CATALOGUE=build/catalogue.sqlite ./piratefinder
```

Without that variable, the application uses the newest (by build date) of an
updated catalogue in `~/.local/share/piratefinder`, the packaged copy in
`/usr/share/piratefinder` and `build/catalogue.sqlite` in a source tree.

## Sources

Each module in `catalogue_builder/sources/` that defines `INFO` and
`collect(ctx)` is a source; the builder finds them itself. A module may also
set `CONTENT_PRIORITY` (lower wins when several sources list the contents of
a disk; 90 when unset) and `DEFAULT_ENABLED` (False for heavy optional
sources). `python3 -m catalogue_builder --list-sources` prints the current
list:

| Source | Priority | Default | Gives |
| --- | --- | --- | --- |
| `atari-legend` | 10 | on | ST menu contents, conditions, SHA-512, downloads |
| `d-bug` | 15 | on | Automation and D-Bug contents, credits, downloads |
| `exxos` | 30 | on | Persistence of Vision contents and downloads |
| `demozoo` | 35 | off | Amiga and ST pack membership |
| `steem` | 40 | on | Automation contents and part numbering |
| `crew-lists` | 50 | on | Pompey, Medway, FOF, Superior and Cynix lists |
| `8bitchip` | 60 | on | Game to disk index for Vectronix, SuperGAU and others |
| `tosec` | 90 | on | Disk names and image hashes |
| `internet-archive` | 90 | on | Download locations |
| `amigascne` | 90 | on | Amiga pack disk files |
| `amigascne-menus` | 90 | off | Amiga menu scroller texts |

### TOSEC DATs

The TOSEC importer finds the newest "DAT Pack - Complete" on
<https://www.tosecdev.org/downloads>, keeps it in the cache under its version
and reads the DATs straight from the zip. `--input tosec=PATH` accepts a
downloaded pack or a folder of DAT files. These DATs are read:

| DAT | Disks filed as |
| --- | --- |
| Atari ST - Compilations - Games - [ST] | menu |
| Atari ST - Compilations - Games - [STX] | menu |
| Atari ST - Compilations - Demos | pack |
| Atari ST - Compilations - Applications - [ST] | pack |
| Atari ST - Games - [ST] | single |
| Atari ST - Games - [STX] | single |
| Commodore Amiga - Compilations - Games | menu |
| Commodore Amiga - Compilations - Various | menu |
| Commodore Amiga - Compilations - Applications | pack |
| Commodore Amiga - Demos - Packs | pack |
| Commodore Amiga - Demos - Music | pack |
| Commodore Amiga - Games - [ADF] | single |

A disk in a declared series takes the kind of its series instead. In the
compilation DATs, an entry that joins two releases with " & " (for example
"Imperium (1990)(Electronic Arts)[cr Hotline] & Pyramax (1990)(Arc)") is a
compilation.

The other TOSEC DATs for these machines (applications, coverdisks, diskmags,
single demos, public domain and the CD sets) are not read: they hold few pirate
releases, and reading them would bury the menu disks in search results.

In the compilation DATs, the full TOSEC name of each entry is matched against
the rules in `data/series/match-tosec.toml`. A match gives the series and
number; the part comes from the rule or from the name's "(Part A)",
"(Part L Disk A)" or "(Disk 1 of 2)" field, and the version from its "v2.0"
suffix.

A numbered name that no rule knows, such as
"Intel Outside #12 (1995)(Ram Jam)", is taken for a series when the DAT holds
at least two numbers of it with "#", "No.", "Vol." or a word such as "Pack",
"Disk" or "Compil" in front of the number, or at least four numbers without
one. It joins a declared series whose name or alias it matches (when the
publisher agrees), or registers a series of its own named after the title and
the publisher.

Other disks are filed under their name. The dumps of one release (the same
title, version, date, publisher, media fields and cracker) collapse into one
disk with several images, so the alternates of a crack, its trained and
modified versions and its bad dumps sit together, ranked.

A single crack has one content item: the game, its publisher, its cracker and
its trainer. A compilation name lists its releases, and a name such as
"A-Ha Menu - Eliminator - Nebulus" lists the titles after its own name.
Series disks get their contents from the other sources.

Crack and trainer tags are expanded through `data/groups.toml`, so "[cr QTX]"
shows as Quartex; the search index keeps both spellings. Every ROM entry
becomes an image with its size, CRC32, MD5 and SHA-1, its TOSEC flags, and a
bad mark for "[b]" dumps.

## Merging

Every source produces `DiskRecord`s (`catalogue_builder/records.py`). The
merge step (`catalogue_builder/merge.py`) joins them into disks:

1. A record with a series and a number has a key: series, number, part and
   version. Records with the same key describe the same disk
   whichever source they come from. Parts are spelled one way ("Disk 1 of 2",
   "1" and "a" are all part A), and so are versions ("Version 2", "v2.0" and
   "bis" are v2; the first edition is unversioned, as TOSEC writes it).
2. Records without a key join the disk that already owns one of their
   images, compared by MD5, SHA-1, SHA-512, or CRC32 with size, on the same
   platform. A hash that two disks share is not used for joining. Otherwise
   the record becomes a disk of its own, labelled from its title.
3. Location-only records (no key, no images, only download locations)
   attach each location to the disk that owns an image with the same file
   name (compared without case, with or without the extension) or the same
   hash. Such a location never creates a disk: when it matches nothing it is
   counted and logged. Only a record with a title whose locations name no
   image at all (an archive of a whole disk, such as a DMS pack file known by
   its title) becomes a disk of its own.

Within a disk:

- Contents come from the best source that lists any: the one with the lowest
  `CONTENT_PRIORITY`. Entries are kept apart by title, kind and note, so a
  game, its documents and its cheat codes are all listed. The titles,
  publishers and notes of every source still go into the search index.
- Single values (title, date, publisher, cracker, condition) come from the
  first source, by priority, that gives one. Credits, notes and menu texts
  from all sources are kept, each once.
- Images that share a hash are one image, completed with the hashes each
  source knows. Images are ranked: verified dumps ("[!]") first, then clean
  dumps, then alternates in order ("[a]", "[a2]"), then modified, hacked,
  fixed, trained or virus-infected dumps, and bad dumps last. Among equals the
  source with the lowest priority comes first.
- Labels come from the series label format, for example "Automation 250",
  "D-Bug 100 B" or "Pompey Pirates 1 v2". A disk without a series is labelled
  from its TOSEC name without its fields: "The Chaos Engine (Disk 1 of 2) [cr
  Cynix]".
- Locations keep the size of the downloaded object when the source knows
  it. For some sources that is the image, for others the archive that holds
  it, so the size is advisory: what a download must match is the image hash.

The builder logs the number of disks per platform and kind, and how many have
contents, images and locations, with the number of locations that matched no
disk.

## Tables

The layout is in `src/piratefinder/catalogue/schema.py`.

| Table | Holds |
| --- | --- |
| `series`, `series_alias` | Series with platform, kind and group; normalised aliases |
| `disks` | One row per disk, part and version, with its label and details |
| `contents` | What is on each disk, in menu order, with the source it came from |
| `images` | Every known dump with its hashes, flags and rank |
| `locations` | Download URLs, the container and member, the hash to check |
| `links` | Reference pages |
| `disk_fts` | Full-text index: label, series, contents, people, notes |
| `disk_trigram` | Substring index over the label and content titles |
| `sources` | Each source with its licence, date read and record count |
| `meta` | Layout version, build date, builder version, source status, licence |

## Searching

`catalogue/search.py` reads a query in two ways.

- A query that starts with a series name or alias and goes on with a number
  is a disk reference: "automation 250",
  "Automation #250", "auto250", "a250", "pp51", "pompey pirates menu disk 51",
  "d-bug 100b", "dbug 100 part b", "skid row compact 128", "automation 100
  v2". The longest alias wins. The disks of that number, every part and
  version unless one is given, come first; any words left over are searched
  as text.
- Any other query searches the full-text index. Every word must match,
  as a prefix, and columns are weighted: label 10, contents 8, series 6,
  people 3, notes 1. When fewer than 20 disks match, words of three letters
  or more are also matched anywhere inside the label and content titles,
  ranked below the full-text hits.

Platform and kind filters apply to every kind of hit. Each hit lists the
content titles of the disk that contain a query word, so the interface can
show why it matched.

## Licence

The catalogue as a whole is published under the Creative Commons
Attribution-NonCommercial-ShareAlike 4.0 licence (CC BY-NC-SA 4.0), because
it includes Atari Legend data published under that licence. The `sources`
table records the licence or terms of each source, and the `meta` table the
licence of the whole file. The builder and the application are
GPL-3.0-or-later.

## Adding a series

Series are data, not code. To add one:

1. Declare it in `data/series/series.toml`, or in a file of its own in
   `data/series/`:

   ```toml
   [[series]]
   id = "example-crew"            # lower case words joined by hyphens
   name = "Example Crew"
   platform = "atari-st"          # or "amiga"
   kind = "menu"                  # menu, pack, single or compilation
   group = "Example Crew"
   label = "Example Crew {number}{part_suffix}{version_suffix}"
   aliases = ["ec", "example", "example crew menu"]
   ```

   `label` may use `{name}`, `{number}`, `{number3}` (three digits),
   `{part}`, `{part_suffix}`, `{version}` and `{version_suffix}`. Aliases are
   what people type in front of a disk number; they are compared after
   normalisation, so case and punctuation do not matter.

2. Tell each source how it names the series, in
   `data/series/match-<source>.toml`:

   ```toml
   [[match]]
   series = "example-crew"
   source = "tosec"
   patterns = ['^Example Crew Menu Disk (?P<number>\d+)(?: (?P<version>v\d+(?:\.\d+)*))? \(']
   ```

   Patterns are Python regular expressions, matched without case. The named
   group `number` is required; `part` and `version` are optional. TOSEC
   patterns see the full TOSEC name, fields included, so a pattern can check
   the publisher: `\([^)]*\)\(Skid Row` tells the Skid Row compacts from
   another crew's.

3. Add the TOSEC names to `tests/test_builder_series.py`, run the tests, and
   rebuild the catalogue:

   ```
   PYTHONPATH=src:. python3 -m unittest discover -s tests
   PYTHONPATH=src:. python3 -m catalogue_builder
   ```

Crew abbreviations used in crack and trainer tags go in `data/groups.toml`,
with the name to show and the tags and spellings that mean it.

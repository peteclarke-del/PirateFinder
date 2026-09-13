# Catalogue

The catalogue is the SQLite file `catalogue.sqlite` that PirateFinder
searches. It lists every known disk (menu disks, compacts, packs,
compilations and single cracks for the Atari ST and the Amiga), what is on
each disk, every known dump of it with its hashes and virus flags, and where
a dump can be downloaded. For the details pane it also holds links to
pictures of disks and titles, facts and notes, Wikipedia article titles and
crew histories. The application opens it read-only and never changes it.

Each source, its terms and its request limits are described in
[Data sources](DATA_SOURCES.md).

## Building

The builder is a separate program in `catalogue_builder/`. It needs Python
3.12 and nothing outside the standard library.

```
PYTHONPATH=src:. python3 -m catalogue_builder
```

writes `build/catalogue.sqlite`, then `build/catalogue.sqlite.gz` and
`build/catalogue.sqlite.gz.sha256` for publishing. The Catalogue workflow
publishes the compressed file named by its layout, as
`catalogue-layout3.sqlite.gz` for layout 3, the `SCHEMA_VERSION` of this
source (see [Release process](RELEASING.md)). The options are:

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
the TOSEC DAT pack (about 100 MB), the older TOSEC DATs the Internet Archive
importer reads (about 140 MB, once), the source dumps described in
[Data sources](DATA_SOURCES.md), and about 2,000 amigascne menu texts, one
request each 1.5 seconds apart (about 50 minutes, then cached for 30 days).

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
sources), and define `collect_crews(ctx)`, which returns `CrewRecord`s for
the `crews` table: one record per crew of that source, with the source's own
id for the crew (`id`) and the number of releases the source credits it with
on each platform (`platforms`). The source's disk records list the ids of
the crews it credits with each disk (`DiskRecord.crew_ids`). Atari Legend
gives every crew of its database, credited with the menu disks of the menu
sets it made (`crew_menu_set`); its crews are all Atari ST crews, counted by
those menu disks and the game releases they are credited with. Demozoo gives
every group with Amiga or Atari ST productions, credited with the packs and
menu intros it is an author of, and counted by its productions on each
platform. A source whose `collect_crews` fails keeps its disks; only its crew
histories are left out (with `--strict` the build stops).
`python3 -m catalogue_builder --list-sources` prints the current list:

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
| `internet-archive` | 90 | on | Download locations, placed by name, old TOSEC hash or series |
| `amigascne` | 90 | on | Amiga pack disk files (scene.org mirror) |
| `amigascne-menus` | 90 | on | Amiga menu scroller texts (scene.org mirror) |
| `libretro-thumbnails` | 95 | on | Box, title and in-game pictures of single games |
| `wikidata` | 95 | on | English Wikipedia article titles of games |

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
shows as Quartex; the search index keeps both spellings. A group entry with
`platforms` expands its tags only on those platforms, because a tag can mean
different crews on the two machines ("ICS" is the Italian Cracking Service
on the Amiga, and the name of an Atari ST menu crew). Besides the
well-known tags, the file holds the tags taken from Demozoo: a tag that
crack and trainer credits use on a platform, and that Demozoo gives as the
abbreviation of exactly one group with productions on that platform, where
at least three quarters of the dated discs credited to the tag fall within
that group's years there (a year either side allowed). A tag that is also
the name of a declared menu series on that platform is left as it is. Every ROM entry
becomes an image with its size, CRC32, MD5 and SHA-1, its TOSEC flags, and a
bad mark for "[b]" dumps.

Three kinds of flag describe viruses, and each image keeps them:

- `[v Name]` (also `[v2 Name]`) names the virus on the dump: `images.virus`
  is "Saddam 1" for `[v Saddam 1]`, and "unknown" for a bare `[v]`. A flag
  that only starts with a v, such as `[very hard levels]` or `[virus
  removed]`, is not a virus flag.
- "virus damage" in any flag (`[b virus damage]`, `[b corrupt file - virus
  damage]`, `[m virus damage]`) sets `images.virus_damage`. A `[b ...]` dump
  stays bad as well.
- A `[m X]` or `[a X]` flag whose text contains "antivirus", "anti-virus",
  "protector" or "virus free" names the anti-virus boot block installed on
  the dump: `images.antivirus` is "The Medway Boys Protector IV" for `[m The
  Medway Boys Protector IV]`.

In a compilation name, numbers joined by " & " belong to one title:
"Mercenary 1 & 2 Collection" is one release, and "Repton 1 & 2 & Editor"
lists "Repton 1 & 2" and "Editor".

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
   attach each location to the disk that owns an image with its hash, or,
   when it carries no hash, an image with the same file name (compared
   without case, with or without the extension). A location with a hash is
   never attached by its name: the name may since have passed to another
   dump, which the download would not match. Such a location never creates
   a disk: when it matches nothing it is counted and logged. Only a record
   with a title whose locations name no image at all (an archive of a whole
   disk, such as a DMS pack file known by its title) becomes a disk of its
   own. A keyed record marked `attach_only` (a menu zip the Internet Archive
   importer recognised by a series rule) joins the disk with its key when
   another record made one; otherwise it is dropped and its locations are
   counted as unmatched.
4. Media-only records (no key, images, contents or locations, only pictures
   and notes) never create a disk. A picture with an `image_name` goes to the
   disk that owns an image of that file name, compared as for locations. A
   picture with a `title_key` goes to every title on the record's platform
   whose title key equals it; when that title is the only one on a single
   disk, the picture is also filed for the disk. A note (`TriviaRecordIn`)
   in a media-only record attaches by its `content_title`, read as a title
   key, to every title on the record's platform with that key. Pictures and
   notes that match nothing are counted and logged. The title key is
   `naming.title_key`: the title without anything from its first bracket on
   and without a trailing version, with a trailing article moved to the
   front, normalised ("Chaos Engine, The (Europe)" is "the chaos engine").
   Importers use the same function.

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
  A location keeps a hash of its own only when the image it is attached to
  lacks that hash; otherwise the download is checked against the image's
  hashes, which include it, and the hash is not stored twice.
- `category` and `crew` come from `piratefinder.archive_layout`, the same
  code that files downloads, so a filter and a download folder never
  disagree. The category is Games, Applications, Demos or Music from the
  shown contents (or from the disk kind when it lists none). The crew of a
  series disk is the series group (Skid Row for Skid Row Compact); a disk
  without a series belongs to its cracker, then its publisher, then
  "Unknown crew".
- `year`, `month` and `day` are the most precise release date any source
  gives. A record's `release_date` ("1989", "1989-06" or "1989-06-17") wins
  over date texts; among release dates the most precise wins, and on a tie
  the source first by priority. Without any release date the date texts of
  every source are compared the same way. "19xx", "198x" and other partial
  years give no year; "1991-xx-xx" gives the year only. `date` keeps the text
  of the first source that gives one.
- `sort_title` is the label (for `entries`, the title) in its sort form: a
  leading English article is dropped (also when written at the end, as in
  "Chaos Engine, The"), case and punctuation do not count and numbers sort
  by value, so "The Chaos Engine" sorts under C and "Disk 9" before "Disk
  10". "A-Ha" keeps its A because a hyphen, not a space, follows it.
- Disk ids follow disc order: numbered disks first by series name, series,
  number, part and version, then every other disk by its sort title and
  platform. Entry ids follow disk ids and then menu order.
- Each shown title gets a row in `entries`, and a disk that lists no titles
  gets one row with no title id, named by its label.
- Titles from sources other than the shown one are matched to the shown
  title they stand for: the same title (normalised, with or without a
  trailing article) first, otherwise the shown title that shares the most
  words, when at least three quarters of the shorter title's words are in
  the other and at least 40 percent of all their words are shared ("Xenon
  II" finds "Xenon 2 - Megablast", "Chaos Strikes Back" does not find "The
  Chaos Engine"). A matched title, and the names in an "aka A; B" note, are
  searchable as that title, with the other source's cracker and publisher.
  A title that matches none is searchable in the notes of every title on
  the disk.
- Pictures (`media`) and notes (`trivia`) in a disk record belong to the
  disk, or to the shown title named by `content_title`, matched as above. A
  title's `("wikipedia", "Article")` link, from any source, becomes a
  `trivia` row of kind "wikipedia" whose text is the article title. The
  same picture URL is kept once per disk and title, and the same note once.
  A Wikipedia article is kept once per disk: when several titles of a disk
  have it (a game and its documents), it is kept for the first game-kind
  title among them, or else for the first of them. A note that repeats the
  disk's notes (compared normalised) is left out, so the details pane does
  not show it twice.
- Crew histories (from `collect_crews`) are never joined by name alone,
  because crews share names: "Awesome" made Atari ST menu disks, and an
  unrelated Amiga demo group had the same name. A crew record goes only to
  disks on a platform in its `platforms`, and only to disks whose crew has
  its name (compared normalised, without a leading "The" or apostrophes,
  with "&" read as "and", as written or after expanding an abbreviation
  from `data/groups.toml`). From each source a disk takes the record of
  the crew that source credits with the disk (`crew_ids`); without such a
  credit, the one record of that source with the disk's crew name and
  platform. When a source has several crews of that name on that platform
  and credits none of them with the disk, the crew pinned for that name,
  platform and source in `data/crew-pins.toml` wins; failing a pin, the
  crew with most of the releases the source credits to all crews of that
  name on that platform, when it has at least 90 percent of them and at
  least 10 (`dominant_share` and `dominant_releases` in the same file), so
  Demozoo's Paradox with 265 Amiga releases is the Paradox of the Amiga
  cracks, not its namesake with one. Otherwise the disk takes no history
  from that source, and the builder logs the names. A pin names the crew by
  the source's own id and says in a comment why it is right; a pin that
  chooses no crew in a build is logged, so a stale one is noticed.
  Disks with the same crew name, platform and records share one `crews`
  row, and `disks.crew_id` points at it (NULL when no source describes the
  crew). Notes from several sources are kept in priority order, members are
  pooled, and the founding date, page and Wikipedia article come from the
  first source that gives one. A crew record that describes the crew of no
  disk is counted and logged.

The builder logs the number of disks per platform and kind, and how many have
contents, images and locations, with the number of locations that matched no
disk. It also logs the number of entries, of disks with a year, month and
day, of images with a virus, virus damage or an anti-virus, and the media,
trivia and crew rows written and the records that matched nothing, the
disks with a crew history, the disks that took a pinned or dominant crew,
and the disks left without one source's history because that source has
several crews of their crew's name on their platform.

### Where the Internet Archive's files go

The Internet Archive importer places each zip it lists in one of three ways,
in this order:

1. By hash. Many Archive items copy TOSEC sets of years ago, under names
   TOSEC has since changed. The older DATs listed in
   `data/old-tosec-dats.toml` map those names to image hashes. For each item
   the DAT released nearest to the item's upload date is asked first, and
   the hash of the image it names (SHA-1, else MD5, else CRC32 with the
   image size) goes on the location. The merge attaches the location to the
   image with that hash, and the application checks the download against it.
2. By series. Menu zips with short names such as
   "[Menus]/P/Pompey Pirates/PP_054.zip" are matched against
   `data/series/match-internet-archive.toml` (the path without ".zip"), and
   become attach-only keyed records. Their locations carry no hash; the
   application checks a download against the disc's known dumps. Some rules
   name series that the Atari Legend importer registers from its set names;
   `data/series/extra-atari-legend-sets.toml` declares those exactly as that
   importer would, so the rules can name them and nothing else changes.
3. By name, as before: the zip's name with the image extension is taken for
   the TOSEC name of the image inside.

## Tables

The layout is in `src/piratefinder/catalogue/schema.py`.

| Table | Holds |
| --- | --- |
| `series`, `series_alias` | Series with platform, kind and group; normalised aliases |
| `disks` | One row per disk, part and version, with its label, details, category, crew, the `crews` row of that crew (`crew_id`), release year, month and day, and sort title |
| `contents` | What is on each disk, in menu order, with the source it came from |
| `images` | Every known dump with its hashes, flags, rank and virus flags (`virus`, `virus_damage`, `antivirus`) |
| `locations` | Download addresses (as prefix and rest), the container and member, the hash to check |
| `links` | Reference pages |
| `entries` | One row per shown title, and one per disk that lists none, with the title, sort title and kind |
| `entry_fts` | Full-text index per entry: title, disk, crew, people, facets, files, notes |
| `disk_fts` | Full-text index per disk: label, series, contents, people, notes, crew, facets, files |
| `disk_trigram` | Substring index over the label and content titles |
| `crews` | Notes, members, founding date, sources, page and Wikipedia article per crew name and platform as the disks carry them; read through `disks.crew_id`, never by name |
| `media` | Pictures per disk or title: addresses of the picture, its thumbnail and its page (as prefix and rest), kind, size, source and credit (through `media_credit`) and rank |
| `address_prefix` | Beginnings that many location and media addresses share, each stored once |
| `media_credit` | The source and credit line of media rows, each pair once |
| `trivia` | Facts, notes and Wikipedia article titles per disk or title, with source, page and licence |
| `sources` | Each source with its licence, date read and record count |
| `meta` | Layout version (3), build date, builder version, source status, licence |

Addresses are stored compactly. An address of a location or a picture is
stored as the longest beginning of it that ends in "/" and that at least 50
of the catalogue's addresses share (a row of `address_prefix`, id 0 being
the empty prefix) and the rest of it. A thumbnail whose rest is the same as
its picture's stores no rest. The source and credit line of a picture are one
`media_credit` row; the credit is a `str.format` template in which "{page}"
stands for the picture's page address without "https://" and a brace of the
credit is doubled, so the Demozoo credit "Demozoo contributors,
demozoo.org/productions/100/" is stored once as "Demozoo contributors,
{page}". `Catalogue.media` and `Catalogue.locations` put the values back
together, so the application sees whole addresses and credits. The search
indexes are not compacted.

The columns of the two full-text indexes hold:

| Column | Holds |
| --- | --- |
| `title` | The shown title, its "aka" names and the other sources' titles matched to it |
| `disk`, `label`, `series` | The disk label, the series name, group and aliases, and the disk's TOSEC name |
| `contents` | Every source's titles and notes (disk index only) |
| `crew` | The disk's crew, crackers and publishers, and the title's own, with every spelling |
| `people` | Credits |
| `facets` | Platform ("Amiga" and "Commodore", or "Atari ST"), category, disk kind ("menu disk compact", "pack", "single", "compilation"), title kind, year and "1989 06" |
| `files` | Image file names and download member names, without folders |
| `notes` | Notes and menu texts (each word once, since scroll texts repeat their words), the title's note and titles that match no shown title |

## Searching

The Find screen calls `search_page(catalogue, query, local_disks=...,
providers=..., overrides=...)`, which returns one page of rows and the number
of rows on every page. In TITLES mode a row is an entry (a title on a disk,
or a disk that lists none); in DISCS mode a row is a disk.

- A query that starts with a series name or alias and goes on with a
  number is a disk reference: "automation 250", "Automation #250",
  "auto250", "a250", "pp51", "pompey pirates menu disk 51", "d-bug 100b",
  "dbug 100 part b", "skid row compact 128", "automation 100 v2". The
  longest alias wins. The reference becomes a filter on the series and
  number, and on the part and version when a disk has them; otherwise every
  part and version is shown. Any other words must each match some column of
  the row, as the start of a word (a word of one character, or a number,
  matches only a whole word): "necron automation" finds Necron on
  Automation 250, and "1989 amiga speedball" finds Speedball on an Amiga disk
  from 1989. In a query of several words, "the", "a" and "an" are left out.
- A reference by an alias of three letters or more that is the whole query,
  such as "lemmings 2", also finds the rows its words match, after the rows
  of the reference, because it may name a game as well as a disk.
- A word of three letters or more that the full-text index does not know,
  and that is not a number, is looked for inside the disk's label and
  titles instead (the substring index); in TITLES mode the row's title or
  its disk label must contain it.
- Filters: platform, category, disk kinds, crew (an exact name from
  `facets`), year, and "available only", which keeps disks in
  `local_disks` or hosted by one of `providers`.
- Sorting: relevance, title (either way), year (either way, undated last,
  then title), disc, crew (then disc order) and platform (then title). Every
  order ends with the entry or disk id, so pages never repeat or skip a
  row. Relevance puts the rows of a disk reference first, then rows whose
  title (sort form) is the query, then titles that start with it, then the
  bm25 rank; in DISCS mode a disk whose own label is the query comes before
  a disk holding such a title. Relevance without text is disc order. When
  more than 20,000 rows match, relevance leaves out bm25, which ranks rows
  matching so common a word alike. The bm25 column weights are title 10,
  disk 5, crew 4, people 2, facets 2, files 1 and notes 1 for titles, and
  label 10, contents 8, series 6, crew 5, people 3, facets 2, notes 1 and
  files 1 for disks.
- `matched` names the titles that contain a query word: the row's own title
  in TITLES mode, up to eight titles of the disk in DISCS mode.
- `overrides` are the user's corrections (`search.Overrides`: each corrected
  disc as corrected, and each renamed title by content id). The crew and
  year filters and the title, year and crew sort keys use the corrected
  values, a row whose corrected text holds a query word is found when its
  catalogue text holds the others, and rows show corrected titles in `title`
  and `matched`. When no correction changes what a query looks at, its SQL
  is the same as without corrections. [Design](DESIGN.md#user-corrections)
  describes how.

`facets(catalogue, overrides=None)` lists the crews, years and categories
with the number of disks each has. The catalogue's counts are worked out once
per open catalogue; a disc whose crew or year was corrected counts under the
corrected value.

## Licence

The catalogue as a whole is published under the Creative Commons
Attribution-NonCommercial-ShareAlike 4.0 licence (CC BY-NC-SA 4.0), because
it includes Atari Legend data published under that licence. The `sources`
table records the licence or terms of each source, and the `meta` table the
licence of the whole file. The builder and the application are
GPL-3.0-or-later.

Every source states its licence or terms in its `INFO` (a test checks it),
so no `sources` row is empty:

| Source | Licence or terms |
| --- | --- |
| Atari Legend | CC BY-NC-SA 4.0 |
| TOSEC DAT pack | Freely distributed DAT files |
| Internet Archive | archive.org terms of use |
| Demozoo | No licence stated (daily public dump) |
| libretro-thumbnails | No licence stated (images are game graphics) |
| Wikidata | CC0 1.0 |
| Wikipedia | CC BY-SA 4.0 |
| D-Bug, Steem, exxos, 8bitchip, the crew lists, the amigascne archive and its menu texts | No licence stated; used under the site's terms, credit given |

Pictures and article texts are not in the catalogue. A `media` row holds the
address of a picture, the id of the source that lists it (`sources.id`), a
credit line to show under the picture and the page it comes from; the
application fetches the picture only when the details pane shows it. A
`trivia` row holds a fact or note with its source, page and `licence`, or,
for kind "wikipedia", only the title of an English Wikipedia article; the
application fetches the article summary when it is shown and credits it
under the CC BY-SA 4.0 licence of Wikipedia text. Wikipedia rows name the
source "wikipedia", which is not an importer; when any row names it the
builder adds a `sources` row for it (Wikipedia, https://en.wikipedia.org/,
CC BY-SA 4.0, with the number of rows). Crew rows name the sources their
notes come from. Pictures and notes from any other source whose id is not in
the `sources` table are logged by the builder, and their attribution is the
row's own credit or licence.

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
with the name to show, the tags and spellings that mean it and, when a tag
means the crew only on one machine, its `platforms`. When a source has
several crews of one name on a platform and the dominant-crew rule cannot
choose, a pin in `data/crew-pins.toml` can.

# Data sources

The PirateFinder catalogue is built by `python3 -m catalogue_builder` from
the public sources listed here. The catalogue records the same list in its
`sources` table, with the date each source was read and the number of records
taken from it.

PirateFinder hosts no disk images. The catalogue holds names, contents,
hashes and links. When the user asks for a disk that is not in their own
folders, the application downloads it from one of the locations below and
keeps it only if it matches the catalogue hash. A download that has no hash
of its own is kept only when it matches one of the known dumps of its disc.
It is kept unchecked, and the user is told so, only when no dump of the disc
has a hash.

## How the builder behaves

Every source fetches through one shared fetcher (`catalogue_builder/context.py`):

- each request carries the User-Agent
  `PirateFinder-catalogue-builder/0.1 (+https://github.com/peteclarke-del/PirateFinder)`;
- requests to one host are spaced at least as far apart as
  `data/fetch-hosts.toml` says, retries included. That file is the only
  place the intervals are set: one second for a host it does not list, 1.5
  seconds for the small sites, and at least the Crawl-delay a site's
  `robots.txt` asks for (5 seconds for www.tosecdev.org and
  www.exxosforum.co.uk, 10 seconds for the demozoo.org website). Hosts are
  compared exactly, so data.demozoo.org, which serves the Demozoo export and
  asks for no delay, is not the demozoo.org website;
- every response is stored in a cache and reused until it is older than the
  source's limit, usually 30 days; a dated dump or DAT pack that cannot change
  is never downloaded twice;
- a 429 or 5xx response is retried with a doubling delay, honouring
  `Retry-After`;
- the builder can run offline from the cache alone, and most sources accept a
  local copy of their input instead of a download.

The weekly GitHub Actions build keeps this cache between runs with
`actions/cache`, so building the published catalogue makes no more requests
than a local build.

## Sources used

### TOSEC DAT pack

- URL: <https://www.tosecdev.org/downloads>
- Used for: the list of disks and the names, sizes and hashes (CRC32, MD5,
  SHA-1) of every known dump, from the Atari ST and Amiga compilation, demo,
  application, pack and single-game DATs. TOSEC names give the series, number,
  part and version of numbered disks through the rules in
  `data/series/match-tosec.toml`, and the crack and trainer groups of single
  cracks.
- Terms: the TOSEC site states no licence for its DAT files. The DAT packs are
  published there for free download and are widely redistributed. The
  catalogue takes names, sizes and hashes from them. Recorded as "Freely
  distributed DAT files".
- Requests: the downloads page once a day at most, the listing of a pack
  version once a month, and the complete DAT pack once per version, five
  seconds apart (the site's Crawl-delay).

### Atari Legend

- URL: <https://www.atarilegend.com/data/database-dumps/>
- Used for: the contents of about 6,900 Atari ST menu disks (set, menu
  number, issue, version, disk part, condition, scroll text and what is on
  each disk), the SHA-512 of each menu disk's MSA dump, and the dump downloads
  as locations. Links to Demozoo productions are taken from the same data.
- Details pane: the menu's release date where the site has one (about 500
  disks, to the day); the addresses of the site's menu screenshots (about
  3,950, `storage/images/menu_screenshots/<id>.<ext>`, credited "Screenshot:
  Atari Legend (atarilegend.com), CC BY-NC-SA 4.0") and of up to three
  screenshots of each game on a menu (`storage/images/game_screenshots/`);
  the site's facts about those games and the editors' notes on the disks,
  as plain text; each game's Atari Legend id; and the crews behind the menu
  sets, with the history and members the site gives. A crew is named as the
  catalogue names the crew of those menus ("The Medway Boys" is the series
  group "Medway Boys").
- Terms: the site states that the content of the exports is available under
  the Creative Commons BY-NC-SA 4.0 licence. This is why the whole catalogue
  is published under CC BY-NC-SA 4.0. Facts and notes carry that licence in
  the catalogue, and every picture carries the credit above. Recorded as
  "CC BY-NC-SA 4.0".
- Requests: the listing of exports once a week at most, and the newest weekly
  MariaDB export (a few megabytes, with the users table already removed by the
  site). A dated export is never downloaded twice. The game image archives on
  the same page are not downloaded, and no screenshot is fetched by the
  builder: the application fetches a picture when it is shown.

### D-Bug search engine

- URL: <https://d-bug.me/>
- Used for: the contents of every Automation compact disk (0 to 512) and
  every D-Bug menu (1 to 200), with publisher, cracker and menu credits, part
  and version numbering, and download locations for the MSA files D-Bug hosts
  for its own menus.
- Details pane: the address of the menu screenshot the credits pages show
  for each disk (about 1,070, under `gfx/automenugfx/` and
  `gfx/dbugmenugfx/`), credited "Menu screenshot: D-Bug archive, d-bug.me
  (screenshots by Mr. Sam)".
- Terms: no licence is stated on the site. The data is used for reference and
  attributed to D-Bug. Recorded as "No licence stated; used under the site's
  terms, credit given", as for every site below that states no licence.
- Requests: one search page lists a whole group. Credits missing from that
  page are fetched one menu at a time. Requests are 1.5 seconds apart and
  every page is cached for 30 days. The screenshots are named on the pages
  already fetched, so they cost no extra request; the application fetches a
  picture when it is shown.

### Steem Automation catalogue

- URL: <http://steem.atari.st/automation.htm>
- Used for: a cross-check of Automation disk contents and part numbering,
  from Chris Edgar's 2002 test of every Automation disk image in the Steem
  emulator. Its download links point at an FTP server that no longer exists,
  so no locations are taken.
- Terms: no licence is stated. The page is used for reference. Recorded as
  "No licence stated; used under the site's terms, credit given".
- Requests: a single page, 1.5 seconds after any earlier request, cached for
  30 days.

### Crew lists on the Pompey Pirates CD-R

- URL: <https://archive.org/details/atari-st-collection-1997-cdr-alien-pompey-pirates>
- Used for: the crews' own lists of what is on every Pompey Pirates, Medway
  Boys, Flame of Finland, Superior, Cynix and Delight menu, the merged title
  index `MENUS/COMPLETE.TXT`, the Sewer Software doc disk list, and the MSA
  files on the CD-R as download locations. The CD-R was compiled in 1997 by
  Alien of the Pompey Pirates and is preserved on the Internet Archive as one
  ISO image.
- Terms: the Internet Archive's terms of use apply to the item; the lists
  themselves state no licence. Recorded as "No licence stated; used under the
  site's terms, credit given".
- Requests: the Archive serves single files from inside the ISO, so only the
  list files are read, one a second and cached for 30 days.

### 8bitchip menu disk index

- URL: <https://atari.8bitchip.info/MenuDG.html>
- Used for: a game to disk index (last revised in 2004) that is turned around
  into disk contents. It is the only source for the Vectronix disks, and it
  also covers SuperGAU, Fuzion and the doc disks.
- Terms: no licence is stated. The page is used for reference. Recorded as
  "No licence stated; used under the site's terms, credit given".
- Requests: a single page, 1.5 seconds after any earlier request, cached for
  30 days.

### exxos Atari pages

- URL: <https://www.exxosforum.co.uk/atari/>
- Used for: the contents of the Persistence of Vision demo compilation disks
  and the zip downloads exxos hosts for them.
- Terms: no licence is stated. The pages are used for reference and
  attributed to exxos. Recorded as "No licence stated; used under the site's
  terms, credit given".
- Requests: the gallery pages only, five seconds apart (the site's
  Crawl-delay), cached for 30 days.

### amigascne archive (scene.org mirror)

- URL: <https://ftp.scene.org/mirrors/amigascne/>
- Used for: download locations of some 11,000 Amiga pack disks (game
  compacts, demo, music and tool packs) and, for ADF files, their CRC32 and
  size, which identify the image the way a TOSEC hash does.
- Why the mirror: the archive's own server forbids automated access. Its
  `robots.txt` is "Disallow: /" for every client, and its `mirror.txt` says
  "Mirroring via ftp or http is strictly prohibited". The same tree
  (`amigascne-index.txt`, `Packdisks/`, `Scrollers/`) is on the scene.org
  mirror, whose `robots.txt` does not exist (so nothing is disallowed), and
  both the builder and the application use only the mirror: every download
  location points at ftp.scene.org.
- Terms: no licence is stated. The archive publishes a daily index of its
  files with their CRC32 and size. Recorded as "No licence stated; used under
  the site's terms, credit given".
- Requests: only the daily index file, 1.5 seconds after any earlier request,
  cached for 30 days. No pack disk is downloaded by the builder.

### amigascne menu texts (scene.org mirror)

- URL: <https://ftp.scene.org/mirrors/amigascne/Scrollers/>
- Used for: the text of pack disk menus and scrollers, stored as the menu
  text of the disk it belongs to, which is searchable and shown under Scroll
  Text in the details pane. A text is fetched only when it can be tied to a
  disk.
- Terms: as for the amigascne archive.
- Requests: one request per text. The archive holds about 3,600 menu texts,
  of which about 2,000 can be tied to a disk and are fetched, 1.5 seconds
  apart and cached for 30 days, so a build with a warm cache asks for none
  and a cold one takes about 50 minutes. The weekly catalogue build keeps the
  cache between runs, so it fetches the texts about once a month.
  `PIRATEFINDER_AMIGASCNE_MENUS_LIMIT` limits a trial run to the first few
  texts.

### Internet Archive

- URL: <https://archive.org>
- Used for: download locations of TOSEC-named Atari ST and Amiga disk images.
  Some items hold one zip per disk; others are single large zip or 7z files
  from which the Archive serves one member at a time, so one disk can be
  fetched without the whole set. No contents are taken from the Archive. A
  location is attached to the catalogue disk whose image has the same TOSEC
  name, and a download is checked against that image's hash.
- Older TOSEC names: many items copy TOSEC sets of years ago, and about 9,500
  of their files carry TOSEC names that have since changed. The builder reads
  older TOSEC DATs (`data/old-tosec-dats.toml`): the complete DAT packs of
  2012-09-15 and 2020-07-29, which are on the Internet Archive, and the Amiga
  games DAT of the TOSEC release of 2023-01-23 from the unofficial Git mirror
  of the TOSEC DATs (github.com/smesgr9000/TOSEC-DAT). An old DAT that knows
  a file's name gives the hash of that image, the location carries it, and
  the download is checked against it. For each item the DAT released nearest
  to the item's upload date is asked first.
- Short menu names: the `[Menus].7z` archive of the atari-st-collection item
  names some 3,200 menu zips by crew and number ("PP_054.zip"). The rules in
  `data/series/match-internet-archive.toml` read the series, number, part
  and version from the path, and the location joins that disc when another
  source describes it. These locations carry no hash; the application checks
  a download against the disc's known dumps.
- Terms: the Internet Archive's terms of use apply to the items it hosts.
  Items are uploaded by third parties. Recorded as "archive.org terms of use".
- Requests: the builder uses the Archive's metadata API, its scrape search API
  and the member listings of the large archives. Requests are one a second and
  cached for 30 days. The two old DAT packs (41 and 84 MB) and the old Amiga
  DAT (13 MB, one request to raw.githubusercontent.com) are downloaded once,
  checked against the SHA-1 the data file gives, and kept.

### Demozoo (off by default)

- URL: <https://data.demozoo.org/demozoo-export.sql.gz>
- Used for: the members of Amiga and Atari ST demo, music and intro packs, in
  order, from Demozoo's daily database export. Demozoo also lists the menus
  of many Atari ST crews as intros ("Automation CD #198 intro"); an intro
  whose title names a numbered disk of a menu series gives that disk its
  screenshot, release date and link (about 4,000 menus, through the rules in
  `data/series/match-demozoo.toml`).
- Details pane: release dates with Demozoo's precision (day, month or year);
  the addresses of screenshots on media.demozoo.org (up to three of a pack or
  menu as pictures of the disc, one of each pack member as a picture of that
  title), credited "Demozoo contributors, demozoo.org/productions/<id>/"; the
  productions' notes as plain text; and, for every group with Amiga or Atari
  ST productions, its notes, members and English Wikipedia article, named as
  the catalogue names the crew when a series group or a crew in
  `data/groups.toml` has the same name.
- Terms: Demozoo publishes a daily dump of its database "in the interest of an
  open data model" (Demozoo FAQ). No licence is stated for the data, so every
  picture and note is credited to Demozoo and links to its page. Recorded as
  "No licence stated (daily public dump)".
- Requests: the export is about 200 MB, so the importer is switched off by
  default. When it is switched on, the export is downloaded from
  data.demozoo.org at most once every 30 days, or read from a local copy given
  with `--input demozoo=FILE`. The builder fetches no screenshot and no page
  of the demozoo.org website; a source that did would be kept to the site's
  Crawl-delay of 10 seconds by `data/fetch-hosts.toml`.

### libretro-thumbnails

- URL: <https://github.com/libretro-thumbnails>
- Used for: the addresses of snaps, title screens and box art in the
  `Atari_-_ST` and `Commodore_-_Amiga` repositories (folders `Named_Snaps`,
  `Named_Titles` and `Named_Boxarts`), shown in the details pane and served
  from raw.githubusercontent.com. Atari ST pictures are named after the TOSEC
  name of the dump, with the characters `` &*/:`<>?\|" `` replaced by "_", and
  are attached to the disc whose dump has that name (about 86% of the single
  game disks). Amiga pictures have No-Intro style names ("Rick Dangerous
  (Europe)") and are attached by normalised title to every Amiga title of
  that name, one picture of each kind per title (about 69% of the single game
  disks).
- Terms: the repositories carry no licence file. Every picture is credited
  "Snap: libretro-thumbnails" (or "Title screen", "Box art"). Recorded as "No
  licence stated (images are game graphics)".
- Requests: one request per repository to the GitHub git trees API
  (`/repos/libretro-thumbnails/<repo>/git/trees/master?recursive=1`), cached
  for 30 days, so two requests a month. The builder fetches no picture.

### Wikidata

- URL: <https://query.wikidata.org/>
- Used for: the English Wikipedia article of a game, found through the ids
  Wikidata holds for it on Atari Legend (P4858), Hall of Light (P4671), Lemon
  Amiga (P4846) and the OpenRetro Game Database (P7683). Atari Legend games
  on menu disks get their article by id; other titles get it by name, on the
  platform of the id, when exactly one item has that name. The catalogue
  holds only the article title; the application fetches a summary from
  Wikipedia (CC BY-SA 4.0) when it is shown.
- Terms: Wikidata's data is CC0. Recorded as "CC0 1.0".
- Requests: one SPARQL query at build time for every item with one of those
  ids and an English article (about 1,700 items), sent with the builder's
  User-Agent and cached for 30 days. The Atari Legend importer reads the same
  cached answer. A local copy of the answer can be given with
  `--input wikidata=FILE`.

## Sources the application reads

The catalogue holds addresses and article titles, not pictures or article
text. The application fetches these while it runs, only when the details pane
shows a disc and only while Download Screenshots and Background Information
and Online Downloads are both on. [Privacy](PRIVACY.md) lists every request.

### Pictures

- From: Atari Legend (`atarilegend.com`), D-Bug (`d-bug.me`), Demozoo
  (`media.demozoo.org`) and libretro-thumbnails
  (`raw.githubusercontent.com`), at the addresses the sources above give.
- Shown with: the credit the catalogue stores for each picture, linked to its
  page.
- Requests: one picture at a time for each site, at least a second apart for
  Atari Legend, D-Bug and Demozoo. Each picture is kept in
  `~/.cache/piratefinder/media` for 30 days and then checked again with
  `If-None-Match` and `If-Modified-Since`. A picture the site does not have,
  or a reply that is not a PNG, GIF or JPEG of at most 8 MB, is not asked for
  again for 7 days.

### Wikipedia

- URL: <https://en.wikipedia.org/api/rest_v1/page/summary/>
- Used for: the summary of the English Wikipedia article about the title
  shown, the disc and the crew, when the catalogue names one (see Wikidata
  and Demozoo above). The article's thumbnail is not used, because its
  licence is not given with the summary. Disambiguation pages are skipped.
- Terms: Wikipedia text is licensed under CC BY-SA 4.0. Each summary is shown
  with "From Wikipedia", the article title, the licence and a link to the
  article.
- Requests: one per article, with a User-Agent that names PirateFinder and
  its purpose as the Wikimedia API asks, cached for 30 days.

### Amiga Bootblock Reader brainfile

- URL: <https://github.com/jasonthesmith79/AmigaBootBlockReader>
- Used for: naming Amiga boot blocks (viruses, anti-virus blocks, loaders and
  intros) by CRC, byte strings and recognisers. `brainfile.xml`,
  `searchbrain.xml` and `catlist.xml` are taken from the latest release. When
  it is installed it is asked first, before the virus data built into
  PirateFinder, and installing or updating it makes the application check
  the boot blocks of the library again.
- Terms: the brainfile, by Jason and Jordan Smith, is published without a
  licence, so it is not shipped with PirateFinder or included in the
  catalogue. Preferences offers to download it into
  `~/.local/share/piratefinder/virus/abr/`, as WinUAE does, and names it and
  its authors.
- Requests: only when the user chooses Download Brainfile or Update
  Brainfile: one request to the GitHub releases API and the release's zip
  file.

## Virus data in the source tree

`src/piratefinder/data/virus/` holds the tables the boot block checks read.
They hold facts about viruses (offsets, values and SHA-1 hashes of stretches
of virus code), never virus code, and no text of the works they come from:

- `st-signatures.toml`: signatures of 8 Atari ST boot sector virus families
  (Ghost, Signum/BPL, Kobold #2, Mad, OLI, C'T, Toubab and Blot/Swiss/FAT),
  each the SHA-1 of a stretch of virus code, taken from the reference sectors
  in the freeware ST anti-virus programs The Killer 2.0 and Xtermine 0.2
  (The Exorcist) and from boot sectors kept with published ST sources;
- `st-markers.toml`: the markers 7 Atari ST viruses leave on the disks they
  infect (Goblin, Evil, Grim Reaper, Macumba 5.2, P.M.S., Puke B and Zoch),
  from appendix A of the Ultimate Virus Killer book by Richard Karsmakers
  (the numbers only). A sector with one of them and code of the kind boot
  viruses use is reported as probably infected, and never cleaned;
- `amiga-signatures.toml`: 34 signatures of stretches of Amiga boot block
  virus code, derived by PirateFinder from samples of the viruses, which
  recognise about 90 viruses, clones that share code included;
- `amiga-markers.toml`: 25 checks from VirusX 4.0 by Steve Tibbett and Dan
  James (two longwords each) and 58 from AntiCicloVir 2.4 by Matthias Gutt,
  whose source is public domain (one longword each). None holds on any of
  318 labelled boot blocks that are not viruses, and each AntiCicloVir check
  kept also holds on a sample of the virus it names;
- `virus-kinds.toml`: where some well-known Amiga viruses live (boot block,
  files, links or system files), from the Virus Help Team's Amiga Virus
  Encyclopedia;
- `indicators.toml`: code patterns reported for information about boot code
  PirateFinder cannot identify, after the UVK book's virus probability
  factors for the ST.

The library keeps a fingerprint of these files, the detection code's version
and the installed brainfile. When it changes, the application checks the boot
block of every library file again, reading only the boot block.

## Sources not used

These sites are good references but block automated clients, so the builder
does not fetch from them:

- Janeway (Amiga demoscene database)
- English Amiga Board
- Hall of Light (Amiga games database)

A source is added only when its terms and `robots.txt` allow automated access,
and preferably when it publishes a dump or index rather than pages that have
to be crawled. See [CONTRIBUTING.md](../CONTRIBUTING.md).

## Licence of the catalogue

The catalogue database is published under CC BY-NC-SA 4.0, because it
includes Atari Legend data under that licence. It may be shared and adapted
for non-commercial purposes, with attribution and under the same licence. The
PirateFinder source code is GPL-3.0-or-later and is not affected. See
[THIRD_PARTY_NOTICES.md](../THIRD_PARTY_NOTICES.md).

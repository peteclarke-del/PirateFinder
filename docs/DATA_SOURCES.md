# Data sources

The PirateFinder catalogue is built by `python3 -m catalogue_builder` from
the public sources listed here. The catalogue records the same list in its
`sources` table, with the date each source was read and the number of records
taken from it.

PirateFinder hosts no disk images. The catalogue holds names, contents,
hashes and links. When the user asks for a disk that is not in their own
folders, the application downloads it from one of the locations below and
keeps it only if it matches the catalogue hash.

## How the builder behaves

Every source fetches through one shared fetcher (`catalogue_builder/context.py`):

- each request carries the User-Agent
  `PirateFinder-catalogue-builder/0.1 (+https://github.com/peteclarke-del/PirateFinder)`;
- requests to one host are spaced at least one second apart, and further apart
  where a source sets a longer interval (1.5 or 2 seconds for small sites);
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
  catalogue takes names, sizes and hashes from them.
- Requests: the downloads page once a day at most, the listing of a pack
  version once a month, and the complete DAT pack once per version, two
  seconds after any earlier request.

### Atari Legend

- URL: <https://www.atarilegend.com/data/database-dumps/>
- Used for: the contents of about 6,900 Atari ST menu disks (set, menu
  number, issue, version, disk part, condition, scroll text and what is on
  each disk), the SHA-512 of each menu disk's MSA dump, and the dump downloads
  as locations. Links to Demozoo productions are taken from the same data.
- Terms: the site states that the content of the exports is available under
  the Creative Commons BY-NC-SA 4.0 licence. This is why the whole catalogue
  is published under CC BY-NC-SA 4.0.
- Requests: the listing of exports once a week at most, and the newest weekly
  MariaDB export (a few megabytes, with the users table already removed by the
  site). A dated export is never downloaded twice. The game image archives on
  the same page are not downloaded.

### D-Bug search engine

- URL: <https://d-bug.me/>
- Used for: the contents of every Automation compact disk (0 to 512) and
  every D-Bug menu (1 to 200), with publisher, cracker and menu credits, part
  and version numbering, and download locations for the MSA files D-Bug hosts
  for its own menus.
- Terms: no licence is stated on the site. The data is used for reference and
  attributed to D-Bug.
- Requests: one search page lists a whole group. Credits missing from that
  page are fetched one menu at a time. Requests are 1.5 seconds apart and
  every page is cached for 30 days.

### Steem Automation catalogue

- URL: <http://steem.atari.st/automation.htm>
- Used for: a cross-check of Automation disk contents and part numbering,
  from Chris Edgar's 2002 test of every Automation disk image in the Steem
  emulator. Its download links point at an FTP server that no longer exists,
  so no locations are taken.
- Terms: no licence is stated. The page is used for reference.
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
- Terms: the Internet Archive's terms of use apply to the item.
- Requests: the Archive serves single files from inside the ISO, so only the
  list files are read, 1.5 seconds apart and cached for 30 days.

### 8bitchip menu disk index

- URL: <https://atari.8bitchip.info/MenuDG.html>
- Used for: a game to disk index (last revised in 2004) that is turned around
  into disk contents. It is the only source for the Vectronix disks, and it
  also covers SuperGAU, Fuzion and the doc disks.
- Terms: no licence is stated. The page is used for reference.
- Requests: a single page, 1.5 seconds after any earlier request, cached for
  30 days.

### exxos Atari pages

- URL: <https://www.exxosforum.co.uk/atari/>
- Used for: the contents of the Persistence of Vision demo compilation disks
  and the zip downloads exxos hosts for them.
- Terms: no licence is stated. The pages are used for reference and
  attributed to exxos.
- Requests: the gallery pages only, two seconds apart, cached for 30 days.

### amigascne.org archive

- URL: <http://ftp.amigascne.org/pub/amiga/>
- Used for: download locations of some 11,000 Amiga pack disks (game
  compacts, demo, music and tool packs) and, for ADF files, their CRC32 and
  size, which identify the image the way a TOSEC hash does.
- Terms: no licence is stated. The archive publishes a daily index of its
  files with their CRC32 and size.
- Requests: only the daily index file, 1.5 seconds after any earlier request,
  cached for 30 days. No pack disk is downloaded by the builder.

### amigascne.org menu texts (off by default)

- URL: <http://ftp.amigascne.org/pub/amiga/Scrollers/>
- Used for: the text of pack disk menus and scrollers, stored as searchable
  menu text for the disk it belongs to. A text is fetched only when it can be
  tied to a disk.
- Terms: as for the amigascne.org archive.
- Requests: one request per text. The archive holds about 3,600 menu texts,
  of which about 2,000 can be tied to a disk and are fetched, 1.5 seconds apart and
  cached for 30 days. Because of that number the importer is switched off by
  default. `PIRATEFINDER_AMIGASCNE_MENUS_LIMIT` limits a trial run to the first
  few texts.

### Internet Archive

- URL: <https://archive.org>
- Used for: download locations of TOSEC-named Atari ST and Amiga disk images.
  Some items hold one zip per disk; others are single large zip or 7z files
  from which the Archive serves one member at a time, so one disk can be
  fetched without the whole set. No contents or hashes are taken from the
  Archive; a location is attached to the catalogue disk whose image has the
  same TOSEC name, and a download is checked against that image's hash.
- Terms: the Internet Archive's terms of use apply to the items it hosts.
  Items are uploaded by third parties.
- Requests: the builder uses the Archive's metadata API, its scrape search API
  and the member listings of the large archives. Requests are one a second and
  cached for 30 days.

### Demozoo (off by default)

- URL: <https://data.demozoo.org/demozoo-export.sql.gz>
- Used for: the members of Amiga and Atari ST demo, music and intro packs, in
  order, from Demozoo's daily database export.
- Terms: Demozoo publishes a daily dump of its database "in the interest of an
  open data model" (Demozoo FAQ). No licence is stated for the data.
- Requests: the export is about 200 MB, so the importer is switched off by
  default. When it is switched on, the export is downloaded at most once every
  30 days, or read from a local copy given with `--input demozoo=FILE`.

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

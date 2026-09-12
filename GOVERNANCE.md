# Project governance

## Scope and roles

PirateFinder is a native GNOME application that catalogues Amiga and Atari ST
menu disks, compacts, packs and single cracks, finds or downloads their images
and writes them to floppy with Greaseweazle hardware. Pete Clarke
(`@peteclarke-del`) is the current maintainer and release owner. Contributors
may propose, implement, test and review changes; repository administration,
application releases and catalogue releases remain with the maintainer unless
this file is updated.

## Decision priorities

When these conflict, the earlier one wins:

1. Never write to a floppy the user has not inserted and confirmed, and never
   modify the user's own image files.
2. Keep only downloads that match the catalogue hash.
3. Respect the sites the catalogue is built from: their terms, their
   `robots.txt` and their bandwidth.
4. Fail closed when an image's format, geometry or origin is uncertain.
5. Prefer reproducible synthetic fixtures and real-hardware evidence.
6. Keep one implementation for each of matching, preparation and writing.
7. Maintain a clear, keyboard-operable GNOME interface.

Significant changes to catalogue sources, the catalogue schema, security,
licensing, dependencies, hardware support or release policy require an issue
before implementation. Normal changes are decided through pull-request review.

Only the maintainer creates releases. Security reports follow
[SECURITY.md](SECURITY.md); conduct concerns follow
[CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md).

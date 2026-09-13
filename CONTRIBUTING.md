# Contributing to PirateFinder

PirateFinder writes to physical floppies and fetches files from other people's
websites. No change may write to a floppy the user has not confirmed, or keep
a download that fails the catalogue check. The catalogue builder must stay
within the request limits of the sites it reads.

## Before starting

Open an issue for a new catalogue source, a new dependency, a change to the
catalogue schema or a workflow redesign. Do not attach disk images, archives,
private paths, credentials or device identifiers, and do not link to places
that host disk images. Report security defects according to
[SECURITY.md](SECURITY.md).

## Development workflow

1. Create a focused branch from current `main`.
2. Keep subprocess arguments as lists; never pass user input through a shell.
3. Treat disk images, archive members, file names, downloaded pages and
   Greaseweazle output as untrusted.
4. Add regression coverage for changed parsing, matching, preparation or
   writing logic. Tests build synthetic disk images at run time; no real disk
   image is ever committed.
5. Update the README, the documents under `docs/` and the in-app help when
   behaviour or terminology changes. The documentation describes what the code
   does now; planned work belongs in [ROADMAP.md](ROADMAP.md) and
   [docs/CURRENT_STATUS.md](docs/CURRENT_STATUS.md).
6. Submit a pull request using the repository template.

## Documentation and screenshots

The User Guide's text is in `src/piratefinder/ui/help_content.py`. The
application shows it in the User Guide window, and
[docs/USER_GUIDE.md](docs/USER_GUIDE.md) is generated from it:

```sh
PYTHONPATH=src python3 -m piratefinder.ui.help_content
```

A test fails when the two differ, and another when the guide names a picture
that does not exist. Write guide text in plain ASCII, put commands and paths
between backticks, and keep `<`, `>`, `*`, `|` and `!` out of the rest; the
tests check this.

The screenshots in `docs/images` and `src/piratefinder/data/help` are drawn
from the real window with the simulated back end, never taken by hand. With a
display, and a catalogue built as described in the README:

```sh
PYTHONPATH=src:. python3 -m piratefinder.ui.screenshot --catalogue build/catalogue.sqlite
```

Without a desktop session, a Broadway display serves as well:
`gtk4-broadwayd :5 &`, then run the command above with
`GDK_BACKEND=broadway BROADWAY_DISPLAY=:5`. With no browser attached,
Broadway draws about once a second and never resizes a window, so the tool
sets the font resolution itself, lays each picture out at its own size,
closes dialogs at once and draws the narrow picture in a window made at
that size.

Each state is saved in light and dark; the light pictures the guide shows are
copied into the package, and ones it no longer shows are removed. A new
picture needs a state in `src/piratefinder/ui/screenshot.py` and a reference
in `help_content.py`. Look at every picture after regenerating it. The
pictures in the details pane are drawn by the simulated back end; never add a
downloaded screenshot to the repository.

The documents follow the house style checked by `tests/test_documentation.py`:
no em or en dashes, ellipsis characters, arrows or curly quotes, British
spelling, and every document linked from the README or
[docs/README.md](docs/README.md).

## Tests and checks

Run before submitting:

```sh
PYTHONPATH=src:. python3 -m unittest discover -s tests -v
python3 -m compileall -q src catalogue_builder tests
ruff check src catalogue_builder tests
ruff format --check src catalogue_builder tests
bash -n piratefinder packaging/*.sh
sh -n packaging/piratefinder packaging/gw packaging/postinst packaging/postrm
desktop-file-validate data/com.github.pclarke.PirateFinder.desktop
appstreamcli validate --no-net data/com.github.pclarke.PirateFinder.metainfo.xml
```

CI installs ruff 0.16.4. Code under `src/piratefinder/images/vendor` is copied
from sibling projects and is excluded from ruff so it stays comparable with its
origin; see [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).

Image handling changes should include generated fixtures and negative cases
for truncation, corruption, unusual geometry and cancellation. Results from
real hardware are welcome, but they do not replace deterministic tests.

## Catalogue sources

A new source is a module under `catalogue_builder/sources/` that fetches
through the build context, so every download is cached and throttled. Before
adding one:

- read the site's terms and `robots.txt`, and do not add a site that blocks
  automated clients or asks not to be scraped;
- prefer a published dump, DAT or index over crawling pages;
- keep at most one request a second to the host, or slower for small hobby
  sites;
- record the source's name, URL and licence in its `INFO`, and add it to
  [docs/DATA_SOURCES.md](docs/DATA_SOURCES.md);
- check that its licence is compatible with publishing the catalogue under
  CC BY-NC-SA 4.0.

Series definitions and aliases live in `data/series/*.toml`. A correction to
the name, aliases or match rules of a series is a normal pull request.

## Pull requests

Explain the user-visible result, the machines and formats affected, the write
and download boundaries, the failure modes tested, the accessibility impact
and the exact verification commands. Keep unrelated changes in separate pull
requests.

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

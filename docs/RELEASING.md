# Release process

Only the repository maintainer publishes a release. The repository publishes
two kinds of release: application releases, tagged `vX.Y.Z`, and catalogue
releases, tagged `catalogue-YYYY-MM-DD`.

## Catalogue releases

The **Catalogue** workflow (`.github/workflows/catalogue.yml`) runs every
Monday at 04:17 UTC and can be started by hand from the Actions tab. It:

1. restores the builder's download cache from the previous run with
   `actions/cache`, so only stale pages and new dumps are fetched;
2. runs `python3 -m catalogue_builder`, which writes `catalogue.sqlite`,
   `catalogue.sqlite.gz` and `catalogue.sqlite.gz.sha256`;
3. opens the result with the application's own catalogue reader and fails if
   it contains no disks;
4. checks the compressed file against its checksum and against the
   uncompressed catalogue;
5. publishes `catalogue.sqlite.gz` and `catalogue.sqlite.gz.sha256` as a
   release tagged `catalogue-YYYY-MM-DD`.

A source that fails during the build is logged and left out, and its failure
is recorded in the catalogue's `meta` table as `source:<id>`. Check the
workflow log after a run whose disk counts drop.

Catalogue releases are published as full releases (not prereleases) and are
never marked as the latest release. The application's update check reads the
repository's release list, skips drafts and prereleases, and takes the newest
release that carries a `catalogue.sqlite.gz` asset with a checksum beside it;
the "Latest" badge stays on the newest application release, whose page holds
the installer. A second run on the same day replaces that day's assets rather
than creating a new tag.

To check a catalogue before it is published, run the builder locally:

```sh
PYTHONPATH=src python3 -m catalogue_builder
PIRATEFINDER_CATALOGUE=build/catalogue.sqlite ./piratefinder
```

## Application releases

### Prepare the source

1. Update `__version__` in `src/piratefinder/__init__.py`. `pyproject.toml`
   reads the version from there and states none of its own.
2. Add a `<release>` entry for the version, with the date, at the top of
   `data/com.github.pclarke.PirateFinder.metainfo.xml`.
3. Review the pinned Greaseweazle version in
   `packaging/greaseweazle-version.txt`, its source archive checksum in
   `packaging/greaseweazle-source.sha256`, and the Python dependencies in
   `packaging/runtime-requirements.txt`. Record any change in
   `THIRD_PARTY_NOTICES.md`.
4. Update `README.md`, `ROADMAP.md`, `docs/CURRENT_STATUS.md` and the in-app
   help for user-visible changes, and the version in the file names in
   `README.md` and `docs/INSTALLATION.md`. When the interface has changed,
   regenerate the screenshots in `docs/images` and the User Guide with
   `PYTHONPATH=src python3 -m piratefinder.ui.screenshot`, which draws the real
   window with the simulated back end and needs a display.
5. Run the complete local verification:

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

### Build locally

On Ubuntu 24.04 with Python 3.12:

```sh
PYTHONPATH=src python3 -m catalogue_builder
./packaging/build-deb.sh dist
dpkg-deb --info dist/PirateFinder_0.1.0_ubuntu24.04_amd64.deb
dpkg-deb --contents dist/PirateFinder_0.1.0_ubuntu24.04_amd64.deb
cd dist
sha256sum PirateFinder_0.1.0_ubuntu24.04_amd64.deb > SHA256SUMS
```

`build-deb.sh` takes the catalogue from `build/catalogue.sqlite` and stops
with an explanation when it is missing. `--catalogue FILE` uses another copy,
such as a decompressed `catalogue.sqlite.gz` from a catalogue release, and
`--no-catalogue` builds a package without one, which is only useful for
testing the packaging. The script refuses a catalogue that is not SQLite,
has a different schema version from the one the release reads, or holds no
disks.

The build downloads the tagged Greaseweazle source archive and rejects it
unless its SHA-256 matches the reviewed value. It installs the project, the
host tools and their pinned dependencies into the private application library
with pip, from a clean copy of the source so nothing else in the working tree
reaches the package. It does not modify the system Python environment, and it
ships no compiled bytecode.

### Publish

Merge the reviewed release pull request, then create and push a tag that
exactly matches the version:

```sh
git tag -s v0.1.0 -m "PirateFinder v0.1.0"
git push origin v0.1.0
```

The **Release** workflow (`.github/workflows/release.yml`) then:

1. checks style, runs the tests and validates the desktop metadata;
2. runs `packaging/check-release-tag.sh`, which fails unless the tag is `v`
   followed by `__version__`;
3. downloads the newest `catalogue-*` release asset and checks it against its
   published SHA-256;
4. builds the `.deb` with that catalogue and inspects it, failing on any
   group-writable or world-writable file;
5. installs it on Ubuntu 24.04, runs `/usr/lib/piratefinder/bin/gw info --help`,
   imports the application and Greaseweazle from the private library, and
   opens the packaged catalogue;
6. writes `SHA256SUMS`, stores a workflow artifact, and publishes the
   installer and checksums in a GitHub Release marked as the latest release,
   naming the catalogue it includes.

A failed check, build or installation prevents publication. At least one
catalogue release must exist before the first application release.

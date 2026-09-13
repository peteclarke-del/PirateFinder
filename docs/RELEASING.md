# Release process

Only the repository maintainer publishes a release. The repository publishes
two kinds of release: application releases, tagged `vX.Y.Z`, and catalogue
releases, tagged `catalogue-YYYY-MM-DD`. The repository is public, so anyone
can download either kind without a GitHub account.

## Catalogue layouts

A catalogue's layout is its database schema, numbered by `SCHEMA_VERSION` in
`src/piratefinder/catalogue/schema.py` (3 at present). The application reads
one layout only. Each catalogue release names its layout in the file name,
`catalogue-layout<N>.sqlite.gz` with `catalogue-layout<N>.sqlite.gz.sha256`
beside it, so that:

- the in-app update (`src/piratefinder/catalogue/update.py`) looks only for the
  asset of the layout it reads, and is never offered a catalogue it cannot
  use;
- the Release workflow bundles the newest catalogue of the layout the tagged
  source reads;
- a change of layout needs no coordination: once `SCHEMA_VERSION` changes, the
  Catalogue workflow publishes the new layout under its new name and older
  applications go on finding the last catalogue of theirs.

When `SCHEMA_VERSION` changes, run the Catalogue workflow on the new code
before tagging an application release, because the Release workflow fails when
no release carries the new layout. Releases published before assets were named
by layout carry `catalogue.sqlite.gz`, which is layout 1 and is ignored by
everything but PirateFinder 0.1.0.

## Catalogue releases

The **Catalogue** workflow (`.github/workflows/catalogue.yml`) runs every
Monday at 04:17 UTC and can be started by hand from the Actions tab. It:

1. restores the builder's download cache from the previous run with
   `actions/cache`, so only stale pages and new dumps are fetched;
2. runs `python3 -m catalogue_builder`, which writes `catalogue.sqlite`,
   `catalogue.sqlite.gz` and `catalogue.sqlite.gz.sha256`;
3. opens the result with the application's own catalogue reader and fails if
   it contains no disks or has another layout;
4. checks the compressed file against the builder's checksum, copies it to
   `catalogue-layout<N>.sqlite.gz` with `N` read from `SCHEMA_VERSION`, writes
   a `.sha256` file that names the new file, and checks the copy against it
   and against the uncompressed catalogue;
5. publishes `catalogue-layout<N>.sqlite.gz` and its `.sha256` file as a
   release tagged `catalogue-YYYY-MM-DD`.

A source that fails during the build is logged and left out, and its failure
is recorded in the catalogue's `meta` table as `source:<id>`. Check the
workflow log after a run whose disk counts drop.

Catalogue releases are published as full releases (not prereleases) and are
never marked as the latest release. The application's update check reads the
repository's public release list without signing in, skips drafts and
prereleases, and takes the newest release, by the date in its tag, that
carries `catalogue-layout<N>.sqlite.gz` for its own layout with the checksum
file beside it. When the list cannot be fetched or read, the check says
"Could not check for a newer catalogue:" and the reason, never that the
catalogue is up to date. The "Latest" badge stays on the newest application
release, whose page holds the installer. A second run on the same day replaces that day's assets rather
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
   help (`src/piratefinder/ui/help_content.py`) for user-visible changes. The
   download commands in `README.md` and `docs/INSTALLATION.md` fetch the
   latest release and name no version, so they need no change.
   Write `docs/USER_GUIDE.md` again from the help with
   `PYTHONPATH=src python3 -m piratefinder.ui.help_content`. When the
   interface has changed, regenerate the screenshots in `docs/images` and the
   User Guide with
   `PYTHONPATH=src:. python3 -m piratefinder.ui.screenshot --catalogue build/catalogue.sqlite`,
   which draws the real window with the simulated back end, searching the
   catalogue given, and needs a display. Look at every picture it writes.
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

### Package targets

`packaging/targets.sh` holds the one table of the distribution releases and
architectures a release is packaged for, with the Docker image of each release
and the Python version it ships:

| Token | Release | Docker image | Python | Architectures |
| --- | --- | --- | --- | --- |
| `ubuntu-24.04` | Ubuntu 24.04 | `ubuntu:24.04` | 3.12 | amd64, arm64, armhf |
| `debian-13` | Debian 13 | `debian:trixie` | 3.13 | amd64, arm64, armhf |

The token is the release's `ID` and `VERSION_ID` from `/etc/os-release`, and
it is part of the package name, `PirateFinder_<version>_<token>_<arch>.deb`.
The extension modules of Greaseweazle and of its dependencies bitarray and
crcmod are compiled for one Python, so each package depends on
`python3` from its release's minor version up to the next one, for example
`python3 (>= 3.13), python3 (<< 3.14)` on Debian 13. Both releases take
`python3-gi`, `gir1.2-gtk-4.0` and `gir1.2-adw-1 (>= 1.5)`.

To add a release, add its row to the table and its token to the `distro`
list of the matrix in `.github/workflows/release.yml`, then update the
expected table in `tests/test_packaging.py`, which checks that the table and
the matrix agree, and the tables in this file and `docs/INSTALLATION.md`.

### Build locally

`packaging/build-deb.sh` builds one package:

```sh
packaging/build-deb.sh --distro DISTRO [--arch ARCH] [--container] \
    [--catalogue FILE | --no-catalogue] [OUTPUT_DIR]
```

`--distro` is a token from the table. `--arch` is `amd64`, `arm64` or `armhf`
and defaults to the architecture of the machine running the script. The
package is written to `OUTPUT_DIR`, `dist` by default.

The compiled modules have to be built on the release and architecture the
package is for. With `--container` the script runs itself in a Docker
container of that release and architecture (`docker run --platform`), where it
installs the compiler, the Python headers, pip and curl, and hands the package
to the user who ran it. Docker runs other architectures through qemu, which
needs qemu's binfmt handlers registered with the fix-binary (`F`) flag, as
`docker run --privileged --rm tonistiigi/binfmt --install arm64,arm` and the
workflows' `docker/setup-qemu-action` register them. Ubuntu's `qemu-user-binfmt`
package registers them without it, and a container of another architecture
then stops at once with `exec /usr/bin/bash: no such file or directory`. Without
`--container` the build runs on the machine itself, which must be the named
release and architecture with `python3-pip`, `python3-dev`,
`build-essential` and `curl` installed.

```sh
PYTHONPATH=src python3 -m catalogue_builder
./packaging/build-deb.sh --container --distro ubuntu-24.04 --arch amd64 dist
./packaging/build-deb.sh --container --distro debian-13 --arch arm64 dist
./packaging/install-test.sh --require-catalogue dist/PirateFinder_*_debian-13_arm64.deb
cd dist
sha256sum -- *.deb > SHA256SUMS
```

`packaging/install-test.sh PACKAGE.deb` reads the release and architecture
from the file name and runs itself with `--here` in a new container of them.
There it checks the package's fields and file modes, installs it with apt,
runs the bundled `gw info --help`, loads every compiled module, opens the
packaged catalogue, and starts the application under Xvfb until its window
opens. `--require-catalogue` fails a package without the catalogue. An amd64
build or test takes two or three minutes on an x86 machine; under emulation
each takes from a quarter of an hour to well over an hour, which is why the
release workflow builds arm64 on Arm runners.

`build-deb.sh` takes the catalogue from `build/catalogue.sqlite` and stops
with an explanation when it is missing. `--catalogue FILE` uses another copy,
such as a decompressed `catalogue-layout3.sqlite.gz` from a catalogue release, and
`--no-catalogue` builds a package without one, which is only useful for
testing the packaging. The script refuses a catalogue that is not SQLite,
has a different schema version from the one the release reads, or holds no
disks.

The build downloads the tagged Greaseweazle source archive and rejects it
unless its SHA-256 matches the reviewed value. It installs the project, the
host tools and their pinned dependencies into the private application library
with pip, from a clean copy of the source so nothing else in the working tree
reaches the package. It stops unless the compiled modules of Greaseweazle,
bitarray and crcmod load in the target Python, because Greaseweazle and crcmod
otherwise fall back to slower code without saying so. It does not modify the
system Python environment, and it ships no compiled bytecode.

### Publish

Merge the reviewed release pull request, then create and push a tag that
exactly matches the version:

```sh
git tag -s v0.2.1 -m "PirateFinder v0.2.1"
git push origin v0.2.1
```

The **Release** workflow (`.github/workflows/release.yml`) then:

1. checks style, runs the tests and validates the desktop metadata;
2. runs `packaging/check-release-tag.sh`, which fails unless the tag is `v`
   followed by `__version__`;
3. reads the layout from `SCHEMA_VERSION` in the tagged source, downloads
   `catalogue-layout<N>.sqlite.gz` from the newest `catalogue-*` release that
   carries it, and checks it against its published SHA-256, once, so that
   every package carries the same catalogue;
4. for each release and architecture in the table, builds the package with
   that catalogue using `build-deb.sh --container`, and runs
   `install-test.sh --require-catalogue` on it. amd64 and armhf run on x86
   runners, armhf under qemu, and arm64 on GitHub's Arm runners. Each package
   is kept as a workflow artifact named `package-<token>-<arch>`;
5. writes `SHA256SUMS` for all six packages and publishes them in a GitHub
   Release marked as the latest release, naming the catalogue they include.

The application's own update (Check for Application Updates, in
`src/piratefinder/app_update.py`) depends on three things this workflow
does: the application release is marked as the latest release, its tag is
`vX.Y.Z`, and it carries `SHA256SUMS` beside packages named
`PirateFinder_<version>_<distro>_<arch>.deb`. Each package records its
distribution and architecture in `/usr/lib/piratefinder/package-target`, which
`build-deb.sh` writes and `install-test.sh` checks. `tests/test_packaging.py`
fails if the workflow, the builder and the update stop agreeing.

A failed check, build or installation of any package prevents publication. A catalogue
release of the source's layout must exist before an application release can
be built.

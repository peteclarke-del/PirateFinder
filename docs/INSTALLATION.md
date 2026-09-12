# Installing PirateFinder

## Supported release package

The native package targets 64-bit Ubuntu 24.04 and Linux Mint 22. It
contains:

- the application, under `/usr/lib/piratefinder`;
- the newest published catalogue at the time of the release, as
  `/usr/share/piratefinder/catalogue.sqlite`;
- a private copy of Greaseweazle Host Tools 1.23 and its Python dependencies,
  run as `/usr/lib/piratefinder/bin/gw`;
- the Greaseweazle udev rules, as
  `/usr/lib/udev/rules.d/49-piratefinder-greaseweazle.rules`;
- the desktop entry, AppStream metadata and application icons.

The distribution supplies Python 3.12, GTK 4, libadwaita and PyGObject. The
package recommends `7zip`, which is needed to read `.7z` archives, and apt
installs it by default.

1. Open the required version under GitHub Releases.
2. Download `PirateFinder_0.1.0_ubuntu24.04_amd64.deb` and `SHA256SUMS` into
   the same folder.
3. Verify the download:

   ```sh
   sha256sum --check --ignore-missing SHA256SUMS
   ```

4. Install the package and its distribution dependencies:

   ```sh
   sudo apt install ./PirateFinder_0.1.0_ubuntu24.04_amd64.deb
   ```

5. Unplug and reconnect the Greaseweazle. The package reloads the udev rules,
   but a physical reconnect is needed for the new access tags to apply.
6. Start **PirateFinder** from the GNOME application grid, or run
   `piratefinder` in a terminal.

The launcher puts `/usr/lib/piratefinder/bin` first on `PATH`, so PirateFinder
always runs the Greaseweazle host tools it was released with, even when a
different `gw` is installed elsewhere.

## Alongside Greaseweazle-GUI

Greaseweazle-GUI installs the same Greaseweazle rules as
`49-greaseweazle.rules`. PirateFinder uses a different file name, so both
packages can be installed together. Loading the same rules twice is harmless.
Each application uses its own private copy of the host tools.

## Where PirateFinder keeps its files

| What | Where |
| --- | --- |
| Settings | `~/.config/piratefinder/settings.json` |
| Library index, write history and corrections | `~/.local/share/piratefinder/user.sqlite` |
| Catalogue installed by an in-app update | `~/.local/share/piratefinder/catalogue.sqlite` |
| Downloads waiting to be checked | `~/.cache/piratefinder/downloads` |
| Downloaded disk images (default) | `~/Floppy Images/PirateFinder` |

The locations follow `XDG_CONFIG_HOME`, `XDG_DATA_HOME` and `XDG_CACHE_HOME`
when they are set. The download folder can be changed on the Library page or
in Preferences, for example to a folder on a NAS.

PirateFinder uses the newest of the catalogue installed by an in-app update,
the catalogue shipped in the package, and, when run from a source tree,
`build/catalogue.sqlite`. Setting `PIRATEFINDER_CATALOGUE` to a file overrides
all three.

## Library folders on a NAS

Library and download folders are ordinary paths. Mount the share first, then
add the mounted folder on the Library page:

- **GNOME Files**: connect to `smb://nas/share` from Other Locations. The share
  appears under `/run/user/<uid>/gvfs/`, and PirateFinder can use that path
  while the connection is open.
- **A permanent mount**: add the share to `/etc/fstab` (CIFS or NFS) so it is
  mounted at start-up under a fixed path such as `/mnt/floppies`. This is
  faster than GVFS for large collections and does not depend on a GNOME
  session.

A library scan reads only files whose size or modification time has changed,
so a rescan of a large share is quick after the first one.

## Upgrading

Download the newer `.deb`, verify its checksum, and install it with the same
`apt install ./FILE.deb` command. Settings, the library index, the history and
downloaded images are outside the package and are kept. A catalogue installed
by an in-app update is kept too; PirateFinder uses whichever catalogue is
newer.

## Removing

```sh
sudo apt remove piratefinder
```

Removal deletes the application, the packaged catalogue, the bundled host
tools, the desktop metadata and the device rule. It does not delete settings,
the library index, history or disk images in your folders.

## Running from source

Other Linux distributions can run PirateFinder from a source checkout. Install
Python 3.12 or newer, GTK 4, libadwaita 1.5 or newer, PyGObject, the upstream
Greaseweazle host tools (so that `gw` is on `PATH`) and 7-Zip, then run:

```sh
./piratefinder
```

A catalogue is needed as well: build one with
`PYTHONPATH=src python3 -m catalogue_builder`,
or download `catalogue.sqlite.gz` from a `catalogue-YYYY-MM-DD` release,
decompress it and point `PIRATEFINDER_CATALOGUE` at it.

Native packages for other distribution families are planned and will be listed
here only after their installation is tested by the release workflow.

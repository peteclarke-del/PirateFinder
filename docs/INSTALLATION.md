# Installing PirateFinder

## Supported release packages

Each release has a native package for each of these systems:

| System | Architectures | Package |
| --- | --- | --- |
| Ubuntu 24.04, and Linux Mint 22 which is built on it | amd64, arm64, armhf | `PirateFinder_<version>_ubuntu-24.04_<arch>.deb` |
| Debian 13 (trixie) | amd64, arm64, armhf | `PirateFinder_<version>_debian-13_<arch>.deb` |

Every package is installed and started on its own release and architecture by
the release workflow before it is published. Version 0.1.0 predates these
names: it has one package, `PirateFinder_0.1.0_ubuntu24.04_amd64.deb`, for
Ubuntu 24.04 amd64.

Each package contains:

- the application, under `/usr/lib/piratefinder`;
- the newest published catalogue at the time of the release, as
  `/usr/share/piratefinder/catalogue.sqlite` (about 249 MB);
- a private copy of Greaseweazle Host Tools 1.23 and its Python dependencies,
  run as `/usr/lib/piratefinder/bin/gw`;
- the Greaseweazle udev rules, as
  `/usr/lib/udev/rules.d/49-piratefinder-greaseweazle.rules`;
- the desktop entry, AppStream metadata and application icons.

The distribution supplies Python, GTK 4, libadwaita 1.5 or newer and PyGObject
(`python3-gi`, `gir1.2-gtk-4.0` and `gir1.2-adw-1`). The package recommends
`7zip`, which is needed to read `.7z` archives, and apt installs recommended
packages by default.

### Choosing the package

The package must match both the release and the architecture of your system.
The Greaseweazle host tools it bundles include compiled code for one Python
version, 3.12 on Ubuntu 24.04 and 3.13 on Debian 13, so a package for one
release does not install on the other. To find your release, run:

```sh
cat /etc/os-release
```

`ID=ubuntu` with `VERSION_ID="24.04"`, or `ID=linuxmint` with
`UBUNTU_CODENAME=noble`, takes the `ubuntu-24.04` package. `ID=debian` with
`VERSION_ID="13"` takes the `debian-13` package. Other distributions built on
one of these releases may be able to use its package, but they are not
tested. To find your architecture, run:

```sh
dpkg --print-architecture
```

It prints `amd64` for a 64-bit Intel or AMD PC, `arm64` for a 64-bit Arm
system such as a Raspberry Pi 4 or 5 running a 64-bit system, or `armhf` for a
32-bit Arm system.

### Downloading the release

Releases are published on the project's
[GitHub releases page](https://github.com/peteclarke-del/PirateFinder/releases)
with a `SHA256SUMS` file. The repository is public, so no GitHub account is
needed. Download the package and `SHA256SUMS` from the latest release in a
browser, or with `curl`, setting `distro` to the release token from the table
above:

```sh
distro=ubuntu-24.04
arch="$(dpkg --print-architecture)"
base=https://github.com/peteclarke-del/PirateFinder/releases/latest/download
curl -L -O "$base/SHA256SUMS"
package="$(grep -o "PirateFinder_[^ ]*_${distro}_${arch}\.deb" SHA256SUMS)"
curl -L -O "$base/$package"
```

or with the GitHub command line tool, `gh`, which works once you have signed
in to any GitHub account of your own with `gh auth login`:

```sh
gh release download --repo peteclarke-del/PirateFinder \
    --pattern "PirateFinder_*_${distro}_${arch}.deb" --pattern SHA256SUMS
```

The releases page also lists the weekly catalogue releases, tagged
`catalogue-YYYY-MM-DD`; the application release is the one tagged `v` and a
version number, marked Latest.

### Installing

1. Put the package, such as `PirateFinder_<version>_debian-13_arm64.deb`, and
   `SHA256SUMS` in the same folder.
2. Verify the download:

   ```sh
   sha256sum --check --ignore-missing SHA256SUMS
   ```

3. Install the package and its distribution dependencies, giving the file
   name with `./` in front so that apt reads the file:

   ```sh
   sudo apt install ./PirateFinder_<version>_debian-13_arm64.deb
   ```

4. Unplug the Greaseweazle and plug it in again. The package reloads the udev
   rules, but they apply to a device only when it is connected again.
5. Start **PirateFinder** from the GNOME application grid, or run
   `piratefinder` in a terminal.

The launcher puts `/usr/lib/piratefinder/bin` first on `PATH`, so PirateFinder
always runs the Greaseweazle host tools it was released with, even when a
different `gw` is installed elsewhere. To check that the device is found
outside the application, run:

```sh
/usr/lib/piratefinder/bin/gw info
```

It prints the host tool version and, under `Device:`, the port, model and
firmware of the Greaseweazle, or `Not found`.

## IPF support

Writing `.ipf` images needs the SPS Decoder Library (CAPSImg,
`libcapsimage.so.5`), which the Greaseweazle host tools load when they read an
IPF. Its licence allows only non-commercial use and redistribution, so the
package does not include it. To add it:

1. Open Preferences, choose the Greaseweazle page, and find IPF Support.
2. Choose Install, read the licence, and choose Accept and Install.

PirateFinder downloads the build that the FS-UAE emulator publishes on
fs-uae.net, checks it against the size and SHA-256 recorded in PirateFinder,
and installs it for your account only, in
`~/.local/share/piratefinder/caps/libcapsimage.so.5`. No administrator rights
are needed. Remove, in the same place, deletes it again.

| Package architecture | IPF Support |
| --- | --- |
| amd64 | CAPSImg 5.1.3 for Linux x86-64 |
| armhf | CAPSImg 5.1.3 for 32-bit Arm, built for ARMv8 processors such as those of the Raspberry Pi 3, 4 and 5 |
| arm64 | Not offered: FS-UAE publishes no 64-bit Arm build for Linux |

On arm64, or to use another copy, build the library from the SPS source and
install it where the system finds libraries, such as `/usr/local/lib` followed
by `sudo ldconfig`. PirateFinder uses a copy that the system finds, says so
under IPF Support, and then offers no download. To check the copy
PirateFinder installed with the packaged host tools, run:

```sh
LD_LIBRARY_PATH=~/.local/share/piratefinder/caps PYTHONPATH=/usr/lib/piratefinder \
    python3 -c 'from greaseweazle.image.caps import get_libcaps; print(get_libcaps())'
```

## Alongside Greaseweazle-GUI

Greaseweazle-GUI installs the same Greaseweazle rules as
`49-greaseweazle.rules`. PirateFinder uses a different file name, so both
packages can be installed together. Loading the same rules twice is harmless.
Each application uses its own private copy of the host tools. Only one program
can use the Greaseweazle at a time, so let a read or write in Greaseweazle-GUI
finish before writing from PirateFinder.

## The device rule

The rule tags the Greaseweazle so that systemd-logind gives the user logged in
at the desktop access to it; no group membership is needed. It also stops
ModemManager from probing the device and adds a `/dev/greaseweazle` link. When
running from source without the package, install the rules that come with the
Greaseweazle host tools, or add your account to the `dialout` group and log in
again.

## Where PirateFinder keeps its files

| What | Where |
| --- | --- |
| Settings | `~/.config/piratefinder/settings.json` |
| Library index, write history, corrections and cleaned-file records | `~/.local/share/piratefinder/user.sqlite` |
| The write queue | `~/.local/share/piratefinder/queue.json` |
| Catalogue installed by an in-app update | `~/.local/share/piratefinder/catalogue.sqlite` |
| Amiga Bootblock Reader brainfile, when downloaded | `~/.local/share/piratefinder/virus/abr/` |
| SPS Decoder Library for IPF, when installed | `~/.local/share/piratefinder/caps/` |
| Pictures and Wikipedia summaries | `~/.cache/piratefinder/media/` |
| Downloads waiting to be checked | `~/.cache/piratefinder/downloads/` |
| Downloaded disk images (default) | `~/Floppy Images/PirateFinder` |

The locations follow `XDG_CONFIG_HOME`, `XDG_DATA_HOME` and `XDG_CACHE_HOME`
when they are set. The download folder can be changed on the Library page or
in Preferences, for example to a folder on a NAS. [Privacy](PRIVACY.md)
describes what each file holds and how long cached files are kept.

PirateFinder uses the newest of the catalogue installed by an in-app update,
the catalogue shipped in the package, and, when run from a source tree,
`build/catalogue.sqlite`. A copy with a layout this version cannot read is
passed over. Setting `PIRATEFINDER_CATALOGUE` to a file overrides all three.

## Library folders on a NAS

Library and download folders are ordinary paths. Mount the share first, then
add the mounted folder on the Library page:

- In GNOME Files, connect to `smb://nas/share` from Other Locations. The
  share appears under `/run/user/<uid>/gvfs/`, and PirateFinder can use that
  path while the connection is open.
- For a permanent mount, add the share to `/etc/fstab` (CIFS or NFS) so it is
  mounted at start-up under a fixed path such as `/mnt/floppies`. This is
  faster than GVFS for large collections and does not depend on a GNOME
  session.

A library scan reads only files whose size or modification time has changed,
so a rescan of a large share is quick after the first one. The first scan
after updating to a version that checks boot blocks for viruses reads every
file once. When an update brings new virus data, or the Amiga Bootblock
Reader brainfile is installed, PirateFinder checks the boot blocks of the
library again at start, reading only the boot block of each file, which is
much quicker than a scan.

## Upgrading

Download the newer `.deb`, verify its checksum, and install it with the same
`apt install ./FILE.deb` command. Settings, the library index, the queue, the
history and downloaded images are outside the package and are kept. A
catalogue installed by an in-app update is kept too; PirateFinder uses
whichever catalogue is newer.

A package works only on the distribution release it was built for, because it
depends on that release's Python version. After upgrading the system to a new
release, such as Ubuntu 24.04 to a later Ubuntu, apt reports the installed
PirateFinder as broken; install the package for the new release when one is
published, or remove PirateFinder.

## Removing

```sh
sudo apt remove piratefinder
```

Removal deletes the application, the packaged catalogue, the bundled host
tools, the desktop metadata and the device rule. It does not delete settings,
the library index, the queue, the history, the cache or the disk images in
your folders. To remove those as well, delete `~/.config/piratefinder`,
`~/.local/share/piratefinder` and `~/.cache/piratefinder`.

## Running from source

Other Linux distributions can run PirateFinder from a source checkout. Install
Python 3.12 or newer, GTK 4, libadwaita 1.5 or newer, PyGObject, the upstream
Greaseweazle host tools (so that `gw` is on `PATH`) and 7-Zip, then run:

```sh
./piratefinder
```

A catalogue is needed as well: build one with
`PYTHONPATH=src:. python3 -m catalogue_builder`,
or download the catalogue file for the source tree's layout, such as
`catalogue-layout3.sqlite.gz` (the number is `SCHEMA_VERSION` in
`src/piratefinder/catalogue/schema.py`), from a `catalogue-YYYY-MM-DD` release,
decompress it and point `PIRATEFINDER_CATALOGUE` at it.

Native packages for other distribution families are planned and will be listed
here only after their installation is tested by the release workflow.

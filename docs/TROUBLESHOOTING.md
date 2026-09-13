# Troubleshooting

PirateFinder reports a problem with the Greaseweazle, a floppy, the catalogue
or a download in a sentence of its own. The sections below quote those
sentences, say what each one means and what to check. The Troubleshooting
topic of the [User Guide](USER_GUIDE.md#troubleshooting) has the same advice in
shorter form.

The **Diagnostic Log** in the main menu holds the output of the Greaseweazle
host tools and the messages from searches, scans, downloads and writing
sessions since PirateFinder started. Copy or save it when asking for help, and
remove private paths first. It is kept in memory only.

## The Greaseweazle is not found

While no Greaseweazle is found, a bar under the header says "No Greaseweazle
connected. You can still search and download." Every two seconds PirateFinder
looks for the device in the system's list of USB devices and serial ports,
which runs no program. When a Greaseweazle appears there, it runs `gw info`
once to identify it. Retry, and Check Connection in Preferences, run `gw info`
at once. Preferences, on the Greaseweazle page, shows the message:

| Message | Meaning |
| --- | --- |
| No Greaseweazle is connected. | No Greaseweazle is plugged in, or `gw info` ran but found no device. |
| The Greaseweazle host tools (gw) are not installed. | No `gw` was found: not `PIRATEFINDER_GW`, not on `PATH`, not `/usr/lib/piratefinder/bin/gw`. |
| The Greaseweazle host tools did not answer in time. | `gw info` took longer than 15 seconds. |
| The Greaseweazle host tools failed: ... | `gw info` stopped with an error; the rest of the message is its last line. |
| ... It is in firmware update mode. | The device was started in its bootloader. Unplug it, check the update jumper or button, and plug it in again. |

Check these in turn:

1. Ask the host tool directly. With the package installed:

   ```sh
   /usr/lib/piratefinder/bin/gw info
   ```

   From a source tree, run `gw info`. Under `Device:` it prints the port,
   model and firmware, or `Not found`.

2. Check that the device exists and that you may use it:

   ```sh
   ls -l /dev/ttyACM* /dev/greaseweazle
   getfacl /dev/ttyACM0
   ```

   With the package's rule in place, `getfacl` lists your user with `rw-`.
   If `/dev/greaseweazle` is missing, the rule did not apply: unplug the
   Greaseweazle and plug it in again. The package reloads the rules when it is
   installed, but they reach a device only when it is connected.

3. When running from source without the package, install the udev rules that
   come with the Greaseweazle host tools, or add yourself to the `dialout`
   group and log in again:

   ```sh
   sudo usermod -aG dialout "$USER"
   ```

4. Close other programs that use the Greaseweazle. Greaseweazle-GUI can be
   installed next to PirateFinder, but only one program can talk to the device
   at a time. `fuser -v /dev/ttyACM0` shows which process holds the port.

5. Try another USB cable or port. Some cables carry power only.

6. If the Greaseweazle is plugged in and `gw info` finds it but the bar stays,
   the device list does not show it. PirateFinder looks for the USB id
   `1209:4d69` or a product name of Greaseweazle under `/sys/bus/usb/devices`,
   the `/dev/greaseweazle` link that the device rule makes, and a Greaseweazle
   name under `/dev/serial/by-id`. Choose Retry, which runs `gw info` itself,
   or set the Device setting described below.

The Device setting in Preferences, such as `/dev/ttyACM0`, is passed to `gw`
for writing and for the connection check, and PirateFinder then watches for
that port alone. Leave it empty unless the wrong serial device is picked.

## The disk is write-protected

The prompt comes back with "The disk is write-protected. Close the
write-protect hole and try again." A 3.5 inch floppy can only be written when
its write-protect tab covers the hole. Slide the tab over, insert the disk
again and choose Write. Greaseweazle reports this as a failed command while
still exiting with status zero; PirateFinder reads the output, so it is
reported as write-protected rather than as written.

## No index, or no disk found

The prompt comes back with "No disk was found in the drive. Insert a disk and
try again." Greaseweazle said No Index (the drive is not turning a disk, so no
index pulse arrives) or Track 0 not found (the head cannot find the first
track). Check that:

- the disk is fully inserted and the drive's light comes on when writing
  starts;
- the drive has power, from the Greaseweazle's power connector or a separate
  supply;
- the ribbon cable is the right way round at both ends (the red stripe to pin
  1);
- the drive choice matches the cable (see the next section).

"The drive did not report track 0. Check the drive cable and power, then try
again." means the same kind of problem, found before writing started.

## The wrong drive is selected

When the drive choice does not match the cable, Greaseweazle selects a drive
that is not there: the light on your drive stays off and the write stops with
no index. Set the drive on the Queue page or in Preferences:

| Cable | Drive |
| --- | --- |
| PC floppy cable, drive after the twist (at the end of the cable) | A |
| PC floppy cable, drive before the twist (middle connector) | B |
| Straight cable (Shugart bus) | 0, 1, 2 or 3, to match the drive's select jumper |

Most setups with one PC drive on a twisted cable use A, the default.

## Tracks that do not verify

Greaseweazle reads each track back after writing it and writes a failing track
again up to the number of Retries in Preferences (3 to start with). When a
track still fails, the disk is reported as Failed with "Track 34.1 did not
verify after 4 attempts. The disk may be worn or damaged; try another disk."
and the summary lists the tracks.

- Try another floppy. Old floppies shed oxide and develop weak spots.
- Clean the drive heads with a cleaning disk, following its instructions.
- Use double-density floppies. High-density ones (1.44 MB, with a second hole
  opposite the write-protect tab) are less reliable when written as
  double-density disks.
- Switch on Erase Before Writing in Preferences, which wipes each track
  before writing it, and raise Retries.

"Greaseweazle finished without confirming the write. Treat the disk as
unverified." means `gw` ended without saying whether the tracks verified.
Write the disk again, and check the Diagnostic Log for the output.

## Other writing errors

| Message | Meaning |
| --- | --- |
| Greaseweazle reported a hardware error: ... | `gw` printed `Command Failed` with a reason other than write protection or a missing disk. |
| Greaseweazle stopped with an error: ... | `gw` stopped with a fatal error; the rest is its message. |
| Writing did not finish within ... and was stopped. | The write took far longer than any disk should, and was ended. The disk is incomplete. |
| No image of this disk is in the library and no enabled provider has one. | Nothing to write from. Add the image to a library folder, or switch on a provider that has it. |
| None of the images found for this disk could be used. | Every image failed to download, failed its checksum or was refused when prepared. The Diagnostic Log has each reason. |

An image that cannot be written faithfully is refused with the reason, for
example a protected STX, an extended ADF, or an IPF without the SPS Decoder
Library. [Format support](FORMAT_SUPPORT.md) lists each case.

### IPF images are refused

"Writing an IPF image needs the SPS Decoder Library (libcapsimage), which is
not installed." means `gw` has no library to read IPF files with. Install it
with IPF Support in Preferences, on the Greaseweazle page. When the message
says PirateFinder has no build for this computer's processor (a 64-bit Arm
system, for example), install the library by other means, such as building it
from the SPS source into `/usr/local/lib` and running `sudo ldconfig`, or
write another dump of the disk.

When the install itself fails, IPF Support says why:

| Message | Meaning |
| --- | --- |
| The SPS Decoder Library could not be downloaded. ... | fs-uae.net could not be reached or refused the download; the rest names the reason. Try again later. |
| The file downloaded from fs-uae.net is not the one PirateFinder expects ... | The file's size or SHA-256 differs from the one recorded in PirateFinder, so nothing was installed. The file on fs-uae.net has changed; report it as a PirateFinder issue. |
| The downloaded library does not load on this computer ... | The library was loaded and started in a separate process as a check, and that failed, for example because a library it needs, `libstdc++.so.6`, is missing. Nothing was installed. |

## The catalogue

### No Catalogue Installed

PirateFinder found no catalogue it can read. Preferences, on the Catalogue
page, shows why under Location. From a package this should not happen;
reinstall the package. From a source tree, build a catalogue with
`PYTHONPATH=src:. python3 -m catalogue_builder`, or set
`PIRATEFINDER_CATALOGUE` to a catalogue file.

### A catalogue from another version

A catalogue built for an older or newer PirateFinder has a different layout.
The message names both, for example "The catalogue
/usr/share/piratefinder/catalogue.sqlite was made for a different PirateFinder
version (layout 1; this version reads layout 3)." PirateFinder uses another
copy it can read when there is one. Install the current package, or build the
catalogue from the same source tree as the application.

### Update Catalogue cannot check

"Could not check for a newer catalogue: ..." means the list of releases on
GitHub could not be fetched or read, and the rest of the message says why:
for example "api.github.com could not be reached" without a network
connection, or HTTP 403 or 429 when GitHub is limiting requests from your
address (it allows 60 an hour without an account). Nothing was checked, so
nothing is known about newer catalogues; try again later. The installed
catalogue stays in use.

"The catalogue is up to date" is shown only when the check worked and found
no newer catalogue of the layout this version reads.

### An update fails

Each catalogue release names its layout in the file name, such as
`catalogue-layout3.sqlite.gz`, and PirateFinder looks only for the layout it
reads, so it is not offered a catalogue it cannot use. A download is still
checked before it is installed:

| Message | Meaning |
| --- | --- |
| The downloaded catalogue does not match its published checksum, so it was not installed. | The file was damaged or cut short on the way. Try again. |
| The downloaded catalogue was made for a different PirateFinder version (layout ...; this version reads layout ...). | The release holds another layout than its name says. Report it. |
| The downloaded catalogue is not a PirateFinder catalogue. | The file is not a catalogue at all. Report it. |

The installed catalogue stays in use in every case, since an update replaces
it only after every check has passed.

PirateFinder 0.1.0 reads layout 1 and looks for any catalogue file in the
releases, not only its own layout. Once the weekly Catalogue workflow
publishes layout 3 catalogues from the current source, 0.1.0 finds one,
downloads it and refuses it with "The new catalogue needs a newer version of
PirateFinder." Its own catalogue stays in use. A PirateFinder package newer
than 0.1.0 reads layout 3 and brings a layout 3 catalogue with it.

## Downloads

- Check that Online Downloads and the provider are switched on in Preferences,
  and that the provider's site opens in a web browser.
- A download that does not match the catalogue checksum is deleted, and the
  next provider is tried. When none works, the disk is reported as No usable
  image, and the Diagnostic Log names each provider with its reason.
- A server that answers 429 or 5xx is asked again after a pause, honouring
  `Retry-After`. One that asks for a wait of more than two minutes is treated
  as unavailable for now; try again later.
- An interrupted download is resumed from where it stopped the next time the
  disk is asked for.

## Pictures and summaries

Pictures and Wikipedia summaries need both Online Downloads and Download
Screenshots and Background Information switched on. "The Picture Is Not
Available" means the site no longer has that picture, or sent something that
is not a PNG, GIF or JPEG of at most 8 MB; PirateFinder does not ask for it
again for seven days. Deleting `~/.cache/piratefinder/media` forgets every
cached picture and summary.

## The library

- The first scan of a folder reads every file, and so does the first scan
  after updating to a version that checks boot blocks. Later scans read only
  new and changed files. After an update with new virus data, Checking Boot
  Blocks on the Library page reads only the boot block of each file; a scan
  asked for meanwhile starts when it ends.
- A file that differs by even one byte from every known dump matches nothing
  and is listed as unmatched. It can still be found by file name and written.
- Reading 7z archives needs the `7z` command: `sudo apt install 7zip`.
- A folder on a NAS must be mounted before a scan. See
  [Installation](INSTALLATION.md#library-folders-on-a-nas).

## Asking for help

[Support](../SUPPORT.md) says what to include in a report: the PirateFinder
version, the catalogue build date from Preferences, the distribution, the
Greaseweazle model, firmware and host tool version, the drive, the disk label,
where the image came from and the Diagnostic Log.

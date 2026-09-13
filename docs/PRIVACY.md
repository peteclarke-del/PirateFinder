# Privacy

PirateFinder has no account, sends no usage statistics and has no crash
reporting. Searching, browsing, the library index and the write history stay
on your computer: a search runs on the local catalogue, and the library is
scanned locally. The sections below list every network request the
application makes, when it makes it and how to stop it, and every file it
keeps.

## Network requests

| When | Request | To | Can be switched off |
| --- | --- | --- | --- |
| At start, with Check for Updates at Start on (the default) | The PirateFinder release list, to see whether a newer catalogue is published | `api.github.com` | Check for Updates at Start, in Preferences, Catalogue |
| When you choose Update Catalogue or Update Now | The same release list | `api.github.com` | Only made when you ask |
| When you install a catalogue update | The catalogue file for this version's layout, such as `catalogue-layout3.sqlite.gz`, and its `.sha256` file | `github.com` and GitHub's download servers | Only made when you ask |
| When you press Check for Application Updates, in the About window | The latest PirateFinder release | `api.github.com` | Only made when you ask |
| When you choose Update to and then Download and Install | The package for your system, such as `PirateFinder_0.3.0_ubuntu-24.04_amd64.deb`, and the release's `SHA256SUMS` | `github.com` and GitHub's download servers | Only made when you ask |
| When you write, or choose Download Only for, a disc that is not in your library | The disk image, from the location the catalogue gives | The provider: `archive.org`, `atarilegend.com`, `d-bug.me`, `exxosforum.co.uk` or `ftp.scene.org` (the mirror of the amigascne archive) | Online Downloads, or the provider's own switch, in Preferences, General |
| During a writing session | The next disc in the session, in the background, when it is only available online | As above | As above |
| When the details pane shows a disc | Each picture as it is shown, not the ones behind the arrows until you step to them | `atarilegend.com`, `d-bug.me`, `media.demozoo.org` or `raw.githubusercontent.com` | Download Screenshots and Background Information, or Online Downloads |
| When the details pane shows a disc | The Wikipedia summaries of the title shown, the disc and the crew, when the catalogue names an article | `en.wikipedia.org` | As above |
| When you choose Download Brainfile or Update Brainfile | The latest Amiga Bootblock Reader release and its zip file | `api.github.com` and `github.com` | Only made when you ask |
| When you accept the licence under IPF Support, in Preferences, Greaseweazle | The SPS Decoder Library archive for your processor, such as `CAPSImg_5.1.3_Linux_x86-64.tar.xz` | `fs-uae.net` | Only made when you ask |
| When a Greaseweazle is plugged in, and when you choose Retry or Check Connection | The newest Greaseweazle firmware version, asked by the host tool's `gw info`; see below | `api.github.com` | Online Downloads, in Preferences, General |

Opening a link in the details pane, or a credit under a picture, opens the
page in your web browser, which makes its own requests.

### The Greaseweazle host tool

PirateFinder follows the Greaseweazle by reading the system's list of devices
every two seconds while the window is open: a USB device with the
Greaseweazle's id (`1209:4d69`) or product name under `/sys/bus/usb/devices`,
the `/dev/greaseweazle` link the device rule makes, a Greaseweazle name under
`/dev/serial/by-id`, or the port named in the Device setting. This check runs
no program and makes no network request. Nothing is checked while a disk is
being written.

To identify the device, PirateFinder runs the host tool's `gw info` command:

- when a Greaseweazle appears, at start or when it is plugged in, and once more
  a moment later if the first run found nothing;
- when you choose Retry in the bar under the header, Check Connection in
  Preferences, or Check Again when writing is started without a device;
- when the Device setting is changed and the port it names exists.

It never runs while a disk is being written, and a device that stays plugged
in is not asked again.

When `gw info` finds a Greaseweazle that is not in firmware update mode, it
asks the GitHub API
(`api.github.com/repos/keirf/greaseweazle-firmware/releases/latest`) for the
newest firmware version, so that it can say when an update is available, and
PirateFinder then adds a sentence such as "Firmware 1.6 is available." to the
connection message. This request is made by the Greaseweazle host tools
(version 1.23, bundled in the package), not by PirateFinder's own code, and
the host tools have no option to skip it. While Online Downloads is off,
PirateFinder starts `gw info` with an HTTPS proxy on the computer itself
(127.0.0.1) that refuses every connection, so the request fails at once and
never leaves the computer; the connection message then names no newer
firmware. When `gw info` finds no Greaseweazle, it makes no request. Writing
a disk (`gw write`) makes no network request.

### What each request carries

Every request from PirateFinder sends the User-Agent
`PirateFinder/<version> (+https://github.com/peteclarke-del/PirateFinder)`;
requests for pictures and summaries add a note saying what they are for, as
Wikimedia asks of clients. No cookie, account or identifier is sent. A request
for a picture or a Wikipedia summary tells that site which title you are
looking at, as visiting its page would, and a download tells the provider
which disk you asked for.

PirateFinder sends at most one request a second to each host, waits when a
server asks it to with `Retry-After`, and makes only one picture request at a
time to each site.

## Files PirateFinder keeps

| What | Where | Kept |
| --- | --- | --- |
| Settings | `~/.config/piratefinder/settings.json`, readable only by you | Until removed |
| Library index: one row per image file or archive member, with its path, checksums, volume label, the names of the files on it and what its boot block holds | `~/.local/share/piratefinder/user.sqlite` | Until the file is gone from the library at the next scan |
| A fingerprint of the virus data the library's boot blocks were last checked with, so they are checked again when it changes | Same file | Until the virus data changes |
| Write history: each session with its disks, results and sources | Same file | Until removed |
| Corrections made with Edit Details, with the series, number and dump checksums that find each disc again after a catalogue update, and which disc a cleaned file came from | Same file | Until reverted with Revert to Catalogue, or removed |
| The write queue | `~/.local/share/piratefinder/queue.json` | Until the disks are written or removed from the queue |
| A catalogue installed by an update | `~/.local/share/piratefinder/catalogue.sqlite` | Until replaced by a newer one |
| The Amiga Bootblock Reader brainfile and a note of its release | `~/.local/share/piratefinder/virus/abr/` | Until replaced by a newer one |
| The SPS Decoder Library for IPF (`libcapsimage.so.5`) and a note of its release and source | `~/.local/share/piratefinder/caps/` | Until you choose Remove under IPF Support |
| Pictures and Wikipedia summaries | `~/.cache/piratefinder/media/<source>/` | 30 days, then checked again with the site; a picture a site does not have is remembered for 7 days |
| Downloads being checked | `~/.cache/piratefinder/downloads/` | Until the check, then deleted; a partial download stays so it can be resumed |
| A new PirateFinder package | `~/.cache/piratefinder/updates/` | Until it is installed, then deleted |
| Downloaded disk images | The download folder, `~/Floppy Images/PirateFinder` unless you choose another | Until you delete them |
| Images prepared for writing | A temporary folder named `piratefinder-write-...` | Until the disk is written |
| The Diagnostic Log | Memory only | Until PirateFinder closes |

The folders follow `XDG_CONFIG_HOME`, `XDG_DATA_HOME` and `XDG_CACHE_HOME`
when they are set. Removing the package leaves all of them in place. Delete
`~/.config/piratefinder`, `~/.local/share/piratefinder` and
`~/.cache/piratefinder` to remove everything PirateFinder stored; your disk
images are not in any of them. Deleting `~/.cache/piratefinder` alone is
always safe.

PirateFinder writes to your library folders only when you confirm Clean the
Stored Image, which rewrites or adds an image file and keeps the original. See
[Viruses](USER_GUIDE.md#viruses) in the User Guide.

## Turning network use off

- Online Downloads off stops every disk image, picture and summary download.
- Download Screenshots and Background Information off stops pictures and
  summaries only.
- Each provider has its own switch.
- Check for Updates at Start off stops the check at start.
- The catalogue update, the application update, the brainfile and the SPS
  Decoder Library are checked for and downloaded only when you ask.

With all of these off, PirateFinder makes no request of its own, and Online
Downloads off also keeps the Greaseweazle host tool's firmware check, above,
from leaving the computer.

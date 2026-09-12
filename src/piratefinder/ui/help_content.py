"""Text of the in-application User Guide, and other small data lists.

Kept apart from the widgets so it can be reviewed and tested as text.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class HelpSection:
    heading: str
    paragraphs: tuple[str, ...]
    steps: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class HelpTopic:
    slug: str
    title: str
    summary: str
    screenshot: str  # file name in piratefinder/data/help, "" for none
    screenshot_alt: str
    sections: tuple[HelpSection, ...]


# Searches offered on the welcome page. Only those that find something in the
# installed catalogue are shown, alongside the names of its largest series.
EXAMPLE_SEARCHES = (
    "Automation 250",
    "Pompey Pirates 51",
    "Dungeon Master",
    "Speedball 2",
)

# Credits in About PirateFinder: (name, web address, what it provides).
DATA_CREDITS = (
    ("TOSEC", "https://www.tosecdev.org", "Disk names and checksums"),
    (
        "Atari Legend",
        "https://www.atarilegend.com",
        "Atari ST menu disk contents, CC BY-NC-SA 4.0",
    ),
    ("D-Bug", "https://d-bug.me", "Automation and D-Bug menu contents"),
    ("Demozoo", "https://demozoo.org", "Demo pack contents"),
    ("Internet Archive", "https://archive.org", "Disk image downloads"),
    ("amigascne.org", "https://www.amigascne.org", "Amiga pack disks and menu texts"),
    ("Steem", "http://steem.atari.st/automation.htm", "Automation list"),
    ("8bitchip", "https://atari.8bitchip.info/MenuDG.html", "Atari ST menu index"),
    ("exxos", "https://www.exxosforum.co.uk/atari/", "Persistence of Vision menus"),
)

SHORTCUTS = (
    (
        "General",
        (
            ("<Control>f", "Search"),
            ("F1", "User Guide"),
            ("<Control>comma", "Preferences"),
            ("<Control>question", "Keyboard Shortcuts"),
            ("<Control>q", "Quit"),
        ),
    ),
    (
        "Pages",
        (("<Alt>1", "Find"), ("<Alt>2", "Queue"), ("<Alt>3", "Library"), ("<Alt>4", "History")),
    ),
    (
        "Disks",
        (
            ("<Control>Return", "Write the selected disks now"),
            ("<Control>plus", "Add the selected disks to the queue"),
        ),
    ),
)

HELP_TOPICS = (
    HelpTopic(
        "overview",
        "Getting Started",
        "What PirateFinder does and how the window is laid out.",
        "find.png",
        "The Find page with search results and the details of one disk",
        (
            HelpSection(
                "What it does",
                (
                    "PirateFinder keeps a catalogue of what is on the numbered menu disks, "
                    "compacts and packs released by Amiga and Atari ST crews, and of single "
                    "cracked games. You search for a game, a crew or a disk number, pick the "
                    "disks you want, and PirateFinder writes them to real floppies with a "
                    "Greaseweazle.",
                    "Images are taken from your own folders first. A disk that is not in your "
                    "folders can be downloaded from the online providers you allow, and is kept "
                    "in your download folder for next time.",
                ),
            ),
            HelpSection(
                "The window",
                (
                    "Find searches the catalogue and your library. Queue holds the disks waiting "
                    "to be written and shows progress while they are written. Library lists the "
                    "folders PirateFinder looks in. History lists past writing sessions.",
                    "The main menu, at the right of the header bar, holds Preferences, Update "
                    "Catalogue, Scan Library, the Diagnostic Log, this guide and About.",
                    "A bar under the header appears when no Greaseweazle is connected. You can "
                    "still search, queue and download; writing starts once the device is "
                    "connected. PirateFinder checks for the device every few seconds.",
                ),
            ),
        ),
    ),
    HelpTopic(
        "searching",
        "Searching",
        "Find disks by game, crew, disk number or file name.",
        "find.png",
        "Search results with matching titles in bold",
        (
            HelpSection(
                "What you can search for",
                (
                    "Type a game title, a crew or series name, a series and a number, or "
                    "several words. Results appear as you type. Matching titles are shown in "
                    "bold in the line under each disk.",
                    "Files in your library that did not match any catalogue disk are searched "
                    "too, by file name, volume label and the names of the files on the disk. "
                    "They are listed as unmatched files.",
                ),
            ),
            HelpSection(
                "Filters",
                (
                    "The platform list limits results to Amiga or Atari ST disks. The kind "
                    "buttons limit them to menu disks, packs, single disks or compilations; "
                    "with none pressed every kind is shown. Available Only hides disks that "
                    "are neither in your library nor downloadable.",
                    "Each result shows whether the disk is Local (in your library), Online "
                    "(downloadable from an enabled provider) or Missing.",
                ),
            ),
            HelpSection(
                "Disk details",
                (
                    "Select a result to see its contents, credits, the known dumps and "
                    "reference links. When a disk has several dumps you can choose which one "
                    "to write; Best Available lets PirateFinder pick, preferring good dumps in "
                    "your library.",
                    "The menu beside Add to Queue has Download Only, Show in Files and Copy "
                    "Label Text, which copies a short line for the floppy label such as the "
                    "disk name followed by its main titles.",
                ),
            ),
        ),
    ),
    HelpTopic(
        "writing",
        "Queueing and Writing",
        "Write one disk now, or queue many and write them in one session.",
        "queue-running.png",
        "The Queue page while a disk is being written",
        (
            HelpSection(
                "Choosing disks",
                (
                    "Tick the check boxes of the results you want, then choose Add to Queue or "
                    "Write Now in the bar at the bottom. Write Now writes the ticked disks "
                    "straight away; Add to Queue keeps them for later. The number on the Queue "
                    "page shows how many disks are waiting.",
                    "On the Queue page, move disks up and down to set the order, set how many "
                    "copies of each to write, and choose the drive.",
                ),
            ),
            HelpSection(
                "A writing session",
                (
                    "Choose Start Writing. For each disk PirateFinder finds the image, "
                    "downloads it if needed, prepares it and asks you to insert a floppy. "
                    "Everything on that floppy is overwritten.",
                ),
                (
                    "Insert a blank or unwanted floppy when asked, then choose Write.",
                    "Choose Skip to leave that disk out, or Stop to end the session.",
                    "Watch the track progress. Retries and conversions are shown as they happen.",
                    "When the session ends, the summary lists every disk with its result, "
                    "whether it was verified, and where the image came from.",
                ),
            ),
            HelpSection(
                "After a session",
                (
                    "Retry Failed puts the disks that failed through a new session. Copy Report "
                    "copies the summary as text. Queued disks that were written are taken off "
                    "the queue; the others stay for another try. Every session is kept on the "
                    "History page.",
                ),
            ),
        ),
    ),
    HelpTopic(
        "library",
        "Library Folders",
        "Tell PirateFinder where your disk images are.",
        "library.png",
        "The Library page with folders, counts and unmatched files",
        (
            HelpSection(
                "Folders and scanning",
                (
                    "Add the folders that hold your disk images on the Library page. Folders on "
                    "a NAS work as long as they are mounted. PirateFinder looks inside zip and "
                    "7z archives as well as plain image files.",
                    "Scan Now reads new and changed files. Each image is identified by its "
                    "checksum, so a file matches the catalogue whatever it is called. MSA, DMS "
                    "and ADZ files are decoded before they are checked, so they match the "
                    "catalogue entry for the same disk.",
                ),
            ),
            HelpSection(
                "Unmatched files",
                (
                    "Images that match no catalogue disk are listed as unmatched. They stay "
                    "searchable and can be written like any other disk. A file that differs by "
                    "one byte from the catalogue dump, such as a disk that was booted with the "
                    "write-protect tab open, will not match.",
                ),
            ),
        ),
    ),
    HelpTopic(
        "downloads",
        "Downloads and Sources",
        "Where missing disks come from and how to control it.",
        "preferences.png",
        "Preferences with the download folder and online providers",
        (
            HelpSection(
                "Online providers",
                (
                    "Nothing is downloaded until you ask for a disk that is not in your "
                    "library. Each provider can be switched off in Preferences, and online "
                    "downloads can be switched off entirely.",
                    "Menu disks contain copyrighted software, and downloads come from "
                    "third-party sites that PirateFinder does not run. Every download is "
                    "checked against the checksum in the catalogue before it is kept, and kept "
                    "in your download folder so it is local next time.",
                    "The download folder is laid out for archiving: platform, then type "
                    "(Games, Applications, Demos or Music), then crew, for example "
                    "Atari ST/Games/Automation. A single disk is filed under the crew that "
                    "cracked it, or its publisher when it was never cracked.",
                ),
            ),
            HelpSection(
                "The catalogue",
                (
                    "The catalogue is built from public sources such as TOSEC, Atari Legend, "
                    "D-Bug and the Internet Archive; About PirateFinder lists them. Update "
                    "Catalogue in the main menu downloads a newer catalogue when one has been "
                    "published. Your library and history are kept separately and are not "
                    "affected by an update.",
                ),
            ),
        ),
    ),
    HelpTopic(
        "formats",
        "Formats and Conversions",
        "How each kind of image is prepared for writing.",
        "summary.png",
        "A session summary listing each disk with its result and source",
        (
            HelpSection(
                "Amiga images",
                (
                    "ADF files are written as they are. An ADF with 81 to 84 cylinders is "
                    "written with its real cylinder count. DMS files are decoded to ADF first, "
                    "and ADZ and gzip files are decompressed first.",
                ),
            ),
            HelpSection(
                "Atari ST images",
                (
                    "ST files are written with the geometry in their boot sector, checked "
                    "against the file size. Disks with 82 or 83 cylinders, 11 sectors a track "
                    "or a single side are written with a matching disk definition, so no track "
                    "is dropped. MSA files are decoded to ST first.",
                    "STX (Pasti) images are converted to ST only when they carry no copy "
                    "protection. A protected STX cannot be written as a normal disk; "
                    "PirateFinder says so and offers another dump when the catalogue has one.",
                ),
            ),
            HelpSection(
                "Flux images",
                (
                    "SCP and HFE files are written directly. The Greaseweazle cannot verify "
                    "a flux write, so the summary shows those disks as written, not verified. "
                    "IPF files are written directly when the CAPS library is installed.",
                ),
            ),
        ),
    ),
    HelpTopic(
        "troubleshooting",
        "Troubleshooting",
        "Write-protected disks, drive selection and other problems.",
        "",
        "",
        (
            HelpSection(
                "The disk is write-protected",
                (
                    "A 3.5 inch floppy can only be written when its write-protect tab covers "
                    "the hole. Slide the tab so the hole is closed, insert the disk again and "
                    "choose Write.",
                ),
            ),
            HelpSection(
                "No index, or no disk found",
                (
                    "The Greaseweazle reports no index when the drive is not turning a disk. "
                    "Check that a disk is fully inserted, that the drive has power, that the "
                    "ribbon cable is the right way round and that the drive choice matches the "
                    "cable.",
                ),
            ),
            HelpSection(
                "Choosing the drive",
                (
                    "With a PC floppy cable that has a twist, choose A for the drive after the "
                    "twist and B for the drive before it. With a straight cable, or a drive "
                    "jumpered for a drive select line, choose 0, 1 or 2 to match the jumper. "
                    "The drive is set on the Queue page and in Preferences.",
                ),
            ),
            HelpSection(
                "Verification failures",
                (
                    "A track that does not verify after the retries usually means a worn "
                    "floppy or dirty drive heads. Try another floppy first. High-density (1.44 "
                    "MB) floppies are less reliable when written as double-density disks; use "
                    "double-density floppies where you can.",
                ),
            ),
            HelpSection(
                "The Greaseweazle is not found",
                (
                    "Check the USB cable and that no other program is using the device. Your "
                    "account needs access to the serial device; the PirateFinder package "
                    "installs a rule for this, otherwise add your account to the dialout group "
                    "and log in again. Preferences has a Check Connection button and a device "
                    "path for when automatic detection picks the wrong port.",
                ),
            ),
            HelpSection(
                "Diagnostic Log",
                (
                    "The Diagnostic Log in the main menu shows recent Greaseweazle output and "
                    "messages from searches, scans and downloads. Copy it when asking for "
                    "help. It is kept in memory only and cleared when PirateFinder closes.",
                ),
            ),
        ),
    ),
)

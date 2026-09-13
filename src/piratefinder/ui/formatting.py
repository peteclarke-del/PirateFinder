"""Text shown by the interface, kept free of GTK so it can be tested alone."""

from __future__ import annotations

import html
import re
from collections.abc import Iterable, Sequence
from datetime import datetime
from pathlib import Path

from ..catalogue.naming import display_title
from ..images.archives import base_name
from ..images.inspect import local_platform, suffix_of
from ..library.library import clean_names, local_name
from ..models import (
    Availability,
    BootRecheck,
    Content,
    ContentKind,
    Disk,
    DiskKind,
    LocalFile,
    MediaItem,
    Platform,
    ResultMode,
    ResultPage,
    ScanSummary,
    SessionSummary,
    SortOrder,
    TriviaItem,
    VirusReport,
    VirusStatus,
    WriteOutcome,
    WriteProgress,
    WriteStatus,
)

PLATFORM_NAMES = {Platform.AMIGA: "Amiga", Platform.ATARI_ST: "Atari ST"}

# Disc kinds on the Find page, in display order.
KIND_FILTERS = (
    (DiskKind.MENU, "Menu Disks", "Numbered crew menu disks and game compacts"),
    (DiskKind.PACK, "Packs", "Demo, music, utility and trainer packs"),
    (DiskKind.SINGLE, "Single Disks", "One release per disk, usually a single crack"),
    (DiskKind.COMPILATION, "Compilations", "Commercial and other compilations"),
)

# The Sort drop-down on the Find page, in display order.
SORT_CHOICES = (
    (SortOrder.RELEVANCE, "Relevance"),
    (SortOrder.TITLE, "Title A to Z"),
    (SortOrder.TITLE_DESC, "Title Z to A"),
    (SortOrder.YEAR, "Year, Oldest First"),
    (SortOrder.YEAR_DESC, "Year, Newest First"),
    (SortOrder.DISC, "Disc Number"),
    (SortOrder.CREW, "Crew"),
    (SortOrder.PLATFORM, "Platform"),
)
PAGE_SIZES = (50, 100, 200, 500)
MONTHS = (
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
)
MEDIA_KIND_NAMES = {
    "menu": "Menu screen",
    "intro": "Intro screen",
    "snap": "Screenshot",
    "title": "Title screen",
    "boxart": "Box art",
    "demo": "Demo screen",
}
VIRUS_KIND_NAMES = {
    "boot": "a boot block virus",
    "file": "a file virus",
    "link": "a link virus",
    "system": "a system virus",
}
KIND_NAMES = {
    DiskKind.MENU: "Menu disk",
    DiskKind.PACK: "Pack",
    DiskKind.SINGLE: "Single disk",
    DiskKind.COMPILATION: "Compilation",
}
CONTENT_KIND_NAMES = {
    ContentKind.GAME: "Game",
    ContentKind.DEMO: "Demo",
    ContentKind.INTRO: "Intro",
    ContentKind.UTILITY: "Utility",
    ContentKind.MUSIC: "Music",
    ContentKind.DOC: "Documentation",
    ContentKind.CHEAT: "Cheat",
    ContentKind.TRAINER: "Trainer",
    ContentKind.OTHER: "Other",
}
AVAILABILITY_NAMES = {
    Availability.LOCAL: "Local",
    Availability.ONLINE: "Online",
    Availability.MISSING: "Missing",
}
AVAILABILITY_ICONS = {
    Availability.LOCAL: "folder-symbolic",
    Availability.ONLINE: "network-server-symbolic",
    Availability.MISSING: "",
}
AVAILABILITY_TOOLTIPS = {
    Availability.LOCAL: "An image is in one of your library folders",
    Availability.ONLINE: "An image can be downloaded from an enabled provider",
    Availability.MISSING: "No image is known in your library or online",
}

# Drive choices passed to gw --drive, with what each one means.
DRIVES = (
    ("A", "Drive A", "PC cable, the drive after the twist"),
    ("B", "Drive B", "PC cable, the drive before the twist"),
    ("0", "Drive 0", "Shugart bus, drive select 0"),
    ("1", "Drive 1", "Shugart bus, drive select 1"),
    ("2", "Drive 2", "Shugart bus, drive select 2"),
    ("3", "Drive 3", "Shugart bus, drive select 3"),
)

STATUS_TEXT = {
    WriteStatus.VERIFIED: "Written and verified",
    WriteStatus.WRITTEN: "Written, not verified",
    WriteStatus.FAILED: "Failed",
    WriteStatus.WRITE_PROTECTED: "Disk is write-protected",
    WriteStatus.NO_DISK: "No disk in the drive",
    WriteStatus.CANCELLED: "Cancelled",
    WriteStatus.SKIPPED: "Skipped",
    WriteStatus.UNAVAILABLE: "No usable image",
}
STATUS_ICONS = {
    WriteStatus.VERIFIED: "emblem-ok-symbolic",
    WriteStatus.WRITTEN: "emblem-ok-symbolic",
    WriteStatus.FAILED: "dialog-error-symbolic",
    WriteStatus.WRITE_PROTECTED: "dialog-warning-symbolic",
    WriteStatus.NO_DISK: "dialog-warning-symbolic",
    WriteStatus.CANCELLED: "process-stop-symbolic",
    WriteStatus.SKIPPED: "media-skip-forward-symbolic",
    WriteStatus.UNAVAILABLE: "dialog-question-symbolic",
}
STATUS_STYLE = {
    WriteStatus.VERIFIED: "success",
    WriteStatus.WRITTEN: "success",
    WriteStatus.FAILED: "error",
    WriteStatus.WRITE_PROTECTED: "warning",
    WriteStatus.NO_DISK: "warning",
    WriteStatus.UNAVAILABLE: "warning",
}

# Sentences for the insert prompt when a disk is asked for again.
RETRY_REASONS = {
    "write-protected": (
        "The disk is write-protected. Slide the tab to cover the hole and insert it again."
    ),
    "no-disk": "No disk was found in the drive. Insert the disk fully and try again.",
    "failed": "Writing that disk failed. Try another floppy.",
    "copy": "Insert the next blank floppy for another copy.",
}

STAGE_TEXT = {
    "resolve": "Finding image",
    "find": "Finding image",
    "download": "Downloading",
    "prepare": "Preparing",
    "insert": "Waiting for the disk",
    "write": "Writing",
    "verify": "Verifying",
    "done": "Finished",
}

STICKER_KINDS = (ContentKind.GAME, ContentKind.DEMO, ContentKind.MUSIC, ContentKind.UTILITY)


def platform_name(platform: Platform | str | None) -> str:
    if platform is None or platform == "":
        return ""
    try:
        return PLATFORM_NAMES[Platform(platform)]
    except ValueError:
        return str(platform)


def plural(count: int, singular: str, many: str | None = None) -> str:
    """ "1 disk", "1,234 disks"."""
    word = singular if count == 1 else (many or singular + "s")
    return f"{count:,} {word}"


def duration_text(seconds: float) -> str:
    """ "about 12 hours", "about 40 minutes" or "under a minute", for an estimate."""
    if seconds < 60:
        return "under a minute"
    minutes = round(seconds / 60)
    if minutes < 90:
        return f"about {minutes} minute{'s' if minutes != 1 else ''}"
    # From an hour and a half up, whole hours; never "1 hours".
    return f"about {max(2, round(seconds / 3600))} hours"


def human_size(size: int | None) -> str:
    if size is None:
        return ""
    if size < 1024:
        return plural(size, "byte")
    value = float(size)
    for unit in ("KB", "MB", "GB", "TB"):
        value /= 1024
        if value < 1024 or unit == "TB":
            return f"{value:.0f} {unit}" if value >= 100 else f"{value:.1f} {unit}"
    return f"{size} bytes"


def format_timestamp(value: str) -> str:
    """An ISO 8601 time as "12 Sep 2026, 14:03"; other text is returned as it is."""
    if not value:
        return ""
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        return value
    if moment.tzinfo is not None:
        moment = moment.astimezone()
    if len(value) <= 10:
        return f"{moment.day} {moment:%b %Y}"
    return f"{moment.day} {moment:%b %Y, %H:%M}"


def escape(text: str) -> str:
    """Escape text for Pango markup."""
    return html.escape(text or "", quote=False)


def summary_markup(summary: str, matched: Sequence[str]) -> str:
    """Pango markup for a result's contents line with matched titles in bold.

    Every piece of text is escaped. A matched title missing from the summary
    (because the summary was shortened) is put in front of it.
    """
    summary = summary or ""
    titles = [title for title in dict.fromkeys(matched) if title and title.strip()]
    spans: list[tuple[int, int]] = []
    missing: list[str] = []
    lowered = summary.lower()
    for title in sorted(titles, key=len, reverse=True):
        found = False
        for hit in re.finditer(re.escape(title.lower()), lowered):
            found = True  # even when a longer matched title already covers it
            start, end = hit.span()
            if any(start < other_end and other_start < end for other_start, other_end in spans):
                continue
            spans.append((start, end))
        if not found:
            missing.append(title)
    spans.sort()
    parts: list[str] = []
    position = 0
    for start, end in spans:
        parts.append(escape(summary[position:start]))
        parts.append(f"<b>{escape(summary[start:end])}</b>")
        position = end
    parts.append(escape(summary[position:]))
    body = "".join(parts)
    if missing:
        prefix = ", ".join(f"<b>{escape(title)}</b>" for title in missing)
        body = f"{prefix} • {body}" if body else prefix
    return body


def disk_subtitle(disk: Disk) -> str:
    """ "Automation • June 1991 • Atari ST • Menu disk" under a disk title."""
    date = disk_release(disk) if disk.year else ""
    parts = [disk.series_name, date, platform_name(disk.platform), KIND_NAMES.get(disk.kind)]
    return " • ".join(part for part in parts if part)


def content_subtitle(content: Content) -> str:
    parts = [CONTENT_KIND_NAMES.get(content.kind, "")]
    if content.cracker:
        parts.append(f"cracked by {content.cracker}")
    if content.publisher:
        parts.append(content.publisher)
    if content.version:
        parts.append(content.version)
    if content.extra:
        parts.append(content.extra)
    return " • ".join(part for part in parts if part)


def sticker_text(label: str, contents: Iterable[Content], max_chars: int = 60) -> str:
    """A short line for a floppy label: "Automation 250: Necron, Boulderdash CK".

    Programs are listed in menu order; documents, intros and trainers are left
    out when there is anything else. Titles that do not fit are counted.
    """
    items = sorted(contents, key=lambda content: content.position)
    chosen = [content for content in items if content.kind in STICKER_KINDS] or items
    titles = list(dict.fromkeys(display_title(c.title) for c in chosen if c.title))
    if not titles:
        return label
    text = f"{label}: "
    shown = 0
    for title in titles:
        candidate = title if shown == 0 else f", {title}"
        remaining = len(titles) - shown - 1
        suffix = f" +{remaining} more" if remaining else ""
        if shown and len(text) + len(candidate) + len(suffix) > max_chars:
            break
        text += candidate
        shown += 1
    if shown < len(titles):
        text += f" +{len(titles) - shown} more"
    return text


def local_file_location(local: LocalFile) -> str:
    return f"{local.path} › {local.member}" if local.member else local.path


def provider_name(provider: str, names: dict[str, str] | None = None) -> str:
    """The display name of a provider or source id; a name already spelt out is kept."""
    if names and provider in names:
        return names[provider]
    if provider != provider.lower():
        return provider  # "TOSEC", "Amiga Bootblock Reader"
    return " ".join(word.capitalize() for word in re.split(r"[-_ ]+", provider) if word)


def stage_text(stage: str, message: str = "") -> str:
    known = STAGE_TEXT.get(stage.lower().strip(), "")
    if known and message and stage.lower() not in ("write", "verify"):
        return message
    return known or message or stage.capitalize()


def download_text(done: int, total: int | None) -> str:
    if total:
        return f"Downloading {min(100, int(done * 100 / total))}%"
    return f"Downloading {human_size(done)}"


def track_text(progress: WriteProgress) -> str:
    text = f"Track {progress.cylinder}.{progress.head}"
    if progress.track_count:
        text += f" • {progress.track_number} of {progress.track_count} track sides"
    return text


def insert_heading(label: str) -> str:
    return f"Insert a Disk for {label}"


def insert_body(index: int, total: int, drive: str, reason: str = "") -> str:
    body = f"Disk {index} of {total}, drive {drive}. Everything on the floppy will be overwritten."
    reason_text = RETRY_REASONS.get(reason.strip().lower(), reason.strip()) if reason else ""
    if reason_text:
        body = f"{reason_text}\n\n{body}"
    return body


def outcome_text(outcome: WriteOutcome) -> str:
    text = STATUS_TEXT.get(outcome.status, str(outcome.status))
    if outcome.summary and outcome.summary.rstrip(".") != text:
        return f"{text}. {outcome.summary}" if not text.endswith(".") else text
    return text


def outcome_details(outcome: WriteOutcome, source: str) -> str:
    parts = []
    if outcome.retries:
        parts.append(plural(outcome.retries, "retry", "retries"))
    if outcome.failed_tracks:
        parts.append("failed tracks " + ", ".join(outcome.failed_tracks[:8]))
    if source:
        parts.append(source)
    return " • ".join(parts)


def outcome_subtitle(outcome: WriteOutcome, source: str) -> str:
    """The lines under a disk in a summary: the result, the details, then each note."""
    lines = [outcome_text(outcome), outcome_details(outcome, source), *outcome.notes]
    return "\n".join(line for line in lines if line)


def session_headline(summary: SessionSummary) -> str:
    """ "4 of 5 disks written and verified"."""
    total = len(summary.items)
    verified = summary.count(WriteStatus.VERIFIED)
    written = summary.count(WriteStatus.WRITTEN)
    done = verified + written
    if total == 0:
        return "No disks were written"
    if written == 0:
        return f"{done} of {plural(total, 'disk')} written and verified"
    if verified == 0:
        return f"{done} of {plural(total, 'disk')} written, without verification"
    return (
        f"{done} of {plural(total, 'disk')} written, {verified:,} verified and "
        f"{written:,} without verification"
    )


def session_title(summary: SessionSummary) -> str:
    total = len(summary.items)
    done = summary.count(WriteStatus.VERIFIED, WriteStatus.WRITTEN)
    if total and done == total:
        return "All Disks Written"
    if summary.count(WriteStatus.CANCELLED) and done < total:
        return "Writing Stopped"
    if done == 0:
        return "No Disks Written"
    return "Some Disks Not Written"


def failed_labels(summary: SessionSummary) -> list[str]:
    return [
        label
        for label, outcome, _source in summary.items
        if not outcome.succeeded and outcome.status != WriteStatus.SKIPPED
    ]


def boot_recheck_text(result: BootRecheck) -> str:
    """What the window says after the library's boot blocks were checked again.

    "The virus data changed, so 1,234 boot blocks were checked again. The
    result changed for 3 images."
    """
    if result.cancelled:
        return (
            f"The boot block check stopped after {plural(result.checked, 'image')}. It starts "
            "again when PirateFinder next starts."
        )
    were = "was" if result.checked == 1 else "were"
    text = (
        f"The virus data changed, so {plural(result.checked, 'boot block')} {were} checked "
        f"again. The result changed for {plural(result.changed, 'image')}."
    )
    if result.unreadable:
        text += (
            f" {plural(result.unreadable, 'image')} could not be read; the next scan reads them."
        )
    return text


def scan_toast(summary: ScanSummary) -> str:
    """ "Found 1,234 images: 1,100 matched the catalogue, 134 did not"."""
    if summary.cancelled:
        return f"Scan stopped after {plural(summary.images_found, 'image')}"
    if summary.images_found == 0:
        return "No disk images were found in the library folders"
    text = (
        f"Found {plural(summary.images_found, 'image')}: {summary.matched:,} matched "
        f"the catalogue, {summary.unmatched:,} did not"
    )
    if summary.errors:
        text += f" ({plural(len(summary.errors), 'file')} could not be read)"
    return text


def catalogue_stats_text(stats: dict[str, int], built_at: str) -> str:
    """ "12,345 disks in 180 series, 30,000 titles. Built 1 Sep 2026."."""
    parts = []
    disks = stats.get("disks")
    if disks is not None:
        text = plural(disks, "disk")
        if stats.get("series"):
            text += f" in {plural(stats['series'], 'series', 'series')}"
        parts.append(text)
    if stats.get("contents"):
        parts.append(plural(stats["contents"], "title"))
    if stats.get("images"):
        parts.append(plural(stats["images"], "known dump"))
    text = ", ".join(parts)
    if built_at:
        built = f"Built {format_timestamp(built_at)}."
        text = f"{text}. {built}" if text else built
    elif text:
        text += "."
    return text


# The Find screen


def query_words(text: str) -> list[str]:
    """The words of a search, lower case, as the catalogue matches them."""
    return [word for word in re.findall(r"\w+", (text or "").lower()) if word]


def highlight_markup(text: str, words: Sequence[str]) -> str:
    """Pango markup for ``text`` with every word that starts with a query word in bold.

    The catalogue matches each query word as a prefix of a word in the row,
    so "rick" makes "Rick" bold in "Rick Dangerous" but not in "Brick".
    """
    text = text or ""
    spans: list[tuple[int, int]] = []
    if words:
        for match in re.finditer(r"\w+", text):
            token = match.group(0).lower()
            best = max((len(word) for word in words if token.startswith(word)), default=0)
            if best:
                spans.append((match.start(), match.start() + best))
    parts: list[str] = []
    position = 0
    for start, end in spans:
        parts.append(escape(text[position:start]))
        parts.append(f"<b>{escape(text[start:end])}</b>")
        position = end
    parts.append(escape(text[position:]))
    return "".join(parts)


def range_text(page: ResultPage) -> str:
    """ "101 to 200 of 1,234 titles" for the pager."""
    noun = "title" if page.query.mode == ResultMode.TITLES else "disc"
    if page.total == 0:
        return f"No {noun}s"
    first = page.query.page * page.query.page_size + 1
    last = min(page.total, first + len(page.rows) - 1)
    if first > page.total or not page.rows:
        return plural(page.total, noun)
    if first == 1 and last == page.total:
        return plural(page.total, noun)
    return f"{first:,} to {last:,} of {plural(page.total, noun)}"


def selection_text(rows: int, discs: int, pages: int) -> str:
    """ "3 selected", "3 titles selected on 2 discs", "... across 2 pages"."""
    if rows != discs:
        text = f"{plural(rows, 'title')} selected on {plural(discs, 'disc')}"
    else:
        text = f"{rows:,} selected"
    if pages > 1:
        text += f" across {pages:,} pages"
    return text


def release_text(year: int | None, month: int | None = None, day: int | None = None) -> str:
    """ "17 June 1989", "June 1989", "1989" or "Unknown"."""
    if not year:
        return "Unknown"
    if month and 1 <= month <= 12:
        name = MONTHS[month - 1]
        if day and 1 <= day <= 31:
            return f"{day} {name} {year}"
        return f"{name} {year}"
    return str(year)


def disk_release(disk: Disk) -> str:
    """The release date of a disc as precisely as the catalogue knows it."""
    return release_text(disk.year, disk.month, disk.day)


def disk_year(disk: Disk) -> str:
    return str(disk.year) if disk.year else ""


def media_caption(item: MediaItem, index: int, count: int, subject: str = "") -> str:
    """ "Menu screen of Automation 250, 1 of 3"."""
    kind = MEDIA_KIND_NAMES.get(item.kind, item.kind.capitalize() or "Picture")
    text = f"{kind} of {subject}" if subject else kind
    if count > 1:
        text += f", {index + 1} of {count}"
    return text


def media_credit(item: MediaItem, names: dict[str, str] | None = None) -> str:
    """The credit line under a picture, naming its source once."""
    source = provider_name(item.source, names) if item.source else ""
    if item.credit and source and source.casefold() not in item.credit.casefold():
        return f"{item.credit}, {source}"
    return item.credit or source


def trivia_credit(item: TriviaItem, names: dict[str, str] | None = None) -> str:
    """ "From Wikipedia, Rick Dangerous, CC BY-SA 4.0" or "Source: Atari Legend"."""
    source = provider_name(item.source, names) if item.source else ""
    if item.kind == "summary":
        parts = [f"From {source or 'Wikipedia'}"]
        if item.title:
            parts.append(item.title)
        if item.licence:
            parts.append(item.licence)
        return ", ".join(parts)
    text = f"Source: {source}" if source else ""
    if item.licence:
        text = f"{text}, {item.licence}" if text else item.licence
    return text


# A boot block that is not a virus, shown for information in the details pane.
BOOT_BLOCK_TEXT = {
    VirusStatus.ANTIVIRUS: "{name}, an anti-virus boot block. It is not a virus.",
    VirusStatus.KNOWN_BOOT: "{name}. It is not a virus.",
    VirusStatus.UNKNOWN_BOOT: (
        "Boot code PirateFinder cannot identify. Many games and crews booted their own code, "
        "so this is not a sign of a virus by itself."
    ),
}


def boot_block_text(report: VirusReport | None) -> str:
    """What a boot block that is not a virus holds; "" for a clean or infected one."""
    if report is None or report.status not in BOOT_BLOCK_TEXT:
        return ""
    if report.explanation.strip():
        return report.explanation.strip()
    return BOOT_BLOCK_TEXT[report.status].format(name=report.name or "A named boot block")


_DRAWN = re.compile(r"[^A-Za-z0-9\s]{4,}")  # ----, ====, ___/__ and the like
_SPACED = re.compile(r"\S {3,}\S|^ {3,}\S")  # words placed with runs of spaces


def laid_out(text: str) -> bool:
    """Whether ``text`` is laid out in columns or drawn with symbols (a menu screen, ASCII
    art), so that it reads right only in a fixed-width font with its lines kept."""
    lines = [line for line in text.splitlines() if line.strip()]
    return sum(1 for line in lines if _DRAWN.search(line) or _SPACED.search(line)) >= 2


def virus_heading(report: VirusReport) -> str:
    name = report.name or "an unnamed virus"
    if report.status == VirusStatus.FLAGGED:
        return f"Dump Flagged with {name}"
    return f"Virus Found: {name}"


def virus_body(report: VirusReport, names: dict[str, str] | None = None) -> str:
    """What the virus is and what can be done: the detector's own words when it gives them."""
    if report.explanation.strip():
        return report.explanation.strip()
    sentences = []
    kind = VIRUS_KIND_NAMES.get(report.kind, "")
    source = provider_name(report.source, names) if report.source else ""
    if report.status == VirusStatus.FLAGGED:
        where = f"{source} lists" if source else "The catalogue lists"
        sentences.append(
            f"{where} this dump as carrying {report.name or 'a virus'}"
            + (f", {kind}." if kind else ".")
        )
    elif kind:
        sentences.append(f"{report.name or 'This'} is {kind}.")
    if report.removable:
        sentences.append(
            "Removing it writes a standard boot block in its place. The files on the disc "
            "are not touched."
        )
    elif report.kind in ("file", "link") or report.status == VirusStatus.FLAGGED:
        sentences.append(
            "It lives in the files on the disc, not in the boot block, so PirateFinder "
            "cannot remove it. Write a clean dump instead when there is one."
        )
    else:
        sentences.append("PirateFinder cannot remove it safely from this disc.")
    return " ".join(sentences)


def virus_source(report: VirusReport, names: dict[str, str] | None = None) -> str:
    """ "Identified by Amiga Bootblock Reader", "Listed by TOSEC"."""
    if not report.source:
        return ""
    if report.source == "built-in":
        return "Identified by PirateFinder's built-in signatures"
    name = provider_name(report.source, names)
    verb = "Listed" if report.status == VirusStatus.FLAGGED else "Identified"
    return f"{verb} by {name}"


def cleaned_text(original: LocalFile, cleaned: LocalFile) -> str:
    """What a finished clean did: rewritten with a backup, or saved as a new copy."""
    if (cleaned.path, cleaned.member) == (original.path, original.member):
        return f"Cleaned {local_name(cleaned)}. The original was kept as a .bak file beside it."
    return f"Saved a cleaned copy as {local_name(cleaned)}. The original is unchanged."


def clean_explanation(local: LocalFile, virus: str, platform: Platform | None = None) -> str:
    """What cleaning a library file does to it, for the confirmation dialog.

    A new file is named for the platform its format says, or ``platform``
    (the disc's) when the format does not say.
    """
    what = f"the {virus} virus" if virus else "the virus"
    if local.member:
        return (
            f"{base_name(local.member)} is inside {Path(local.path).name}, which is not changed. "
            f"PirateFinder removes {what} and saves the cleaned disk as a new file in your "
            "download folder."
        )
    name = Path(local.path).name
    image_format = local.format or suffix_of(name).lstrip(".")
    names = clean_names(local, image_format, local_platform(local) or platform)
    if names.backup:
        return (
            f"PirateFinder writes a standard boot block over {what} in {name}. The original "
            f"file is kept beside it as {names.backup}, so nothing is lost."
        )
    return (
        f"PirateFinder removes {what} and saves the cleaned disk beside {name} as "
        f"{names.cleaned}. The original file is not changed."
    )

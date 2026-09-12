"""Text shown by the interface, kept free of GTK so it can be tested alone."""

from __future__ import annotations

import html
import re
import uuid
from collections.abc import Iterable, Sequence
from datetime import datetime
from pathlib import Path

from ..catalogue.naming import display_title
from ..models import (
    Availability,
    Content,
    ContentKind,
    Disk,
    DiskKind,
    LocalFile,
    Platform,
    QueueItem,
    ScanSummary,
    SessionSummary,
    WriteOutcome,
    WriteProgress,
    WriteStatus,
)

PLATFORM_NAMES = {Platform.AMIGA: "Amiga", Platform.ATARI_ST: "Atari ST"}

# Filter toggles on the Find page, in display order.
KIND_FILTERS = (
    (DiskKind.MENU, "Menu Disks", "Numbered crew menu disks and game compacts"),
    (DiskKind.PACK, "Packs", "Demo, music, utility and trainer packs"),
    (DiskKind.SINGLE, "Single Disks", "One release per disk, usually a single crack"),
    (DiskKind.COMPILATION, "Compilations", "Commercial and other compilations"),
)
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
    """ "Automation, 1990, Atari ST, Menu disk" style line under a disk title."""
    parts = [disk.series_name, disk.date, platform_name(disk.platform), KIND_NAMES.get(disk.kind)]
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


def local_file_name(local: LocalFile) -> str:
    if local.display_name:
        return local.display_name
    if local.member:
        return Path(local.member).name
    return Path(local.path).name


def local_file_location(local: LocalFile) -> str:
    return f"{local.path} › {local.member}" if local.member else local.path


def provider_name(provider: str, names: dict[str, str] | None = None) -> str:
    if names and provider in names:
        return names[provider]
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


def report_text(summary: SessionSummary) -> str:
    """A plain text report, used when the history module offers none."""
    lines = [
        "PirateFinder write report",
        f"Started: {format_timestamp(summary.started)}",
        f"Finished: {format_timestamp(summary.finished)}",
        f"Drive: {summary.drive}",
        session_headline(summary),
        "",
    ]
    for label, outcome, source in summary.items:
        lines.append(f"{label}: {outcome_text(outcome)}")
        details = outcome_details(outcome, source)
        if details:
            lines.append(f"    {details}")
    return "\n".join(lines) + "\n"


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


def new_queue_item(
    label: str,
    platform: Platform | None,
    *,
    disk_id: int | None = None,
    image_id: int | None = None,
    local: LocalFile | None = None,
    copies: int = 1,
) -> QueueItem:
    return QueueItem(
        id=uuid.uuid4().hex,
        label=label,
        platform=platform,
        disk_id=disk_id,
        image_id=image_id,
        local=local,
        copies=max(1, copies),
    )


def queue_key(item: QueueItem) -> tuple[str, ...]:
    """What makes two queue items the same disk, as ``jobs.queue.WriteQueue`` sees it."""
    if item.disk_id is not None:
        return ("disk", str(item.disk_id))
    if item.local is not None:
        return ("file", item.local.path, item.local.member)
    return ("item", item.id)


def fresh_copy(item: QueueItem) -> QueueItem:
    """The same disk as a new queue entry with no outcome, for Retry Failed."""
    return new_queue_item(
        item.label,
        item.platform,
        disk_id=item.disk_id,
        image_id=item.image_id,
        local=item.local,
        copies=item.copies,
    )

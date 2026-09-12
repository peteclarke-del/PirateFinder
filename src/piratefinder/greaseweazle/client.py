"""Probe the Greaseweazle and write prepared images with the ``gw`` host tool.

gw prints everything, including errors, to stderr, and its exit status is
not enough to judge a write: a hardware command error such as a
write-protected disk is printed as ``Command Failed: <reason>`` and gw still
exits 0. The outcome is therefore read from the output, with these rules,
first match wins:

* the caller cancelled: cancelled;
* the timeout expired: failed;
* ``Disk is Write Protected`` in a ``Command Failed`` line or fatal error:
  write-protected;
* ``No Index`` or ``Track 0 not found`` there: no disk;
* any other ``Command Failed``, ``Failed to verify Track c.h``,
  ``** FATAL ERROR`` or a nonzero exit: failed;
* ``All tracks verified``: verified;
* ``N tracks verified; M tracks *not* verified (Reason: Verify unavailable)``
  or ``No tracks verified (Reason: Verify disabled)``: written without
  verification;
* anything else: failed, because gw did not confirm the write.

The message strings are those of Greaseweazle 1.23 (``usb.py`` Ack strings,
``tools/write.py`` and ``cli.py``).
"""

from __future__ import annotations

import os
import re
import shutil
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from ..models import DeviceStatus, Geometry, PreparedImage, WriteOutcome, WriteProgress, WriteStatus
from .runner import OperationController, ProcessResult, run_streaming

#: Where the Debian package keeps its private copy of the host tools.
PACKAGED_GW = Path("/usr/lib/piratefinder/bin/gw")
DRIVES = ("A", "B", "0", "1", "2", "3")
DIAGNOSTIC_LINES = 60

_TRACK = re.compile(r"^T(\d+)\.(\d+)(?:\s*->\s*Drive\s+\d+\.\d+)?:\s*(.*)$")
_RETRY = re.compile(r"Retry #(\d+)")
_TRACKSET = re.compile(r"^Writing c=([0-9,\-]+):h=([0-9,\-]+)")
_PARTIAL = re.compile(
    r"(?:(\d+) tracks verified; (\d+) tracks \*not\* verified|No tracks verified)"
    r"\s*\(Reason: Verify (unavailable|disabled)\)"
)
_FAILED_VERIFY = re.compile(r"Failed to verify Track (\d+)\.(\d+)")
_COMMAND_FAILED = re.compile(r"Command Failed:\s*(.*)")
_INFO_FIELD = re.compile(r"^\s+([A-Za-z ]+):\s*(.*?)\s*$")
_NEW_FIRMWARE = re.compile(r"New firmware version (\S+) is available")

ProgressCallback = Callable[[WriteProgress], None]


def find_gw() -> str | None:
    """The gw command: $PIRATEFINDER_GW, then PATH, then the packaged copy."""
    explicit = os.environ.get("PIRATEFINDER_GW", "")
    if explicit and os.access(explicit, os.X_OK):
        return explicit
    found = shutil.which("gw")
    if found:
        return found
    if os.access(PACKAGED_GW, os.X_OK):
        return str(PACKAGED_GW)
    return None


def probe(timeout: float = 20, *, device: str = "", executable: str | None = None) -> DeviceStatus:
    """Ask ``gw info`` whether a Greaseweazle is connected, and which one.

    ``device`` is the serial port chosen in Preferences; "" lets gw search.
    """
    command = executable or find_gw()
    if command is None:
        return DeviceStatus(False, "The Greaseweazle host tools (gw) are not installed.")
    if not _valid_device(device):
        return DeviceStatus(False, "The Greaseweazle device name is not valid.")
    arguments = [command, "info"] + ([f"--device={device}"] if device else [])
    try:
        result = run_streaming(arguments, timeout=timeout)
    except OSError as error:
        return DeviceStatus(False, f"The Greaseweazle host tools could not be started: {error}.")
    return parse_info(result)


def parse_info(result: ProcessResult) -> DeviceStatus:
    """Read ``gw info`` output. It exits 0 even when the device is not found."""
    lines = result.lines
    fields: dict[str, str] = {}
    host_tools = ""
    in_device = False
    not_found = False
    for line in lines:
        if line.startswith("Host Tools:"):
            host_tools = line.partition(":")[2].strip()
        elif line.strip() == "Device:":
            in_device = True
        elif in_device and line.startswith((" ", "\t")):
            if line.strip().lower() == "not found":
                not_found = True
            match = _INFO_FIELD.match(line)
            if match:
                fields[match.group(1).strip()] = match.group(2)
        elif line.strip():
            in_device = False
    model = fields.get("Model", "")
    firmware = fields.get("Firmware", "")
    port = fields.get("Port", "")
    if not_found or not (model or firmware):
        if result.timed_out:
            message = "The Greaseweazle host tools did not answer in time."
        elif not_found or result.return_code == 0:
            message = "No Greaseweazle is connected."
        else:
            message = f"The Greaseweazle host tools failed: {_last_line(lines)}"
        return DeviceStatus(False, message, host_tools=host_tools)
    # gw info looks up the latest firmware online after printing the device;
    # when that fails it ends with a fatal error, but the device is fine.
    name = model or "Greaseweazle"
    message = f"{name} connected" + (f" on {port}" if port else "") + "."
    if "Bootloader" in firmware:
        message += " It is in firmware update mode."
    newer = next((m.group(1) for m in map(_NEW_FIRMWARE.search, lines) if m), "")
    if newer:
        message += f" Firmware {newer} is available."
    return DeviceStatus(True, message, model, firmware, port, host_tools)


def write(
    prepared: PreparedImage,
    *,
    drive: str,
    device: str = "",
    retries: int = 3,
    pre_erase: bool = False,
    progress: ProgressCallback | None = None,
    controller: OperationController | None = None,
    timeout: float = 1800,
    executable: str | None = None,
) -> WriteOutcome:
    """Write ``prepared`` to the disk in ``drive`` and report what happened."""
    started = time.monotonic()
    command = executable or find_gw()
    if command is None:
        return WriteOutcome(
            WriteStatus.FAILED, "The Greaseweazle host tools (gw) are not installed."
        )
    arguments = write_command(
        command, prepared, drive=drive, device=device, retries=retries, pre_erase=pre_erase
    )
    if isinstance(arguments, WriteOutcome):
        return arguments
    reader = _WriteReader(prepared.geometry, progress)
    try:
        result = run_streaming(
            arguments, timeout=timeout, on_line=reader.feed, controller=controller
        )
    except OSError as error:
        return WriteOutcome(
            WriteStatus.FAILED, f"The Greaseweazle host tools could not be started: {error}."
        )
    outcome = reader.outcome(result, timeout=timeout, retries_allowed=retries)
    diagnostic = "\n".join(["$ " + " ".join(arguments), *result.lines[-DIAGNOSTIC_LINES:]])
    return WriteOutcome(
        outcome.status,
        outcome.summary,
        diagnostic=diagnostic,
        retries=reader.total_retries,
        failed_tracks=outcome.failed_tracks,
        seconds=round(time.monotonic() - started, 1),
    )


def write_command(
    command: str,
    prepared: PreparedImage,
    *,
    drive: str,
    device: str = "",
    retries: int = 3,
    pre_erase: bool = False,
) -> list[str] | WriteOutcome:
    """The gw argument list, or a failed outcome saying why it cannot be built."""
    selected = str(drive).strip().upper()
    if selected not in DRIVES:
        return WriteOutcome(
            WriteStatus.FAILED, f"Drive {drive!r} is not one of A, B, 0, 1, 2 or 3."
        )
    if not _valid_device(device):
        return WriteOutcome(WriteStatus.FAILED, "The Greaseweazle device name is not valid.")
    path = Path(prepared.write_path).absolute()
    if not path.is_file():
        return WriteOutcome(WriteStatus.FAILED, f"The image to write is missing: {path.name}.")
    if "::" in str(path):
        # gw reads "::" in a file argument as the start of file options.
        return WriteOutcome(
            WriteStatus.FAILED, "The image path contains '::', which gw cannot accept."
        )
    arguments = [command, "write", f"--drive={selected}"]
    if device:
        arguments.append(f"--device={device}")
    if prepared.diskdefs_path:
        arguments.append(f"--diskdefs={Path(prepared.diskdefs_path).absolute()}")
    if prepared.gw_format:
        arguments.append(f"--format={prepared.gw_format}")
    if prepared.tracks:
        arguments.append(f"--tracks={prepared.tracks}")
    arguments.append(f"--retries={max(0, int(retries))}")
    if pre_erase:
        arguments.append("--pre-erase")
    arguments.append(str(path))
    return arguments


@dataclass(slots=True)
class _Judgement:
    status: WriteStatus
    summary: str
    failed_tracks: tuple[str, ...] = ()


@dataclass(slots=True)
class _WriteReader:
    """Follows gw write output line by line and judges the result."""

    geometry: Geometry | None
    progress: ProgressCallback | None
    total: int | None = None
    seen: dict[tuple[int, int], int] = field(default_factory=dict)
    retries: dict[tuple[int, int], int] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.geometry is not None and self.geometry.track_count:
            self.total = self.geometry.track_count

    @property
    def total_retries(self) -> int:
        return sum(self.retries.values())

    def feed(self, line: str) -> None:
        text = line.strip()
        trackset = _TRACKSET.match(text)
        if trackset:
            cylinders = _count(trackset.group(1))
            heads = _count(trackset.group(2))
            if cylinders and heads:
                self.total = cylinders * heads
            return
        track = _TRACK.match(text)
        if track:
            self._track(int(track.group(1)), int(track.group(2)), track.group(3))
        elif text.startswith("All tracks verified") or _PARTIAL.search(text):
            self._report(1.0, 0, 0, len(self.seen), 0, "Finished writing.")

    def _track(self, cylinder: int, head: int, action: str) -> None:
        key = (cylinder, head)
        number = self.seen.setdefault(key, len(self.seen) + 1)
        retry_match = _RETRY.search(action)
        retry = int(retry_match.group(1)) if retry_match else 0
        if retry:
            self.retries[key] = max(retry, self.retries.get(key, 0))
            message = f"Retrying cylinder {cylinder}, head {head} (retry {retry})."
        elif action.startswith("Erasing"):
            message = f"Erasing cylinder {cylinder}, head {head}."
        else:
            message = f"Writing cylinder {cylinder}, head {head}."
        total = max(self.total or 0, number)
        self._report((number - 1) / total, cylinder, head, number, retry, message)

    def _report(
        self, fraction: float, cylinder: int, head: int, number: int, retry: int, message: str
    ) -> None:
        if self.progress is None:
            return
        total = max(self.total or 0, number)
        self.progress(
            WriteProgress(
                fraction=min(1.0, max(0.0, fraction)),
                cylinder=cylinder,
                head=head,
                track_number=number,
                track_count=total,
                retry=retry,
                message=message,
            )
        )

    def outcome(self, result: ProcessResult, *, timeout: float, retries_allowed: int) -> _Judgement:
        output = result.output
        lines = result.lines
        if result.cancelled:
            return _Judgement(
                WriteStatus.CANCELLED, "Writing was cancelled. The disk is incomplete."
            )
        if result.timed_out:
            return _Judgement(
                WriteStatus.FAILED,
                f"Writing did not finish within {_duration(timeout)} and was stopped. "
                "The disk is incomplete.",
            )
        command_failed = next((m.group(1) for m in map(_COMMAND_FAILED.search, lines) if m), "")
        errors = f"{command_failed} {_fatal_detail(lines)}".casefold()
        if "write protected" in errors:
            return _Judgement(
                WriteStatus.WRITE_PROTECTED,
                "The disk is write-protected. Close the write-protect hole and try again.",
            )
        if "no index" in errors or "track 0 not found" in errors:
            return _Judgement(
                WriteStatus.NO_DISK, "No disk was found in the drive. Insert a disk and try again."
            )
        if command_failed:
            return _Judgement(
                WriteStatus.FAILED, f"Greaseweazle reported a hardware error: {command_failed}."
            )
        failed = tuple(f"{m.group(1)}.{m.group(2)}" for m in _FAILED_VERIFY.finditer(output))
        if failed:
            attempts = retries_allowed + 1
            tracks = ", ".join(failed)
            return _Judgement(
                WriteStatus.FAILED,
                f"Track {tracks} did not verify after {attempts} attempts. "
                "The disk may be worn or damaged; try another disk.",
                failed,
            )
        if "** FATAL ERROR" in output or result.return_code != 0:
            return _Judgement(WriteStatus.FAILED, _fatal_summary(lines, result.return_code))
        if "All tracks verified" in output:
            retried = self.total_retries
            suffix = f" after {retried} {'retry' if retried == 1 else 'retries'}" if retried else ""
            return _Judgement(WriteStatus.VERIFIED, f"Written and verified{suffix}.")
        partial = _PARTIAL.search(output)
        if partial:
            verified, unverified, reason = partial.groups()
            if reason == "disabled":
                summary = "Written without verification, because verification was switched off."
            elif verified:
                summary = (
                    f"Written. {verified} tracks were verified and {unverified} could not be, "
                    "because Greaseweazle cannot verify them."
                )
            else:
                summary = (
                    "Written without verification, because Greaseweazle cannot verify this "
                    "kind of image."
                )
            return _Judgement(WriteStatus.WRITTEN, summary)
        return _Judgement(
            WriteStatus.FAILED,
            "Greaseweazle finished without confirming the write. Treat the disk as unverified.",
        )


def _fatal_detail(lines: list[str]) -> str:
    """The first line of the message gw prints after ``** FATAL ERROR:``."""
    for index, line in enumerate(lines):
        if line.startswith("** FATAL ERROR"):
            rest = [item.strip() for item in lines[index + 1 :] if item.strip()]
            return rest[0] if rest else ""
    return ""


def _fatal_summary(lines: list[str], return_code: int) -> str:
    detail = _fatal_detail(lines)
    if not detail:
        detail = next(
            (line.partition(":")[2].strip() for line in lines if line.startswith("ERROR:")), ""
        ) or _last_line(lines)
    if "Cannot find the Greaseweazle device" in detail:
        return "No Greaseweazle was found. Check that it is plugged in."
    if "Track0 signal" in detail:
        return "The drive did not report track 0. Check the drive cable and power, then try again."
    if detail:
        return f"Greaseweazle stopped with an error: {detail.rstrip('.')}."
    return f"Greaseweazle stopped with exit status {return_code}."


def _valid_device(device: str) -> bool:
    return not any(character in device for character in "\0\n\r")


def _duration(seconds: float) -> str:
    if seconds >= 120:
        return f"{round(seconds / 60)} minutes"
    whole = max(1, round(seconds))
    return f"{whole} second" + ("" if whole == 1 else "s")


def _last_line(lines: list[str]) -> str:
    return next((line.strip() for line in reversed(lines) if line.strip()), "no output")


def _count(ranges: str) -> int:
    """How many values a gw range list such as "0-79" or "0,2,4-9" names."""
    total = 0
    for part in ranges.split(","):
        low, _, high = part.partition("-")
        if not low.isdigit() or (high and not high.isdigit()):
            return 0
        total += (int(high) - int(low) + 1) if high else 1
    return total

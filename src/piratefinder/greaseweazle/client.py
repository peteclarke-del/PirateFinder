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

Whether a Greaseweazle is plugged in is judged from files alone
(``device_present``): no gw run and no network request, so the window can
follow the device every few seconds. ``gw info`` is run only to identify a
device, because each run that finds one also asks the GitHub API for the
newest firmware (``tools/info.py``, ``latest_firmware``). gw 1.23 has no
option to skip that request; ``--bootloader`` avoids it only by switching the
device into its bootloader, which is not wanted here. While online use is
switched off, ``probe`` gives gw an HTTPS proxy on this computer that refuses
every connection (``refusing_proxy``), so the lookup fails at once and no
request leaves the computer; gw then ends with a fatal error after printing
the device, which ``parse_info`` reads as connected.

Every gw run gets its environment from ``gw_environment``, which adds the
folder of the SPS Decoder Library PirateFinder installed to
``LD_LIBRARY_PATH`` (see ``caps``).
"""

from __future__ import annotations

import contextlib
import os
import re
import shutil
import socket
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path

from ..models import DeviceStatus, Geometry, PreparedImage, WriteOutcome, WriteProgress, WriteStatus
from . import caps
from .runner import OperationController, ProcessResult, minimal_environment, run_streaming

#: Where the Debian package keeps its private copy of the host tools.
PACKAGED_GW = Path("/usr/lib/piratefinder/bin/gw")
DRIVES = ("A", "B", "0", "1", "2", "3")
DIAGNOSTIC_LINES = 60
NOT_CONNECTED = "No Greaseweazle is connected."
#: The Greaseweazle's own USB id, assigned by pid.codes: vendor 1209, product 4d69.
USB_ID = ("1209", "4d69")
#: Words in a USB product string, or a by-id serial name, that gw also accepts
#: (``tools/util.py``, ``score_port``): its own name and "Greaseweazle compatible".
DEVICE_WORDS = ("greaseweazle", "gw-compat")
SYSFS_USB_DEVICES = Path("/sys/bus/usb/devices")
DEVICE_FOLDER = Path("/dev")

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


def gw_environment() -> dict[str, str]:
    """The environment every gw run gets: the runner's minimal one, and the library path.

    gw loads the SPS Decoder Library by its soname, so the dynamic loader of
    the gw process must find it, and the loader reads ``LD_LIBRARY_PATH`` only
    when the process starts. The folder PirateFinder installed the library in
    comes first; the ``LD_LIBRARY_PATH`` PirateFinder was started with
    follows, so a library found through it is found by gw as well.
    """
    environment = minimal_environment()
    search = caps.library_path(os.environ.get("LD_LIBRARY_PATH", ""))
    if search:
        environment["LD_LIBRARY_PATH"] = search
    return environment


def device_present(
    device: str = "", *, sysfs: Path = SYSFS_USB_DEVICES, dev: Path = DEVICE_FOLDER
) -> bool:
    """Whether a Greaseweazle looks plugged in, read from files: no gw, no network.

    ``device`` is the serial port chosen in Preferences; when it is set, the
    answer is whether that port exists. Otherwise: a USB device in sysfs with
    the Greaseweazle's id or product name, the ``greaseweazle`` link the udev
    rule makes, or a serial port whose ``by-id`` name is a Greaseweazle's.
    """
    if device:
        path = Path(device)
        return (path if path.is_absolute() else dev / path).exists()
    try:
        entries = sorted(sysfs.iterdir())
    except OSError:
        entries = []
    for entry in entries:
        if (_read_attribute(entry / "idVendor"), _read_attribute(entry / "idProduct")) == USB_ID:
            return True
        if _names_device(_read_attribute(entry / "product")):
            return True
    if (dev / "greaseweazle").exists():
        return True
    try:
        return any(_names_device(path.name) for path in (dev / "serial" / "by-id").iterdir())
    except OSError:
        return False


def _names_device(text: str) -> bool:
    folded = text.casefold()
    return any(word in folded for word in DEVICE_WORDS)


def _read_attribute(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace").strip().lower()
    except OSError:
        return ""


@contextlib.contextmanager
def refusing_proxy() -> Iterator[str]:
    """A proxy address on this computer that refuses every connection, while the block runs.

    The port is bound and never listened on, so a connection to it is refused
    at once, and no other program can take the port while it is held.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as reserved:
        reserved.bind(("127.0.0.1", 0))
        yield f"http://127.0.0.1:{reserved.getsockname()[1]}"


def probe(
    timeout: float = 20,
    *,
    device: str = "",
    executable: str | None = None,
    online: bool = True,
) -> DeviceStatus:
    """Ask ``gw info`` whether a Greaseweazle is connected, and which one.

    ``device`` is the serial port chosen in Preferences; "" lets gw search.
    ``online`` False keeps gw's firmware lookup from reaching the network,
    and so from reporting newer firmware.
    """
    command = executable or find_gw()
    if command is None:
        return DeviceStatus(False, "The Greaseweazle host tools (gw) are not installed.")
    if not _valid_device(device):
        return DeviceStatus(False, "The Greaseweazle device name is not valid.")
    arguments = [command, "info"] + ([f"--device={device}"] if device else [])
    environment = gw_environment()
    with contextlib.ExitStack() as stack:
        if not online:
            proxy = stack.enter_context(refusing_proxy())
            for name in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy"):
                environment[name] = proxy
        try:
            result = run_streaming(arguments, timeout=timeout, environment=environment)
        except OSError as error:
            return DeviceStatus(
                False, f"The Greaseweazle host tools could not be started: {error}."
            )
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
            message = NOT_CONNECTED
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
            arguments,
            timeout=timeout,
            on_line=reader.feed,
            controller=controller,
            environment=gw_environment(),
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

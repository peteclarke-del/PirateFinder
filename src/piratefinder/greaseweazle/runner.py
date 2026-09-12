"""Run a command line by line, with a timeout and cancellation.

Ported from Greaseweazle-GUI's ``subprocess_runner.py`` and ``operation.py``,
with the process handling of the File Forge Greaseweazle clients: no shell,
stdin closed, stderr merged into stdout, and a minimal environment so the
user's Python settings cannot change how the host tools behave.

Cancelling sends SIGINT, which gw handles by resetting the drive and
exiting; a process that ignores it is killed after a grace period. A
timeout is handled the same way.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass

#: How long an interrupted process has to exit before it is killed.
GRACE_SECONDS = 10.0


def minimal_environment() -> dict[str, str]:
    """HOME, locale and PATH from the caller, and unbuffered Python output."""
    return {
        "HOME": os.environ.get("HOME", "/tmp"),
        "LANG": os.environ.get("LANG", "C.UTF-8"),
        "LC_ALL": os.environ.get("LC_ALL", "C.UTF-8"),
        "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
        "PYTHONUNBUFFERED": "1",
    }


class OperationController:
    """Thread-safe cancellation of the process currently registered with it."""

    def __init__(self) -> None:
        self._cancelled = threading.Event()
        self._lock = threading.Lock()
        self._process: subprocess.Popen[str] | None = None

    @property
    def cancelled(self) -> bool:
        return self._cancelled.is_set()

    def register(self, process: subprocess.Popen[str]) -> None:
        with self._lock:
            self._process = process
            cancelled = self._cancelled.is_set()
        if cancelled:
            _interrupt(process)

    def unregister(self, process: subprocess.Popen[str]) -> None:
        with self._lock:
            if self._process is process:
                self._process = None

    def cancel(self) -> None:
        self._cancelled.set()
        with self._lock:
            process = self._process
        if process is not None:
            _interrupt(process)


@dataclass(frozen=True, slots=True)
class ProcessResult:
    return_code: int
    output: str
    timed_out: bool
    cancelled: bool

    @property
    def lines(self) -> list[str]:
        return self.output.splitlines()


# Restores the default SIGINT action, then becomes the real command.
_RESET_INTERRUPT = (
    "import os, signal, sys; "
    "signal.signal(signal.SIGINT, signal.SIG_DFL); "
    "os.execvp(sys.argv[1], sys.argv[1:])"
)


def with_default_interrupt(command: Sequence[str]) -> list[str]:
    """The command line to start, making sure ``gw`` can be interrupted.

    Cancelling sends SIGINT. A process started in the background by a
    non-interactive shell (``nohup ... &``, ``xvfb-run``) has SIGINT ignored,
    every child inherits that, and gw would then ignore the cancel until the
    grace period ran out. A shell cannot undo it, because a signal ignored on
    entry to a non-interactive shell cannot be reset there, and ``preexec_fn``
    is unsafe in a program with threads, so a small Python step resets the
    signal and then execs gw.
    """
    if signal.getsignal(signal.SIGINT) == signal.SIG_IGN:
        return [sys.executable, "-c", _RESET_INTERRUPT, *command]
    return list(command)


def run_streaming(
    command: Sequence[str],
    *,
    timeout: float,
    on_line: Callable[[str], None] | None = None,
    controller: OperationController | None = None,
    environment: dict[str, str] | None = None,
    process_factory: Callable[..., subprocess.Popen[str]] = subprocess.Popen,
    grace: float = GRACE_SECONDS,
) -> ProcessResult:
    """Run ``command`` and pass each output line to ``on_line`` as it arrives.

    Raises OSError when the command cannot be started.
    """
    process = process_factory(
        with_default_interrupt(command),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
        env=environment if environment is not None else minimal_environment(),
    )
    if controller is not None:
        controller.register(process)
    timed_out = threading.Event()
    stopped = threading.Event()

    def stop_process() -> None:
        if process.poll() is None:
            timed_out.set()
            _interrupt(process)
            if not stopped.wait(grace):
                _kill(process)

    def watch_cancel() -> None:
        # The controller has already sent SIGINT; kill if it is ignored.
        while not stopped.wait(0.2):
            if controller is not None and controller.cancelled:
                if not stopped.wait(grace):
                    _kill(process)
                return

    timer = threading.Timer(timeout, stop_process)
    timer.daemon = True
    timer.start()
    if controller is not None:
        threading.Thread(target=watch_cancel, name="gw-cancel", daemon=True).start()
    lines: list[str] = []
    try:
        if process.stdout is not None:
            for raw_line in process.stdout:
                line = raw_line.rstrip("\r\n")
                lines.append(line)
                if on_line is not None:
                    on_line(line)
        return_code = process.wait()
    except BaseException:
        _kill(process)
        process.wait()
        raise
    finally:
        stopped.set()
        timer.cancel()
        if controller is not None:
            controller.unregister(process)
        if process.stdout is not None:
            process.stdout.close()
    return ProcessResult(
        return_code,
        "\n".join(lines).strip(),
        timed_out.is_set(),
        controller.cancelled if controller is not None else False,
    )


def _interrupt(process: subprocess.Popen[str]) -> None:
    try:
        if process.poll() is None:
            process.send_signal(signal.SIGINT)
    except OSError:
        # The process exited between poll() and signal delivery.
        pass


def _kill(process: subprocess.Popen[str]) -> None:
    try:
        if process.poll() is None:
            process.kill()
    except OSError:
        pass

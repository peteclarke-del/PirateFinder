"""A thread-safe cancellation flag shared by scans, downloads and sessions.

Anything with a ``cancelled`` attribute works where a cancellation is
accepted, so ``greaseweazle.runner.OperationController`` can be passed too.
"""

from __future__ import annotations

import threading
import time


class Cancellation:
    """A cancellation request that a worker thread polls."""

    def __init__(self) -> None:
        self._event = threading.Event()

    @property
    def cancelled(self) -> bool:
        """True once ``cancel`` has been called."""
        return self._event.is_set()

    def cancel(self) -> None:
        """Ask the worker to stop at the next safe point."""
        self._event.set()

    def wait(self, seconds: float) -> bool:
        """Sleep up to ``seconds``, returning True early when cancelled."""
        return self._event.wait(max(seconds, 0.0))


def is_cancelled(cancel: object | None) -> bool:
    """Whether an optional cancellation object, or a threading.Event, has been triggered."""
    if cancel is None:
        return False
    if isinstance(cancel, threading.Event):
        return cancel.is_set()
    return bool(getattr(cancel, "cancelled", False))


def sleep_unless_cancelled(seconds: float, cancel: object | None) -> bool:
    """Sleep for ``seconds`` in short steps; return True when cancelled meanwhile."""
    if seconds <= 0:
        return is_cancelled(cancel)
    if isinstance(cancel, Cancellation):
        return cancel.wait(seconds)
    deadline = time.monotonic() + seconds
    while True:
        if is_cancelled(cancel):
            return True
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        time.sleep(min(remaining, 0.1))

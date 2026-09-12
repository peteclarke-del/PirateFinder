"""A small in-memory log shown by Diagnostic Log.

It keeps the most recent lines of Greaseweazle output, backend log records
and interface events, so a user reporting a problem can copy what happened
without finding a log file. Nothing is written to disk.
"""

from __future__ import annotations

import logging
import threading
from collections import deque
from datetime import datetime

MAX_LINES = 4000


class DiagnosticLog:
    """Thread-safe ring buffer of timestamped lines."""

    def __init__(self, max_lines: int = MAX_LINES) -> None:
        self._lines: deque[str] = deque(maxlen=max_lines)
        self._lock = threading.Lock()
        self._listeners: list = []

    def add(self, source: str, text: str) -> None:
        """Record ``text`` (one or more lines) under a short source name."""
        stamp = datetime.now().strftime("%H:%M:%S")
        lines = [line.rstrip() for line in str(text).splitlines() if line.strip()]
        if not lines:
            return
        with self._lock:
            for line in lines:
                self._lines.append(f"{stamp} {source}: {line}")
            listeners = list(self._listeners)
        for listener in listeners:
            listener()

    def text(self) -> str:
        with self._lock:
            return "\n".join(self._lines)

    def __len__(self) -> int:
        with self._lock:
            return len(self._lines)

    def clear(self) -> None:
        with self._lock:
            self._lines.clear()
            listeners = list(self._listeners)
        for listener in listeners:
            listener()

    def subscribe(self, listener) -> None:
        """Call ``listener()`` (on any thread) whenever the log changes."""
        with self._lock:
            self._listeners.append(listener)

    def unsubscribe(self, listener) -> None:
        with self._lock:
            if listener in self._listeners:
                self._listeners.remove(listener)


class _LogHandler(logging.Handler):
    def __init__(self, log: DiagnosticLog) -> None:
        super().__init__(level=logging.INFO)
        self._log = log

    def emit(self, record: logging.LogRecord) -> None:
        try:
            message = record.getMessage()
            if record.exc_info and record.exc_info[1] is not None:
                message += f" ({type(record.exc_info[1]).__name__}: {record.exc_info[1]})"
            self._log.add(record.name.removeprefix("piratefinder."), message)
        except Exception:  # noqa: BLE001 - logging must never raise
            self.handleError(record)


LOG = DiagnosticLog()
_handler: _LogHandler | None = None


def capture_backend_logging() -> None:
    """Copy records from the ``piratefinder`` logger into the diagnostic log."""
    global _handler
    if _handler is not None:
        return
    _handler = _LogHandler(LOG)
    logger = logging.getLogger("piratefinder")
    logger.addHandler(_handler)
    if logger.level == logging.NOTSET or logger.level > logging.INFO:
        logger.setLevel(logging.INFO)

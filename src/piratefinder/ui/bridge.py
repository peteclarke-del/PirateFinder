"""Passing work between the GTK main loop and worker threads.

Widgets are only touched on the main thread. Anything that may block (a
search in a large catalogue, a scan, a probe, a download, a write) runs on a
daemon thread and hands its result back with ``GLib.idle_add``.

``SessionEventsBridge`` implements the ``SessionEvents`` protocol of
``jobs.session`` for a write session running on a worker thread. Each event
is forwarded to the main loop; ``ask_insert`` blocks the worker on a
``threading.Event`` until the interface answers the insert-disk prompt.
"""

from __future__ import annotations

import threading
import traceback
from collections.abc import Callable
from typing import Any

import gi

gi.require_version("GLib", "2.0")
from gi.repository import GLib  # noqa: E402

from .log import LOG  # noqa: E402

ANSWERS = ("write", "skip", "stop")


def on_main(callback: Callable[..., Any], *args: Any) -> None:
    """Run ``callback(*args)`` once on the main loop."""

    def run() -> bool:
        callback(*args)
        return GLib.SOURCE_REMOVE

    GLib.idle_add(run)


def run_in_thread(
    work: Callable[[], Any],
    on_done: Callable[[Any], None] | None = None,
    on_error: Callable[[BaseException], None] | None = None,
    *,
    name: str = "piratefinder-worker",
) -> threading.Thread:
    """Run ``work()`` on a daemon thread and deliver the outcome on the main loop.

    ``on_done(result)`` or ``on_error(exception)`` is called on the main loop.
    An exception with no ``on_error`` is written to the diagnostic log.
    """

    def worker() -> None:
        try:
            result = work()
        except Exception as error:  # noqa: BLE001 - reported to the interface
            LOG.add("error", "".join(traceback.format_exception(error)).strip())
            if on_error is not None:
                on_main(on_error, error)
            return
        if on_done is not None:
            on_main(on_done, result)

    thread = threading.Thread(target=worker, name=name, daemon=True)
    thread.start()
    return thread


class Latest:
    """Delivers only the most recent value posted from a worker thread.

    A scan reports every file it looks at; forwarding each report would flood
    the main loop. ``post`` keeps the newest value and schedules at most one
    delivery at a time.
    """

    def __init__(self, deliver: Callable[..., None]) -> None:
        self._deliver = deliver
        self._lock = threading.Lock()
        self._value: tuple | None = None
        self._scheduled = False

    def post(self, *value: Any) -> None:
        with self._lock:
            self._value = value
            if self._scheduled:
                return
            self._scheduled = True
        GLib.timeout_add(50, self._flush)

    def _flush(self) -> bool:
        with self._lock:
            value = self._value
            self._value = None
            self._scheduled = False
        if value is not None:
            self._deliver(*value)
        return GLib.SOURCE_REMOVE


class PendingAnswer:
    """A question asked by a worker thread and answered on the main loop."""

    def __init__(self) -> None:
        self._event = threading.Event()
        self._value = "stop"

    @property
    def answered(self) -> bool:
        return self._event.is_set()

    def answer(self, value: str) -> None:
        """Give the answer. Only the first answer counts."""
        if self._event.is_set():
            return
        self._value = value if value in ANSWERS else "stop"
        self._event.set()

    def wait(self, timeout: float | None = None) -> str:
        """Block until answered; an unanswered question counts as "stop"."""
        self._event.wait(timeout)
        return self._value


class SessionEventsBridge:
    """Forwards ``SessionEvents`` from the worker to a main-loop target.

    The target provides ``session_stage``, ``session_download``,
    ``session_progress``, ``session_ask_insert`` (which receives a
    ``PendingAnswer`` to answer) and ``session_item_finished``, all called on
    the main loop.
    """

    def __init__(self, target: Any) -> None:
        self._target = target
        self._lock = threading.Lock()
        self._pending: PendingAnswer | None = None
        self._stopped = False

    # SessionEvents protocol, called on the worker thread.

    def on_stage(self, item, index: int, total: int, stage: str, message: str) -> None:
        if message:
            LOG.add("session", f"{item.label}: {message}")
        on_main(self._target.session_stage, item, index, total, stage, message)

    def on_download(self, item, done: int, total: int | None) -> None:
        on_main(self._target.session_download, item, done, total)

    def on_write_progress(self, item, progress) -> None:
        if progress.message:
            LOG.add("gw", progress.message)
        on_main(self._target.session_progress, item, progress)

    def ask_insert(self, item, index: int, total: int, reason: str = "") -> str:
        pending = PendingAnswer()
        with self._lock:
            if self._stopped:
                return "stop"
            self._pending = pending
        on_main(self._target.session_ask_insert, item, index, total, reason, pending)
        answer = pending.wait()
        with self._lock:
            if self._pending is pending:
                self._pending = None
        LOG.add("session", f"{item.label}: insert prompt answered {answer!r}")
        return answer

    def on_item_finished(self, item, outcome) -> None:
        LOG.add("session", f"{item.label}: {outcome.status.value}: {outcome.summary}")
        if outcome.diagnostic:
            LOG.add("gw", outcome.diagnostic)
        on_main(self._target.session_item_finished, item, outcome)

    # Called on the main loop.

    def stop(self) -> None:
        """Release a worker waiting on a prompt and refuse any further prompts."""
        with self._lock:
            self._stopped = True
            pending = self._pending
            self._pending = None
        if pending is not None:
            pending.answer("stop")

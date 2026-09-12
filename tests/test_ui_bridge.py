"""The bridge between worker threads and the GTK main loop, without a display."""

from __future__ import annotations

import threading
import time
import unittest

import gi

gi.require_version("GLib", "2.0")
from gi.repository import GLib  # noqa: E402

from piratefinder.models import (  # noqa: E402
    Platform,
    QueueItem,
    WriteOutcome,
    WriteProgress,
    WriteStatus,
)
from piratefinder.ui.bridge import (  # noqa: E402
    Latest,
    PendingAnswer,
    SessionEventsBridge,
    run_in_thread,
)


def pump_until(predicate, timeout: float = 5.0) -> bool:
    context = GLib.MainContext.default()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        while context.pending():
            context.iteration(False)
        if predicate():
            return True
        time.sleep(0.005)
    return predicate()


class Target:
    """Records what the bridge delivers, and on which thread."""

    def __init__(self, answer: str | None = "write") -> None:
        self.answer = answer
        self.events: list[tuple] = []
        self.threads: set[str] = set()
        self.pending: list[PendingAnswer] = []

    def _note(self, *event) -> None:
        self.events.append(event)
        self.threads.add(threading.current_thread().name)

    def session_stage(self, item, index, total, stage, message) -> None:
        self._note("stage", index, total, stage)

    def session_download(self, item, done, total) -> None:
        self._note("download", done, total)

    def session_progress(self, item, progress) -> None:
        self._note("progress", progress.track_number)

    def session_ask_insert(self, item, index, total, reason, pending) -> None:
        self._note("ask", index, reason)
        self.pending.append(pending)
        if self.answer is not None:
            pending.answer(self.answer)

    def session_item_finished(self, item, outcome) -> None:
        self._note("finished", outcome.status)


ITEM = QueueItem("id", "Automation 250", Platform.ATARI_ST, disk_id=1)


class BridgeTests(unittest.TestCase):
    def test_events_reach_the_main_loop_in_order(self) -> None:
        target = Target()
        bridge = SessionEventsBridge(target)
        answers: list[str] = []

        def worker() -> None:
            bridge.on_stage(ITEM, 0, 2, "resolve", "Looking")
            bridge.on_download(ITEM, 10, 100)
            bridge.on_write_progress(ITEM, WriteProgress(0.5, 40, 0, 80, 160))
            answers.append(bridge.ask_insert(ITEM, 0, 2, "no-disk"))
            bridge.on_item_finished(ITEM, WriteOutcome(WriteStatus.VERIFIED, "ok"))

        thread = threading.Thread(target=worker, name="worker")
        thread.start()
        self.assertTrue(pump_until(lambda: len(target.events) == 5))
        thread.join(2)
        self.assertEqual(answers, ["write"])
        self.assertEqual(
            [event[0] for event in target.events],
            ["stage", "download", "progress", "ask", "finished"],
        )
        self.assertEqual(target.threads, {threading.main_thread().name})

    def test_ask_insert_blocks_until_answered(self) -> None:
        target = Target(answer=None)
        bridge = SessionEventsBridge(target)
        answers: list[str] = []
        thread = threading.Thread(target=lambda: answers.append(bridge.ask_insert(ITEM, 1, 3)))
        thread.start()
        self.assertTrue(pump_until(lambda: bool(target.pending)))
        time.sleep(0.05)
        self.assertEqual(answers, [], "the worker must wait for the answer")
        target.pending[0].answer("skip")
        thread.join(2)
        self.assertEqual(answers, ["skip"])

    def test_stop_releases_a_waiting_worker_and_refuses_new_prompts(self) -> None:
        target = Target(answer=None)
        bridge = SessionEventsBridge(target)
        answers: list[str] = []
        thread = threading.Thread(target=lambda: answers.append(bridge.ask_insert(ITEM, 0, 1)))
        thread.start()
        self.assertTrue(pump_until(lambda: bool(target.pending)))
        bridge.stop()
        thread.join(2)
        self.assertEqual(answers, ["stop"])
        self.assertEqual(bridge.ask_insert(ITEM, 0, 1), "stop")

    def test_only_the_first_answer_counts_and_nonsense_means_stop(self) -> None:
        pending = PendingAnswer()
        pending.answer("write")
        pending.answer("stop")
        self.assertEqual(pending.wait(0), "write")
        odd = PendingAnswer()
        odd.answer("format everything")
        self.assertEqual(odd.wait(0), "stop")

    def test_run_in_thread_delivers_results_and_errors(self) -> None:
        results: list = []
        run_in_thread(lambda: 6 * 7, results.append)
        run_in_thread(lambda: 1 / 0, None, lambda error: results.append(type(error)))
        self.assertTrue(pump_until(lambda: len(results) == 2))
        self.assertIn(42, results)
        self.assertIn(ZeroDivisionError, results)

    def test_latest_coalesces_a_flood_of_progress(self) -> None:
        seen: list[int] = []
        latest = Latest(seen.append)

        def flood() -> None:
            for number in range(5000):
                latest.post(number)

        thread = threading.Thread(target=flood)
        thread.start()
        thread.join(5)
        self.assertTrue(pump_until(lambda: bool(seen) and seen[-1] == 4999))
        self.assertLess(len(seen), 100)


if __name__ == "__main__":
    unittest.main()

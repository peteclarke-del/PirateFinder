from __future__ import annotations

import threading
import time
import unittest

from piratefinder.greaseweazle.runner import OperationController
from piratefinder.jobs.cancellation import Cancellation, is_cancelled, sleep_unless_cancelled


class CancellationTests(unittest.TestCase):
    def test_flag(self) -> None:
        cancel = Cancellation()
        self.assertFalse(cancel.cancelled)
        cancel.cancel()
        self.assertTrue(cancel.cancelled)

    def test_accepted_objects(self) -> None:
        self.assertFalse(is_cancelled(None))
        event = threading.Event()
        self.assertFalse(is_cancelled(event))
        event.set()
        self.assertTrue(is_cancelled(event))
        controller = OperationController()
        self.assertFalse(is_cancelled(controller))
        controller.cancel()
        self.assertTrue(is_cancelled(controller))

    def test_sleep_ends_early_when_cancelled(self) -> None:
        for cancel in (Cancellation(), OperationController()):
            timer = threading.Timer(0.1, cancel.cancel)
            timer.start()
            started = time.monotonic()
            self.assertTrue(sleep_unless_cancelled(5.0, cancel))
            self.assertLess(time.monotonic() - started, 2.0)
            timer.join()
        self.assertFalse(sleep_unless_cancelled(0.01, None))


if __name__ == "__main__":
    unittest.main()

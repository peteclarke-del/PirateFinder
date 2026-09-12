"""The streaming subprocess runner: output, environment, cancellation and timeout.

The commands are small Python scripts written at run time. ``fake_gw`` is
also used by the client tests to stand in for the gw host tool.
"""

from __future__ import annotations

import json
import os
import signal
import stat
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from piratefinder.greaseweazle.runner import OperationController, run_streaming


def fake_gw(
    folder: Path,
    lines: list[str],
    *,
    exit_code: int = 0,
    delay: float = 0.0,
    hang: float = 0.0,
    ignore_sigint: bool = False,
) -> str:
    """Write an executable that prints ``lines`` to stderr as gw does, then exits.

    It records its arguments in ``argv.json`` and its environment in
    ``env.json`` beside itself. On SIGINT it prints ``Interrupted`` and
    exits 1, like gw after it resets the drive.
    """
    script = folder / "gw"
    body = f"""#!{sys.executable}
import json, os, signal, sys, time
folder = {str(folder)!r}
with open(os.path.join(folder, "argv.json"), "w") as stream:
    json.dump(sys.argv[1:], stream)
with open(os.path.join(folder, "env.json"), "w") as stream:
    json.dump(dict(os.environ), stream)
if {ignore_sigint!r}:
    signal.signal(signal.SIGINT, signal.SIG_IGN)
try:
    for line in {lines!r}:
        print(line, file=sys.stderr, flush=True)
        time.sleep({delay!r})
    time.sleep({hang!r})
except KeyboardInterrupt:
    print("Interrupted", file=sys.stderr, flush=True)
    sys.exit(1)
sys.exit({exit_code!r})
"""
    script.write_text(body)
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    return str(script)


class RunnerTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._folder = tempfile.TemporaryDirectory()
        self.folder = Path(self._folder.name)

    def tearDown(self) -> None:
        self._folder.cleanup()

    def recorded(self, name: str):
        return json.loads((self.folder / name).read_text())


class StreamingTests(RunnerTestCase):
    def test_lines_arrive_in_order_and_stderr_is_captured(self) -> None:
        command = fake_gw(self.folder, ["one", "two", "three"], exit_code=3)
        seen: list[str] = []
        result = run_streaming([command], timeout=30, on_line=seen.append)
        self.assertEqual(seen, ["one", "two", "three"])
        self.assertEqual(result.output, "one\ntwo\nthree")
        self.assertEqual(result.return_code, 3)
        self.assertFalse(result.timed_out)
        self.assertFalse(result.cancelled)

    def test_arguments_are_passed_without_a_shell(self) -> None:
        command = fake_gw(self.folder, [])
        tricky = "$(touch pwned); echo `id` > x | y"
        run_streaming([command, tricky, "two words"], timeout=30)
        self.assertEqual(self.recorded("argv.json"), [tricky, "two words"])
        self.assertFalse((self.folder / "pwned").exists())

    def test_minimal_environment(self) -> None:
        command = fake_gw(self.folder, [])
        with mock.patch.dict(os.environ, {"PIRATEFINDER_TEST_SECRET": "x", "PYTHONPATH": "/x"}):
            run_streaming([command], timeout=30)
        environment = self.recorded("env.json")
        self.assertNotIn("PIRATEFINDER_TEST_SECRET", environment)
        self.assertNotIn("PYTHONPATH", environment)
        self.assertEqual(environment["PYTHONUNBUFFERED"], "1")
        self.assertLessEqual(
            set(environment), {"HOME", "LANG", "LC_ALL", "PATH", "PYTHONUNBUFFERED", "LC_CTYPE"}
        )

    def test_a_missing_command_raises_oserror(self) -> None:
        with self.assertRaises(OSError):
            run_streaming([str(self.folder / "missing")], timeout=5)

    def test_an_exception_in_the_callback_stops_the_process(self) -> None:
        command = fake_gw(self.folder, ["one"], hang=30)

        def explode(_line: str) -> None:
            raise RuntimeError("stop")

        started = time.monotonic()
        with self.assertRaises(RuntimeError):
            run_streaming([command], timeout=60, on_line=explode)
        self.assertLess(time.monotonic() - started, 10)


class CancelTests(RunnerTestCase):
    def test_cancel_interrupts_the_process(self) -> None:
        command = fake_gw(self.folder, ["writing"], hang=30)
        controller = OperationController()

        def on_line(line: str) -> None:
            if line == "writing":
                threading.Thread(target=controller.cancel).start()

        started = time.monotonic()
        result = run_streaming([command], timeout=60, on_line=on_line, controller=controller)
        self.assertTrue(result.cancelled)
        self.assertIn("Interrupted", result.output)
        self.assertEqual(result.return_code, 1)
        self.assertLess(time.monotonic() - started, 10)

    def test_cancel_interrupts_even_when_the_parent_ignores_interrupts(self) -> None:
        # Started with "nohup ... &" or under xvfb-run, SIGINT arrives ignored
        # and every child inherits that.
        previous = signal.signal(signal.SIGINT, signal.SIG_IGN)
        self.addCleanup(signal.signal, signal.SIGINT, previous)
        command = fake_gw(self.folder, ["writing"], hang=30)
        controller = OperationController()

        def on_line(line: str) -> None:
            if line == "writing":
                threading.Thread(target=controller.cancel).start()

        started = time.monotonic()
        result = run_streaming(
            [command], timeout=60, on_line=on_line, controller=controller, grace=20
        )
        self.assertTrue(result.cancelled)
        self.assertIn("Interrupted", result.output, "gw ignored the cancel")
        self.assertLess(time.monotonic() - started, 10)

    def test_cancel_before_start_interrupts_at_once(self) -> None:
        command = fake_gw(self.folder, [], hang=30)
        controller = OperationController()
        controller.cancel()
        started = time.monotonic()
        result = run_streaming([command], timeout=60, controller=controller)
        self.assertTrue(result.cancelled)
        self.assertLess(time.monotonic() - started, 10)

    def test_a_process_that_ignores_sigint_is_killed_after_the_grace_period(self) -> None:
        command = fake_gw(self.folder, ["writing"], hang=30, ignore_sigint=True)
        controller = OperationController()
        timer = threading.Timer(1.0, controller.cancel)
        timer.start()
        started = time.monotonic()
        result = run_streaming([command], timeout=60, controller=controller, grace=0.5)
        timer.join()
        self.assertTrue(result.cancelled)
        self.assertLess(result.return_code, 0)
        self.assertLess(time.monotonic() - started, 10)


class TimeoutTests(RunnerTestCase):
    def test_timeout_interrupts_and_is_reported(self) -> None:
        command = fake_gw(self.folder, ["writing"], hang=30)
        started = time.monotonic()
        result = run_streaming([command], timeout=0.5)
        self.assertTrue(result.timed_out)
        self.assertFalse(result.cancelled)
        self.assertIn("Interrupted", result.output)
        self.assertLess(time.monotonic() - started, 10)

    def test_timeout_kills_a_process_that_ignores_sigint(self) -> None:
        command = fake_gw(self.folder, [], hang=30, ignore_sigint=True)
        started = time.monotonic()
        result = run_streaming([command], timeout=0.5, grace=0.5)
        self.assertTrue(result.timed_out)
        self.assertLess(time.monotonic() - started, 10)


if __name__ == "__main__":
    unittest.main()

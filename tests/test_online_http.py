from __future__ import annotations

import email.utils
import http.server
import shutil
import tempfile
import threading
import time
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

from piratefinder import __version__
from piratefinder.jobs.cancellation import Cancellation
from piratefinder.online.http import (
    USER_AGENT,
    DownloadCancelled,
    Downloader,
    DownloadError,
    HostThrottle,
    _retry_after,
)
from tests.test_library_helpers import serve

PAYLOAD = bytes(range(256)) * 1024  # 256 KiB


class State:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.requests: list[tuple[str, dict[str, str], float]] = []
        self.counters: dict[str, int] = {}

    def hit(self, path: str) -> int:
        with self.lock:
            self.counters[path] = self.counters.get(path, 0) + 1
            return self.counters[path]


def make_handler(state: State) -> type[http.server.BaseHTTPRequestHandler]:
    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:  # noqa: A002
            return None

        def do_GET(self) -> None:  # noqa: N802
            with state.lock:
                state.requests.append((self.path, dict(self.headers), time.monotonic()))
            count = state.hit(self.path)
            if self.path == "/file":
                self.send_payload(honour_range=True)
            elif self.path == "/no-range":
                self.send_payload(honour_range=False)
            elif self.path == "/redirect":
                self.send_response(302)
                self.send_header("Location", "/file")
                self.end_headers()
            elif self.path == "/flaky":
                if count <= 2:
                    self.send_error_code(503, {"Retry-After": "0"} if count == 1 else {})
                else:
                    self.send_payload(honour_range=True)
            elif self.path == "/limited":
                if count == 1:
                    self.send_error_code(429, {"Retry-After": "3"})
                else:
                    self.send_payload(honour_range=True)
            elif self.path == "/wait-long":
                self.send_error_code(429, {"Retry-After": "3600"})
            elif self.path == "/always-503":
                self.send_error_code(503, {})
            elif self.path == "/truncated":
                if count == 1:
                    self.send_response(200)
                    self.send_header("Content-Length", str(len(PAYLOAD)))
                    self.end_headers()
                    self.wfile.write(PAYLOAD[:1000])
                    self.wfile.flush()
                    self.close_connection = True
                else:
                    self.send_payload(honour_range=True)
            elif self.path == "/json":
                body = b'{"ok": true}'
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            else:
                self.send_error_code(404, {})

        def send_error_code(self, code: int, headers: dict[str, str]) -> None:
            self.send_response(code)
            for key, value in headers.items():
                self.send_header(key, value)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def send_payload(self, honour_range: bool) -> None:
            start = 0
            requested = self.headers.get("Range", "")
            if honour_range and requested.startswith("bytes="):
                start = int(requested[6:].split("-")[0])
                if start >= len(PAYLOAD):
                    self.send_error_code(416, {})
                    return
                self.send_response(206)
                self.send_header(
                    "Content-Range", f"bytes {start}-{len(PAYLOAD) - 1}/{len(PAYLOAD)}"
                )
            else:
                self.send_response(200)
            body = PAYLOAD[start:]
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    return Handler


class DownloaderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.folder = Path(tempfile.mkdtemp(prefix="pf-http-"))
        self.addCleanup(shutil.rmtree, self.folder, True)
        self.state = State()
        context = serve(make_handler(self.state))
        self.base = context.__enter__()
        self.addCleanup(context.__exit__, None, None, None)
        self.sleeps: list[float] = []
        self.downloader = Downloader(
            throttle=HostThrottle(0.0), sleep=self.sleeps.append, timeout=10
        )

    def test_user_agent_names_the_application(self) -> None:
        self.assertEqual(
            USER_AGENT,
            f"PirateFinder/{__version__} (+https://github.com/peteclarke-del/PirateFinder)",
        )
        self.downloader.download(f"{self.base}/file", self.folder / "a.bin")
        self.assertEqual(self.state.requests[0][1]["User-Agent"], USER_AGENT)

    def test_download_with_progress(self) -> None:
        calls: list[tuple[int, int | None]] = []
        target = self.downloader.download(
            f"{self.base}/file", self.folder / "sub" / "a.bin", progress=lambda *c: calls.append(c)
        )
        self.assertEqual(target.read_bytes(), PAYLOAD)
        self.assertFalse(target.with_name("a.bin.part").exists())
        self.assertEqual(calls[0], (0, len(PAYLOAD)))
        self.assertEqual(calls[-1], (len(PAYLOAD), len(PAYLOAD)))

    def test_follows_redirects(self) -> None:
        target = self.downloader.download(f"{self.base}/redirect", self.folder / "r.bin")
        self.assertEqual(target.read_bytes(), PAYLOAD)
        self.assertEqual([path for path, _h, _t in self.state.requests], ["/redirect", "/file"])

    def test_resumes_a_partial_file(self) -> None:
        target = self.folder / "resume.bin"
        target.with_name("resume.bin.part").write_bytes(PAYLOAD[:5000])
        self.downloader.download(f"{self.base}/file", target)
        self.assertEqual(target.read_bytes(), PAYLOAD)
        self.assertEqual(self.state.requests[0][1]["Range"], "bytes=5000-")

    def test_restarts_when_the_server_ignores_the_range(self) -> None:
        target = self.folder / "restart.bin"
        target.with_name("restart.bin.part").write_bytes(b"stale data")
        self.downloader.download(f"{self.base}/no-range", target)
        self.assertEqual(target.read_bytes(), PAYLOAD)

    def test_restarts_when_the_partial_file_is_too_long(self) -> None:
        target = self.folder / "long.bin"
        target.with_name("long.bin.part").write_bytes(PAYLOAD + b"extra")
        self.downloader.download(f"{self.base}/file", target)
        self.assertEqual(target.read_bytes(), PAYLOAD)

    def test_a_dropped_connection_is_resumed(self) -> None:
        target = self.downloader.download(f"{self.base}/truncated", self.folder / "t.bin")
        self.assertEqual(target.read_bytes(), PAYLOAD)
        self.assertEqual(self.state.requests[1][1]["Range"], "bytes=1000-")

    def test_backs_off_on_server_errors(self) -> None:
        self.downloader.download(f"{self.base}/flaky", self.folder / "f.bin")
        self.assertEqual(self.sleeps, [0.0, 4.0], "Retry-After first, then doubled backoff")

    def test_honours_retry_after_on_429(self) -> None:
        self.downloader.download(f"{self.base}/limited", self.folder / "l.bin")
        self.assertEqual(self.sleeps, [3.0])

    def test_refuses_to_wait_very_long(self) -> None:
        with self.assertRaises(DownloadError) as caught:
            self.downloader.download(f"{self.base}/wait-long", self.folder / "w.bin")
        self.assertIn("60 minutes", str(caught.exception))
        self.assertEqual(self.sleeps, [])

    def test_gives_up_after_the_retries(self) -> None:
        downloader = Downloader(throttle=HostThrottle(0.0), sleep=self.sleeps.append, retries=2)
        with self.assertRaises(DownloadError) as caught:
            downloader.download(f"{self.base}/always-503", self.folder / "x.bin")
        self.assertIn("503", str(caught.exception))
        self.assertEqual(self.sleeps, [2.0, 4.0])
        self.assertEqual(self.state.counters["/always-503"], 3)

    def test_missing_file_is_not_retried(self) -> None:
        with self.assertRaises(DownloadError) as caught:
            self.downloader.download(f"{self.base}/nothing", self.folder / "n.bin")
        self.assertIn("404", str(caught.exception))
        self.assertEqual(len(self.state.requests), 1)

    def test_unreachable_host(self) -> None:
        downloader = Downloader(throttle=HostThrottle(0.0), sleep=self.sleeps.append, retries=1)
        with self.assertRaises(DownloadError) as caught:
            downloader.download("http://127.0.0.1:9/none", self.folder / "u.bin")
        self.assertIn("could not be reached", str(caught.exception))

    def test_only_web_addresses(self) -> None:
        with self.assertRaises(DownloadError):
            self.downloader.download("file:///etc/passwd", self.folder / "p.bin")

    def test_cancel_before_and_during(self) -> None:
        cancel = Cancellation()
        cancel.cancel()
        with self.assertRaises(DownloadCancelled):
            self.downloader.download(f"{self.base}/file", self.folder / "c.bin", cancel=cancel)
        cancel = Cancellation()

        def progress(done: int, total: int | None) -> None:
            cancel.cancel()  # the first report comes before the first chunk

        with self.assertRaises(DownloadCancelled):
            self.downloader.download(
                f"{self.base}/file", self.folder / "d.bin", progress=progress, cancel=cancel
            )
        self.assertTrue((self.folder / "d.bin.part").exists())
        self.assertFalse((self.folder / "d.bin").exists())

    def test_get_json(self) -> None:
        self.assertEqual(self.downloader.get_json(f"{self.base}/json"), {"ok": True})

    def test_requests_to_one_host_are_a_second_apart(self) -> None:
        downloader = Downloader(throttle=HostThrottle(1.0))
        downloader.get_bytes(f"{self.base}/json")
        downloader.get_bytes(f"{self.base}/json")
        first, second = (moment for _p, _h, moment in self.state.requests)
        self.assertGreaterEqual(second - first, 0.95)


class ThrottleTests(unittest.TestCase):
    def test_default_interval_is_one_second(self) -> None:
        self.assertEqual(HostThrottle().interval, 1.0)

    def test_waits_per_host(self) -> None:
        now = [100.0]
        waits: list[float] = []
        throttle = HostThrottle(1.0, clock=lambda: now[0], sleep=waits.append)
        throttle.wait("a")
        throttle.wait("b")
        throttle.wait("a")
        now[0] += 0.4
        throttle.wait("a")
        self.assertEqual(len(waits), 2)
        self.assertAlmostEqual(waits[0], 1.0)
        self.assertAlmostEqual(waits[1], 1.6)


class RetryAfterTests(unittest.TestCase):
    def test_seconds_and_dates(self) -> None:
        self.assertEqual(_retry_after("12"), 12.0)
        now = datetime(2026, 9, 12, 12, 0, 0, tzinfo=UTC)
        later = email.utils.format_datetime(now + timedelta(seconds=30), usegmt=True)
        self.assertAlmostEqual(_retry_after(later, now), 30.0)
        self.assertIsNone(_retry_after("soon"))
        self.assertIsNone(_retry_after(None))


if __name__ == "__main__":
    unittest.main()

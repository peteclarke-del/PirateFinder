"""Pictures and Wikipedia summaries fetched on demand, against a local web server.

The pictures are a few bytes with the right magic numbers; the "Wikipedia"
is the local server answering in the REST summary format.
"""

from __future__ import annotations

import hashlib
import http.server
import json
import shutil
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from piratefinder.branding import HOMEPAGE
from piratefinder.models import MediaItem
from piratefinder.online import media
from piratefinder.online.http import (
    Downloader,
    DownloadError,
    HostThrottle,
    HttpReply,
    ReplyTooLarge,
)
from tests.test_library_helpers import serve

PNG = b"\x89PNG\r\n\x1a\n" + b"synthetic picture"
GIF = b"GIF89a" + b"another synthetic picture"
JPEG = b"\xff\xd8\xff\xe0" + b"a third"
DAY = 24 * 60 * 60

SUMMARIES = {
    "Automation_(group)": {
        "type": "standard",
        "title": "Automation (group)",
        "extract": "Automation was a cracking group on the Atari ST.",
        "content_urls": {"desktop": {"page": "https://en.wikipedia.invalid/wiki/Automation"}},
        "thumbnail": {"source": "/thumb.png"},
    },
    "Mercury": {"type": "disambiguation", "title": "Mercury", "extract": "Mercury may refer to"},
    "AC%2FDC": {
        "type": "standard",
        "title": "AC/DC",
        "extract": "A band.",
        "content_urls": {"desktop": {"page": "https://en.wikipedia.invalid/wiki/AC/DC"}},
    },
}


class Server:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.requests: list[tuple[str, dict[str, str]]] = []
        self.pictures = {"/pic.png": PNG, "/pic.gif": GIF, "/pic.jpg": JPEG, "/other.png": PNG}
        self.etag = '"v1"'
        self.active = 0
        self.most_active = 0
        self.fail = False

    def handler(self) -> type[http.server.BaseHTTPRequestHandler]:
        server = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: object) -> None:  # noqa: A002
                return None

            def do_GET(self) -> None:  # noqa: N802
                with server.lock:
                    server.requests.append((self.path, dict(self.headers)))
                    server.active += 1
                    server.most_active = max(server.most_active, server.active)
                try:
                    time.sleep(0.02)
                    self.answer()
                finally:
                    with server.lock:
                        server.active -= 1

            def answer(self) -> None:
                if server.fail:
                    self.send(500, b"")
                elif self.path in server.pictures:
                    if self.headers.get("If-None-Match") == server.etag:
                        self.send(304, b"")
                    else:
                        self.send(200, server.pictures[self.path], {"ETag": server.etag})
                elif self.path == "/page.png":
                    self.send(200, b"<html>not a picture</html>")
                elif self.path.startswith("/summary/"):
                    found = SUMMARIES.get(self.path.removeprefix("/summary/"))
                    if found is None:
                        self.send(404, b"{}")
                    else:
                        self.send(200, json.dumps(found).encode())
                else:
                    self.send(404, b"not here")

            def send(self, status: int, body: bytes, headers: dict | None = None) -> None:
                self.send_response(status)
                for name, value in (headers or {}).items():
                    self.send_header(name, value)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        return Handler

    def paths(self) -> list[str]:
        with self.lock:
            return [path for path, _headers in self.requests]


class MediaCacheTests(unittest.TestCase):
    def setUp(self) -> None:
        self.folder = Path(tempfile.mkdtemp(prefix="pf-media-"))
        self.addCleanup(shutil.rmtree, self.folder, True)
        self.server = Server()
        context = serve(self.server.handler())
        self.base = context.__enter__()
        self.addCleanup(context.__exit__, None, None, None)
        self.now = [1_000_000.0]
        self.enabled = [True]
        downloader = Downloader(throttle=HostThrottle(0), retries=0, sleep=lambda s: None)
        self.cache = media.MediaCache(
            downloader,
            self.folder / "media",
            enabled=lambda: self.enabled[0],
            clock=lambda: self.now[0],
        )
        patcher = mock.patch.object(media, "WIKIPEDIA_SUMMARY_URL", self.base + "/summary/{title}")
        patcher.start()
        self.addCleanup(patcher.stop)

    def item(self, path: str, source: str = "atari-legend") -> MediaItem:
        return MediaItem("menu", self.base + path, source)

    def test_a_picture_is_cached_under_its_source(self) -> None:
        path = self.cache.fetch(self.item("/pic.png", "Atari Legend"))
        digest = hashlib.sha1((self.base + "/pic.png").encode()).hexdigest()
        self.assertEqual(path, self.folder / "media" / "atari_legend" / f"{digest}.png")
        self.assertEqual(path.read_bytes(), PNG)
        self.assertEqual(self.cache.fetch(self.item("/pic.gif")).suffix, ".gif")
        self.assertEqual(self.cache.fetch(self.item("/pic.jpg")).suffix, ".jpg")
        user_agent = self.server.requests[0][1]["User-Agent"]
        self.assertIn("PirateFinder", user_agent)
        self.assertIn(HOMEPAGE, user_agent)

    def test_a_fresh_picture_is_not_asked_for_again_and_an_old_one_is_revalidated(self) -> None:
        first = self.cache.fetch(self.item("/pic.png"))
        self.now[0] += 29 * DAY
        self.assertEqual(self.cache.fetch(self.item("/pic.png")), first)
        self.assertEqual(self.server.paths(), ["/pic.png"])
        self.now[0] += 2 * DAY
        self.assertEqual(self.cache.fetch(self.item("/pic.png")), first)
        self.assertEqual(self.server.requests[-1][1].get("If-None-Match"), '"v1"')
        self.assertEqual(len(self.server.requests), 2, "revalidated with a 304")
        self.assertEqual(first.read_bytes(), PNG)
        # The revalidation counts as fresh again.
        self.now[0] += 29 * DAY
        self.cache.fetch(self.item("/pic.png"))
        self.assertEqual(len(self.server.requests), 2)
        # A changed picture replaces the cached one, whatever its type.
        self.server.etag = '"v2"'
        self.server.pictures["/pic.png"] = GIF
        self.now[0] += 31 * DAY
        changed = self.cache.fetch(self.item("/pic.png"))
        self.assertEqual((changed.suffix, changed.read_bytes()), (".gif", GIF))
        self.assertFalse(first.exists())

    def test_one_miss_is_held_for_an_hour_and_two_in_a_row_for_a_week(self) -> None:
        for path, reason in (("/gone.png", "HTTP 404"), ("/page.png", "not a picture")):
            with self.subTest(path=path):
                self.assertIsNone(self.cache.fetch(self.item(path)))
                count = len(self.server.requests)
                note = self.note(path)
                self.assertEqual((note["misses"], note["reason"]), (1, reason))
                self.now[0] += 50 * 60
                self.assertIsNone(self.cache.fetch(self.item(path)))
                self.assertEqual(len(self.server.requests), count)
                # A busy site says so of pictures it has: after an hour, ask again.
                self.now[0] += 20 * 60
                self.assertIsNone(self.cache.fetch(self.item(path)))
                self.assertEqual(len(self.server.requests), count + 1)
                self.assertEqual(self.note(path)["misses"], 2)
                self.now[0] += 6 * DAY
                self.assertIsNone(self.cache.fetch(self.item(path)))
                self.assertEqual(len(self.server.requests), count + 1)
                self.now[0] += 2 * DAY
                self.cache.fetch(self.item(path))
                self.assertEqual(len(self.server.requests), count + 2)

    def test_a_cached_picture_survives_one_miss_and_goes_on_the_second(self) -> None:
        first = self.cache.fetch(self.item("/pic.png"))
        pictures = self.server.pictures
        self.server.pictures = {}
        self.now[0] += 31 * DAY
        self.assertEqual(self.cache.fetch(self.item("/pic.png")), first)
        self.assertTrue(first.is_file())
        self.assertEqual(self.note("/pic.png")["misses"], 1)
        self.assertIsNone(self.cache.fetch(self.item("/pic.png")))
        self.assertFalse(first.exists())
        self.assertEqual(self.cache.misses(self.base + "/pic.png", "atari-legend"), 2)
        # Back on the site: found again, and the misses forgotten.
        self.server.pictures = pictures
        self.now[0] += 8 * DAY
        self.assertEqual(self.cache.fetch(self.item("/pic.png")), first)
        self.assertEqual(self.cache.misses(self.base + "/pic.png", "atari-legend"), 0)

    def test_a_held_picture_can_be_asked_for_again_on_purpose(self) -> None:
        self.assertIsNone(self.cache.fetch(self.item("/gone.png")))
        count = len(self.server.requests)
        self.assertEqual(self.cache.fetch_telling(self.item("/gone.png")), (None, False))
        self.assertEqual(
            self.cache.fetch_telling(self.item("/gone.png"), recheck_missing=True), (None, True)
        )
        self.assertEqual(len(self.server.requests), count + 1)

    def test_a_note_from_before_misses_were_counted_is_one_miss(self) -> None:
        folder, key = media.picture_key(self.base + "/gone.png", "atari-legend")
        (self.folder / "media" / folder).mkdir(parents=True)
        note = {"url": self.base + "/gone.png", "status": "missing", "fetched": self.now[0]}
        (self.folder / "media" / folder / f"{key}.json").write_text(json.dumps(note))
        self.assertEqual(self.cache.misses(self.base + "/gone.png", "atari-legend"), 1)
        self.now[0] += 2 * 60 * 60
        self.assertIsNone(self.cache.fetch(self.item("/gone.png")))
        self.assertEqual(self.note("/gone.png")["misses"], 2)

    def note(self, path: str) -> dict:
        folder, key = media.picture_key(self.base + path, "atari-legend")
        return json.loads((self.folder / "media" / folder / f"{key}.json").read_text())

    def test_a_picture_that_is_too_large_is_refused(self) -> None:
        with mock.patch.object(media, "MAX_PICTURE_BYTES", 10):
            self.assertIsNone(self.cache.fetch(self.item("/pic.png")))
        self.assertEqual(list((self.folder / "media").rglob("*.png")), [])

    def test_a_stale_picture_is_used_while_the_site_is_down(self) -> None:
        first = self.cache.fetch(self.item("/pic.png"))
        self.server.fail = True
        self.now[0] += 40 * DAY
        self.assertEqual(self.cache.fetch(self.item("/pic.png")), first)
        self.assertIsNone(self.cache.fetch(self.item("/other.png")))
        self.server.fail = False
        self.assertIsNotNone(self.cache.fetch(self.item("/other.png")), "errors are not cached")

    def test_nothing_is_fetched_when_switched_off(self) -> None:
        self.enabled[0] = False
        self.assertIsNone(self.cache.fetch(self.item("/pic.png")))
        self.assertIsNone(self.cache.wikipedia_summary("Automation (group)"))
        self.assertEqual(self.server.requests, [])
        self.assertIsNone(self.cache.fetch(MediaItem("menu", "file:///etc/passwd", "x")))

    def test_one_request_at_a_time_per_host(self) -> None:
        threads = [
            threading.Thread(target=self.cache.fetch, args=(self.item(path),))
            for path in ("/pic.png", "/pic.gif", "/pic.jpg", "/other.png")
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(len(self.server.requests), 4)
        self.assertEqual(self.server.most_active, 1)

    def test_wikipedia_summary(self) -> None:
        item = self.cache.wikipedia_summary("Automation (group)")
        self.assertEqual(item.kind, "summary")
        self.assertEqual(item.text, "Automation was a cracking group on the Atari ST.")
        self.assertEqual(item.title, "Automation (group)")
        self.assertEqual(item.url, "https://en.wikipedia.invalid/wiki/Automation")
        self.assertEqual((item.source, item.licence), ("Wikipedia", "CC BY-SA 4.0"))
        self.assertEqual(self.server.paths(), ["/summary/Automation_(group)"])
        self.now[0] += 29 * DAY
        self.assertEqual(self.cache.wikipedia_summary("Automation (group)"), item)
        self.assertEqual(len(self.server.requests), 1, "cached, and the thumbnail never fetched")
        self.assertEqual(self.cache.wikipedia_summary("AC/DC").title, "AC/DC")

    def test_disambiguation_and_missing_articles(self) -> None:
        self.assertIsNone(self.cache.wikipedia_summary("Mercury"))
        self.assertIsNone(self.cache.wikipedia_summary("No Such Article"))
        count = len(self.server.requests)
        self.assertIsNone(self.cache.wikipedia_summary("Mercury"))
        self.assertEqual(len(self.server.requests), count)
        self.assertIsNone(self.cache.wikipedia_summary("  "))

    def test_a_cached_summary_is_used_while_wikipedia_is_down(self) -> None:
        item = self.cache.wikipedia_summary("Automation (group)")
        self.server.fail = True
        self.now[0] += 31 * DAY
        self.assertEqual(self.cache.wikipedia_summary("Automation (group)"), item)


class RecordingDownloader:
    def __init__(self) -> None:
        self.urls: list[str] = []

    def get_reply(self, url, *, headers=None, max_bytes=0, cancel=None) -> HttpReply:
        self.urls.append(url)
        return HttpReply(200, PNG)


class PoliteHostTests(unittest.TestCase):
    def test_host_names_of_a_small_site_share_one_throttle(self) -> None:
        self.assertEqual(media.host_key("https://media.demozoo.org/a.png"), "demozoo.org")
        self.assertEqual(media.host_key("https://www.atarilegend.com/x"), "atarilegend.com")
        self.assertEqual(media.host_key("https://d-bug.me/x"), "d-bug.me")
        self.assertEqual(media.host_key("https://notd-bug.me/x"), "notd-bug.me")
        self.assertEqual(media.host_key("https://en.wikipedia.org/x"), "en.wikipedia.org")

    def test_requests_to_small_sites_are_a_second_apart(self) -> None:
        clock = [0.0]
        waits: list[float] = []
        throttle = HostThrottle(1.0, clock=lambda: clock[0], sleep=waits.append)
        folder = Path(tempfile.mkdtemp(prefix="pf-polite-"))
        self.addCleanup(shutil.rmtree, folder, True)
        cache = media.MediaCache(RecordingDownloader(), folder)
        with mock.patch.object(media, "_THROTTLE", throttle):
            cache.fetch(MediaItem("snap", "https://www.atarilegend.com/a.png", "al"))
            cache.fetch(MediaItem("snap", "https://media.atarilegend.com/b.png", "al"))
            cache.fetch(MediaItem("snap", "https://en.wikipedia.org/c.png", "wp"))
        self.assertEqual(waits, [1.0])

    def test_picture_types(self) -> None:
        self.assertEqual(
            [media.picture_type(data) for data in (PNG, GIF, JPEG, b"GIF87a..", b"<html>")],
            ["png", "gif", "jpg", "gif", ""],
        )


class GetReplyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.server = Server()
        context = serve(self.server.handler())
        self.base = context.__enter__()
        self.addCleanup(context.__exit__, None, None, None)
        self.downloader = Downloader(throttle=HostThrottle(0), retries=1, sleep=lambda s: None)

    def test_not_modified_and_missing_are_replies(self) -> None:
        reply = self.downloader.get_reply(self.base + "/pic.png")
        self.assertEqual((reply.status, reply.data, reply.header("etag")), (200, PNG, '"v1"'))
        again = self.downloader.get_reply(self.base + "/pic.png", headers={"If-None-Match": '"v1"'})
        self.assertEqual((again.status, again.data), (304, b""))
        self.assertEqual(self.downloader.get_reply(self.base + "/gone").status, 404)

    def test_errors_are_retried_then_raised(self) -> None:
        self.server.fail = True
        with self.assertRaises(DownloadError):
            self.downloader.get_reply(self.base + "/pic.png")
        self.assertEqual(len(self.server.requests), 2)

    def test_a_reply_that_is_too_large(self) -> None:
        with self.assertRaises(ReplyTooLarge):
            self.downloader.get_reply(self.base + "/pic.png", max_bytes=4)


if __name__ == "__main__":
    unittest.main()

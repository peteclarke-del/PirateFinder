from __future__ import annotations

import functools
import gzip
import hashlib
import json
import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path

from piratefinder.catalogue import schema
from piratefinder.catalogue.update import (
    UpdateCancelled,
    UpdateError,
    UpdateInfo,
    asset_name,
    check_catalogue_file,
    check_for_update,
    install_update,
    newest_release,
)
from piratefinder.jobs.cancellation import Cancellation
from piratefinder.online.http import Downloader, HostThrottle
from tests.test_library_helpers import CatalogueBuilder, QuietHandler, serve

NAME = asset_name()  # the layout this version reads, catalogue-layout<N>.sqlite.gz
CHECKSUM = f"{NAME}.sha256"


def release(tag: str, assets: list[str], base: str = "http://x", **extra: object) -> dict:
    return {
        "tag_name": tag,
        "html_url": f"{base}/releases/{tag}",
        "body": "notes",
        "assets": [
            {"name": name, "browser_download_url": f"{base}/{name}", "size": 10} for name in assets
        ],
        **extra,
    }


class NewestReleaseTests(unittest.TestCase):
    def test_the_asset_is_named_by_its_layout(self) -> None:
        self.assertEqual(NAME, f"catalogue-layout{schema.SCHEMA_VERSION}.sqlite.gz")
        self.assertEqual(asset_name(7), "catalogue-layout7.sqlite.gz")

    def test_picks_the_newest_complete_release(self) -> None:
        releases = [
            release("catalogue-2026-08-01", [NAME, CHECKSUM]),
            release("catalogue-2026-10-01", [NAME]),  # no checksum
            release("catalogue-2026-11-01", [NAME, CHECKSUM], draft=True),
            release("catalogue-2026-12-01", [NAME, CHECKSUM], prerelease=True),
            release("catalogue-2026-09-05", [NAME, CHECKSUM]),
            release("v0.2.0", ["piratefinder.deb"]),
        ]
        info = newest_release(releases, "2026-09-01T10:00:00Z")
        self.assertIsNotNone(info)
        assert info is not None
        self.assertEqual(info.built_at, "2026-09-05")
        self.assertEqual(info.url, f"http://x/{NAME}")
        self.assertEqual(info.sha256_url, f"http://x/{CHECKSUM}")
        self.assertEqual(info.asset_name, NAME)
        self.assertEqual(info.size, 10)

    def test_only_the_layout_this_version_reads_is_offered(self) -> None:
        other = asset_name(schema.SCHEMA_VERSION + 1)
        releases = [
            release("catalogue-2026-09-05", [NAME, CHECKSUM]),
            release("catalogue-2026-10-01", [other, f"{other}.sha256"]),
            # The name catalogues had before assets were named by layout.
            release("catalogue-2026-10-02", ["catalogue.sqlite.gz", "catalogue.sqlite.gz.sha256"]),
        ]
        info = newest_release(releases, "2026-09-01")
        assert info is not None
        self.assertEqual(info.built_at, "2026-09-05")
        self.assertIsNone(newest_release(releases[1:], ""))

    def test_a_tag_without_a_date_is_passed_over(self) -> None:
        self.assertIsNone(newest_release([release("latest", [NAME, CHECKSUM])], ""))

    def test_nothing_newer(self) -> None:
        releases = [release("catalogue-2026-09-01", [NAME, CHECKSUM])]
        self.assertIsNone(newest_release(releases, "2026-09-01"))
        self.assertIsNotNone(newest_release(releases, ""))


class UpdateServerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.folder = Path(tempfile.mkdtemp(prefix="pf-update-"))
        self.addCleanup(shutil.rmtree, self.folder, True)
        self.site = self.folder / "site"
        self.site.mkdir()
        self.install = self.folder / "data"
        self.target = self.install / "catalogue.sqlite"
        context = serve(functools.partial(QuietHandler, directory=str(self.site)))
        self.base = context.__enter__()
        self.addCleanup(context.__exit__, None, None, None)
        self.downloader = Downloader(throttle=HostThrottle(0.0), sleep=lambda _s: None, retries=1)

    def publish_catalogue(
        self, *, built_at: str = "2026-09-20", checksum: str | None = None
    ) -> UpdateInfo:
        path = CatalogueBuilder(self.folder / "build.sqlite", built_at=built_at)
        path.disk(1, "Crew 1")
        database = path.close()
        compressed = gzip.compress(database.read_bytes())
        (self.site / NAME).write_bytes(compressed)
        digest = checksum or hashlib.sha256(compressed).hexdigest()
        (self.site / CHECKSUM).write_text(f"{digest}  {NAME}\n")
        tag = f"catalogue-{built_at}"
        releases = [release(tag, [NAME, CHECKSUM], self.base)]
        (self.site / "releases").write_text(json.dumps(releases))
        database.unlink()
        info = check_for_update("2026-09-01", f"{self.base}/releases", downloader=self.downloader)
        assert info is not None
        return info

    def assert_no_leftovers(self) -> None:
        names = (
            sorted(path.name for path in self.install.iterdir()) if self.install.exists() else []
        )
        self.assertEqual([name for name in names if name != "catalogue.sqlite"], [])

    def test_check_and_install(self) -> None:
        info = self.publish_catalogue()
        self.assertEqual(info.built_at, "2026-09-20")
        calls: list[tuple[int, int | None]] = []
        installed = install_update(
            info, lambda *c: calls.append(c), downloader=self.downloader, target=self.target
        )
        self.assertEqual(installed, self.target)
        self.assertEqual(check_catalogue_file(self.target), "2026-09-20")
        self.assertTrue(calls)
        self.assert_no_leftovers()

    def test_checksum_of_the_uncompressed_file_is_accepted(self) -> None:
        builder = CatalogueBuilder(self.folder / "probe.sqlite", built_at="2026-09-20")
        builder.disk(1, "Crew 1")
        raw = builder.close().read_bytes()
        info = self.publish_catalogue(checksum=hashlib.sha256(raw).hexdigest())
        install_update(info, downloader=self.downloader, target=self.target)
        self.assertTrue(self.target.exists())

    def test_checksum_mismatch_installs_nothing(self) -> None:
        self.install.mkdir()
        self.target.write_bytes(b"old catalogue")
        info = self.publish_catalogue(checksum="0" * 64)
        with self.assertRaises(UpdateError) as caught:
            install_update(info, downloader=self.downloader, target=self.target)
        self.assertIn("does not match its published checksum", str(caught.exception))
        self.assertEqual(self.target.read_bytes(), b"old catalogue")
        self.assert_no_leftovers()

    def test_wrong_schema_is_refused(self) -> None:
        info = self.publish_catalogue()
        connection = sqlite3.connect(self.folder / "future.sqlite")
        schema.create(connection)
        connection.execute(
            "UPDATE meta SET value = ? WHERE key = 'schema_version'",
            (str(schema.SCHEMA_VERSION + 1),),
        )
        connection.commit()
        connection.close()
        compressed = gzip.compress((self.folder / "future.sqlite").read_bytes())
        (self.site / NAME).write_bytes(compressed)
        (self.site / CHECKSUM).write_text(hashlib.sha256(compressed).hexdigest())
        with self.assertRaises(UpdateError) as caught:
            install_update(info, downloader=self.downloader, target=self.target)
        self.assertEqual(
            str(caught.exception),
            "The downloaded catalogue was made for a different PirateFinder version "
            f"(layout {schema.SCHEMA_VERSION + 1}; this version reads layout "
            f"{schema.SCHEMA_VERSION}).",
        )
        self.assertFalse(self.target.exists())
        self.assert_no_leftovers()

    def test_not_a_catalogue(self) -> None:
        info = self.publish_catalogue()
        compressed = gzip.compress(b"just some text")
        (self.site / NAME).write_bytes(compressed)
        (self.site / CHECKSUM).write_text(hashlib.sha256(compressed).hexdigest())
        with self.assertRaises(UpdateError):
            install_update(info, downloader=self.downloader, target=self.target)
        self.assert_no_leftovers()

    def test_cut_short_download(self) -> None:
        info = self.publish_catalogue()
        compressed = (self.site / NAME).read_bytes()[:-20]
        (self.site / NAME).write_bytes(compressed)
        (self.site / CHECKSUM).write_text(hashlib.sha256(compressed).hexdigest())
        with self.assertRaises(UpdateError):
            install_update(info, downloader=self.downloader, target=self.target)
        self.assert_no_leftovers()

    def test_cancelled(self) -> None:
        info = self.publish_catalogue()
        cancel = Cancellation()
        cancel.cancel()
        with self.assertRaises(UpdateCancelled):
            install_update(info, cancel=cancel, downloader=self.downloader, target=self.target)
        self.assertFalse(self.target.exists())
        self.assert_no_leftovers()

    def test_a_check_that_fails_says_why(self) -> None:
        with self.assertRaises(UpdateError) as caught:
            check_for_update("", "http://127.0.0.1:9/releases", downloader=self.downloader)
        self.assertIn("127.0.0.1:9 could not be reached", str(caught.exception))
        (self.site / "odd").write_text('{"message": "API rate limit exceeded"}')
        with self.assertRaises(UpdateError) as caught:
            check_for_update("", f"{self.base}/odd", downloader=self.downloader)
        host = self.base.removeprefix("http://")
        self.assertEqual(
            str(caught.exception),
            f"{host} did not send a list of releases: API rate limit exceeded.",
        )
        (self.site / "broken").write_text("<html>")
        with self.assertRaises(UpdateError) as caught:
            check_for_update("", f"{self.base}/broken", downloader=self.downloader)
        self.assertIn("could not be read", str(caught.exception))
        with self.assertRaises(UpdateError):
            check_for_update("", f"{self.base}/absent", downloader=self.downloader)

    def test_a_check_that_works_and_finds_nothing_gives_none(self) -> None:
        (self.site / "releases").write_text(json.dumps([release("v0.1.0", ["x.deb"])]))
        self.assertIsNone(check_for_update("", f"{self.base}/releases", downloader=self.downloader))

    def test_missing_asset_raises(self) -> None:
        info = self.publish_catalogue()
        (self.site / NAME).unlink()
        with self.assertRaises(UpdateError) as caught:
            install_update(info, downloader=self.downloader, target=self.target)
        self.assertIn("could not be downloaded", str(caught.exception))
        self.assert_no_leftovers()


if __name__ == "__main__":
    unittest.main()

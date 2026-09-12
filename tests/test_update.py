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
    check_catalogue_file,
    check_for_update,
    install_update,
    newest_release,
)
from piratefinder.jobs.cancellation import Cancellation
from piratefinder.online.http import Downloader, HostThrottle
from tests.test_library_helpers import CatalogueBuilder, QuietHandler, serve


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
    def test_picks_the_newest_complete_release(self) -> None:
        releases = [
            release("catalogue-2026-08-01", ["catalogue.sqlite.gz", "catalogue.sqlite.gz.sha256"]),
            release("catalogue-2026-10-01", ["catalogue.sqlite.gz"]),  # no checksum
            release(
                "catalogue-2026-11-01",
                ["catalogue.sqlite.gz", "catalogue.sqlite.gz.sha256"],
                draft=True,
            ),
            release(
                "catalogue-2026-12-01",
                ["catalogue.sqlite.gz", "catalogue.sqlite.gz.sha256"],
                prerelease=True,
            ),
            release("catalogue-2026-09-05", ["catalogue.sqlite.gz", "catalogue.sqlite.gz.sha256"]),
            release("v0.2.0", ["piratefinder.deb"]),
        ]
        info = newest_release(releases, "2026-09-01T10:00:00Z")
        self.assertIsNotNone(info)
        assert info is not None
        self.assertEqual(info.built_at, "2026-09-05")
        self.assertEqual(info.url, "http://x/catalogue.sqlite.gz")
        self.assertEqual(info.sha256_url, "http://x/catalogue.sqlite.gz.sha256")
        self.assertEqual(info.size, 10)

    def test_date_from_the_asset_name(self) -> None:
        releases = [
            release(
                "latest", ["catalogue-2026-09-20.sqlite.gz", "catalogue-2026-09-20.sqlite.sha256"]
            )
        ]
        info = newest_release(releases, "2026-09-01")
        assert info is not None
        self.assertEqual(info.built_at, "2026-09-20")

    def test_nothing_newer(self) -> None:
        releases = [
            release("catalogue-2026-09-01", ["catalogue.sqlite.gz", "catalogue.sqlite.gz.sha256"])
        ]
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
        (self.site / "catalogue.sqlite.gz").write_bytes(compressed)
        digest = checksum or hashlib.sha256(compressed).hexdigest()
        (self.site / "catalogue.sqlite.gz.sha256").write_text(f"{digest}  catalogue.sqlite.gz\n")
        tag = f"catalogue-{built_at}"
        releases = [release(tag, ["catalogue.sqlite.gz", "catalogue.sqlite.gz.sha256"], self.base)]
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
        (self.site / "catalogue.sqlite.gz").write_bytes(compressed)
        (self.site / "catalogue.sqlite.gz.sha256").write_text(
            hashlib.sha256(compressed).hexdigest()
        )
        with self.assertRaises(UpdateError) as caught:
            install_update(info, downloader=self.downloader, target=self.target)
        self.assertIn("newer version of PirateFinder", str(caught.exception))
        self.assertFalse(self.target.exists())
        self.assert_no_leftovers()

    def test_not_a_catalogue(self) -> None:
        info = self.publish_catalogue()
        compressed = gzip.compress(b"just some text")
        (self.site / "catalogue.sqlite.gz").write_bytes(compressed)
        (self.site / "catalogue.sqlite.gz.sha256").write_text(
            hashlib.sha256(compressed).hexdigest()
        )
        with self.assertRaises(UpdateError):
            install_update(info, downloader=self.downloader, target=self.target)
        self.assert_no_leftovers()

    def test_cut_short_download(self) -> None:
        info = self.publish_catalogue()
        compressed = (self.site / "catalogue.sqlite.gz").read_bytes()[:-20]
        (self.site / "catalogue.sqlite.gz").write_bytes(compressed)
        (self.site / "catalogue.sqlite.gz.sha256").write_text(
            hashlib.sha256(compressed).hexdigest()
        )
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

    def test_offline_or_bad_feed_gives_none(self) -> None:
        self.assertIsNone(
            check_for_update("", "http://127.0.0.1:9/releases", downloader=self.downloader)
        )
        (self.site / "odd").write_text('{"message": "rate limited"}')
        self.assertIsNone(check_for_update("", f"{self.base}/odd", downloader=self.downloader))
        (self.site / "broken").write_text("<html>")
        self.assertIsNone(check_for_update("", f"{self.base}/broken", downloader=self.downloader))

    def test_missing_asset_raises(self) -> None:
        info = self.publish_catalogue()
        (self.site / "catalogue.sqlite.gz").unlink()
        with self.assertRaises(UpdateError) as caught:
            install_update(info, downloader=self.downloader, target=self.target)
        self.assertIn("could not be downloaded", str(caught.exception))
        self.assert_no_leftovers()


if __name__ == "__main__":
    unittest.main()

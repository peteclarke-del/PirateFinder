"""Download All Pictures: the catalogue's picture list, the cache's count and the download."""

from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

from piratefinder.catalogue.store import Catalogue
from piratefinder.jobs.cancellation import Cancellation
from piratefinder.models import MediaItem, Platform
from piratefinder.online import media, prefetch
from piratefinder.online.http import Downloader, HostThrottle
from tests.test_library_helpers import CatalogueBuilder, serve
from tests.test_online_media import GIF, PNG, Server


class PictureAddressTests(unittest.TestCase):
    def setUp(self) -> None:
        folder = Path(tempfile.mkdtemp(prefix="pf-pictures-"))
        self.addCleanup(shutil.rmtree, folder, True)
        builder = CatalogueBuilder(folder / "catalogue.sqlite")
        builder.disk(1, "Automation 250", contents=["Necron"])
        builder.disk(2, "Menu 2", platform=Platform.AMIGA)
        builder.disk(3, "Automation 251")
        builder.media(3, "https://m/c.png", source="atari-legend")
        builder.media(1, "https://m/a.png", source="atari-legend")
        builder.media(1, "https://m/b.png", source="demozoo", content_id=1)
        builder.media(2, "https://m/amiga.png", source="demozoo")
        # The same picture filed for two discs is fetched once.
        builder.media(3, "https://m/a.png", source="atari-legend")
        self.catalogue = Catalogue.open(builder.close())
        self.addCleanup(self.catalogue.close)

    def test_every_picture_once_in_disc_order(self) -> None:
        self.assertEqual(
            self.catalogue.picture_addresses(),
            [
                ("https://m/a.png", "atari-legend"),
                ("https://m/b.png", "demozoo"),
                ("https://m/amiga.png", "demozoo"),
                ("https://m/c.png", "atari-legend"),
            ],
        )

    def test_only_the_chosen_platforms(self) -> None:
        self.assertEqual(
            self.catalogue.picture_addresses([Platform.AMIGA]),
            [("https://m/amiga.png", "demozoo")],
        )
        self.assertEqual(len(self.catalogue.picture_addresses([Platform.ATARI_ST])), 3)
        self.assertEqual(
            len(self.catalogue.picture_addresses([Platform.ATARI_ST, Platform.AMIGA])), 4
        )


class PrefetchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.folder = Path(tempfile.mkdtemp(prefix="pf-prefetch-"))
        self.addCleanup(shutil.rmtree, self.folder, True)
        self.server = Server()
        self.server.pictures = {f"/{n}.png": PNG for n in range(6)} | {"/big.gif": GIF}
        context = serve(self.server.handler())
        base = context.__enter__()
        self.addCleanup(context.__exit__, None, None, None)
        port = base.rsplit(":", 1)[1]
        # Two names for the one server, so the pictures come from two sites.
        self.sites = (f"http://127.0.0.1:{port}", f"http://localhost:{port}")
        self.enabled = [True]
        downloader = Downloader(throttle=HostThrottle(0), retries=0, sleep=lambda s: None)
        self.cache = media.MediaCache(
            downloader, self.folder / "media", enabled=lambda: self.enabled[0]
        )
        first, second = self.sites
        self.addresses = [
            (f"{first}/0.png", "atari-legend"),
            (f"{first}/1.png", "atari-legend"),
            (f"{first}/2.png", "atari-legend"),
            (f"{second}/3.png", "demozoo"),
            (f"{second}/big.gif", "demozoo"),
            (f"{second}/gone.png", "demozoo"),
        ]

    def test_the_count_says_what_is_cached_and_how_long_the_rest_takes(self) -> None:
        count = prefetch.count(self.cache, self.addresses)
        self.assertEqual((count.total, count.cached, count.size), (6, 0, 0))
        self.assertEqual(count.remaining_by_site, {"127.0.0.1": 3, "localhost": 3})
        self.assertEqual(count.seconds, 3 * prefetch.SECONDS_PER_PICTURE)
        self.assertIsNone(count.estimated_size)  # too few pictures to go by
        self.cache.fetch(MediaItem("", self.addresses[0][0], "atari-legend"))
        count = prefetch.count(self.cache, self.addresses)
        self.assertEqual((count.cached, count.size), (1, len(PNG)))
        self.assertEqual(count.remaining_by_site, {"127.0.0.1": 2, "localhost": 3})

    def test_an_estimate_comes_from_the_pictures_cached_so_far(self) -> None:
        count = prefetch.PictureCount(1000, 200, 200 * 5000, {"a": 800})
        self.assertEqual(count.estimated_size, 800 * 5000)

    def test_every_picture_goes_into_the_panes_cache(self) -> None:
        steps: list[prefetch.PictureProgress] = []
        summary = prefetch.download(self.cache, self.addresses, steps.append)
        self.assertEqual(
            (summary.total, summary.already, summary.fetched, summary.unavailable), (6, 0, 5, 1)
        )
        self.assertFalse(summary.stopped)
        self.assertEqual(steps[-1].done, 6)
        # The pane finds each one without asking the site again.
        asked = len(self.server.requests)
        path = self.cache.fetch(MediaItem("snap", self.addresses[4][0], "demozoo"))
        self.assertEqual(path.read_bytes(), GIF)
        self.assertEqual(len(self.server.requests), asked)
        self.assertEqual(prefetch.count(self.cache, self.addresses).cached, 5)

    def test_a_second_run_carries_on_without_asking_again(self) -> None:
        prefetch.download(self.cache, self.addresses[:2])
        asked = len(self.server.requests)
        summary = prefetch.download(self.cache, self.addresses)
        self.assertEqual((summary.already, summary.fetched, summary.unavailable), (2, 3, 1))
        self.assertEqual(len(self.server.requests), asked + 4)
        # A picture the site did not have is not asked for again for a while.
        again = prefetch.download(self.cache, self.addresses)
        self.assertEqual((again.already, again.fetched, again.unavailable), (5, 0, 1))
        self.assertEqual(len(self.server.requests), asked + 4)

    def test_sites_are_fetched_side_by_side_one_request_each_at_a_time(self) -> None:
        prefetch.download(self.cache, self.addresses)
        # One worker per site: both sites at once, never two requests to one.
        self.assertEqual(self.server.most_active, len(self.sites))

    def test_stop_and_the_switch_end_the_download(self) -> None:
        cancel = Cancellation()
        cancel.cancel()
        summary = prefetch.download(self.cache, self.addresses, cancel=cancel)
        self.assertTrue(summary.stopped)
        self.assertEqual(self.server.requests, [])
        self.enabled[0] = False
        summary = prefetch.download(self.cache, self.addresses)
        self.assertTrue(summary.stopped)
        self.assertEqual(self.server.requests, [])

    def test_the_cache_knows_its_pictures_without_the_network(self) -> None:
        url = self.addresses[0][0]
        self.assertIsNone(self.cache.cached_picture(url, "atari-legend"))
        fetched = self.cache.fetch(MediaItem("", url, "atari-legend"))
        self.assertEqual(self.cache.cached_picture(url, "atari-legend"), fetched)
        folder, key = media.picture_key(url, "atari-legend")
        self.assertEqual(fetched, self.folder / "media" / folder / f"{key}.png")
        self.assertEqual(self.cache.cached_keys(), {folder: {key}})
        self.assertEqual(self.cache.picture_bytes(), len(PNG))


if __name__ == "__main__":
    unittest.main()

"""The shared fetcher: request spacing per host from data/fetch-hosts.toml."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from catalogue_builder import context
from catalogue_builder.context import BuildContext, HostPolicy
from catalogue_builder.series import SeriesRegistry

# The Crawl-delay each site's robots.txt asks for (read September 2026).
CRAWL_DELAYS = {
    "https://www.tosecdev.org/downloads": 5.0,
    "https://www.exxosforum.co.uk/atari/games/POV/page1.htm": 5.0,
    "https://demozoo.org/productions/1/": 10.0,
}


class _Reply:
    def __init__(self, data: bytes) -> None:
        self._data = [data, b""]

    def read(self, _size: int) -> bytes:
        return self._data.pop(0)

    def __enter__(self) -> _Reply:
        return self

    def __exit__(self, *exc_info: object) -> None:
        return None


class _Clock:
    """time.monotonic and time.sleep that only move when asked to sleep."""

    def __init__(self) -> None:
        self.now = 1000.0
        self.slept: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


class HostPolicyTest(unittest.TestCase):
    def test_crawl_delays_are_honoured(self) -> None:
        policy = HostPolicy.load()
        for url, delay in CRAWL_DELAYS.items():
            self.assertGreaterEqual(policy.interval(url), delay, url)

    def test_hosts_are_compared_exactly(self) -> None:
        policy = HostPolicy.load()
        # The export host is not the website, and has no Crawl-delay.
        self.assertEqual(policy.interval("https://data.demozoo.org/x.sql.gz"), policy.default)
        self.assertEqual(policy.interval("https://DEMOZOO.ORG/groups/1/"), 10.0)
        self.assertEqual(policy.interval("https://unknown.example/"), policy.default)

    def test_a_host_listed_twice_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "hosts.toml"
            path.write_text(
                'default_interval = 1.0\n[[host]]\nhost = "a.example"\ninterval = 2\n'
                '[[host]]\nhost = "A.example"\ninterval = 3\n'
            )
            with self.assertRaisesRegex(ValueError, "listed twice"):
                HostPolicy.load(path)


class FetchSpacingTest(unittest.TestCase):
    def fetch_all(self, urls: list[str]) -> list[float]:
        clock = _Clock()
        with (
            tempfile.TemporaryDirectory() as folder,
            mock.patch.object(context.time, "monotonic", clock.monotonic),
            mock.patch.object(context.time, "sleep", clock.sleep),
            mock.patch.object(
                context.urllib.request, "urlopen", side_effect=lambda *a, **k: _Reply(b"x")
            ),
        ):
            ctx = BuildContext(cache_dir=Path(folder), series=SeriesRegistry({}))
            for url in urls:
                ctx.fetch(url)
        return clock.slept

    def test_requests_to_one_host_wait_its_interval(self) -> None:
        slept = self.fetch_all(
            ["https://www.tosecdev.org/a", "https://www.tosecdev.org/b", "https://demozoo.org/c"]
        )
        # The second TOSEC page waits the full 5 seconds; the first request to
        # another host does not wait.
        self.assertEqual(slept, [5.0])

    def test_each_request_to_demozoo_waits_ten_seconds(self) -> None:
        slept = self.fetch_all([f"https://demozoo.org/productions/{n}/" for n in range(3)])
        self.assertEqual(slept, [10.0, 10.0])

    def test_the_fetcher_takes_no_interval_of_its_own(self) -> None:
        # Intervals live only in data/fetch-hosts.toml; a source cannot pass one.
        with tempfile.TemporaryDirectory() as folder:
            ctx = BuildContext(cache_dir=Path(folder), series=SeriesRegistry({}), offline=True)
            with self.assertRaises(TypeError):
                ctx.fetch("https://www.tosecdev.org/", min_interval=0.0)


if __name__ == "__main__":
    unittest.main()

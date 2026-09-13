"""The application update: reading the latest release, and fetching and installing its package."""

from __future__ import annotations

import functools
import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from piratefinder import __version__, app_update
from piratefinder.__main__ import restart_command
from piratefinder.app_update import (
    AppRelease,
    PackageTarget,
    check,
    download,
    install,
    installed_target,
    is_newer,
    parse_version,
    published_sum,
    release_from,
)
from piratefinder.jobs.cancellation import Cancellation
from piratefinder.online.http import Downloader, HostThrottle
from piratefinder.online.releases import UpdateCancelled, UpdateError
from tests.test_library_helpers import QuietHandler, serve

UBUNTU = PackageTarget("ubuntu-24.04", "amd64")
PACKAGE = "PirateFinder_9.0.0_ubuntu-24.04_amd64.deb"


def release(tag: str = "v9.0.0", assets=(PACKAGE, "SHA256SUMS"), base="http://x", **extra):
    return {
        "tag_name": tag,
        "name": f"PirateFinder {tag.removeprefix('v')}",
        "html_url": f"{base}/releases/tag/{tag}",
        "body": "Faster searches.",
        "assets": [
            {"name": name, "browser_download_url": f"{base}/{name}", "size": 1234}
            for name in assets
        ],
        **extra,
    }


class VersionTests(unittest.TestCase):
    def test_only_application_tags_are_versions(self) -> None:
        self.assertEqual(parse_version("v0.2.0"), (0, 2, 0))
        self.assertEqual(parse_version("0.2.0"), (0, 2, 0))
        for tag in ("catalogue-2026-09-13", "v0.2", "v0.2.0-rc1", "v0.2.0.1", ""):
            self.assertIsNone(parse_version(tag), tag)

    def test_versions_compare_as_numbers(self) -> None:
        self.assertTrue(is_newer("v0.10.0", "0.9.0"))
        self.assertTrue(is_newer("v1.0.0", "0.99.99"))
        self.assertFalse(is_newer("v0.2.0", "0.2.0"))
        self.assertFalse(is_newer("v0.1.9", "0.2.0"))
        self.assertFalse(is_newer("catalogue-2026-09-13", "0.2.0"))

    def test_the_running_version_is_an_application_version(self) -> None:
        self.assertIsNotNone(parse_version(__version__))

    def test_restart_runs_the_same_module_with_the_same_arguments(self) -> None:
        self.assertEqual(
            restart_command(["/usr/lib/piratefinder/piratefinder/__main__.py", "--x"]),
            [sys.executable, "-m", "piratefinder", "--x"],
        )


class TargetTests(unittest.TestCase):
    def setUp(self) -> None:
        self.folder = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.folder)

    def test_the_file_the_package_writes_names_its_system(self) -> None:
        path = self.folder / "package-target"
        path.write_text("distro=debian-13\narch=arm64\n", encoding="utf-8")
        self.assertEqual(installed_target(path), PackageTarget("debian-13", "arm64"))

    def test_the_source_tree_and_a_broken_file_have_no_system(self) -> None:
        self.assertIsNone(installed_target(self.folder / "package-target"))
        path = self.folder / "package-target"
        path.write_text("distro=debian-13\n", encoding="utf-8")
        self.assertIsNone(installed_target(path))
        # The source tree has no package-target beside src/piratefinder.
        self.assertFalse(app_update.PACKAGE_TARGET.exists())

    def test_the_package_name_is_the_one_the_release_carries(self) -> None:
        self.assertEqual(UBUNTU.package_name("0.3.0"), "PirateFinder_0.3.0_ubuntu-24.04_amd64.deb")


class ReleaseTests(unittest.TestCase):
    def test_a_newer_release_with_this_systems_package_is_installable(self) -> None:
        found = release_from(release(), UBUNTU, current="0.2.0")
        assert found is not None
        self.assertEqual(
            (found.version, found.tag, found.name), ("9.0.0", "v9.0.0", "PirateFinder 9.0.0")
        )
        self.assertTrue(found.installable)
        self.assertEqual(found.package_name, PACKAGE)
        self.assertEqual(found.package_url, f"http://x/{PACKAGE}")
        self.assertEqual(found.package_size, 1234)
        self.assertEqual(found.sums_url, "http://x/SHA256SUMS")
        self.assertEqual(found.page_url, "http://x/releases/tag/v9.0.0")
        self.assertEqual(found.notes, "Faster searches.")

    def test_the_same_or_an_older_version_is_not_offered(self) -> None:
        self.assertIsNone(release_from(release("v0.2.0"), UBUNTU, current="0.2.0"))
        self.assertIsNone(release_from(release("v0.1.0"), UBUNTU, current="0.2.0"))

    def test_another_systems_package_or_the_source_tree_cannot_install(self) -> None:
        debian = release_from(release(), PackageTarget("debian-13", "armhf"), current="0.2.0")
        assert debian is not None
        self.assertFalse(debian.installable)
        self.assertEqual(debian.package_name, "")
        source = release_from(release(), None, current="0.2.0")
        assert source is not None
        self.assertFalse(source.installable)
        self.assertEqual(source.page_url, "http://x/releases/tag/v9.0.0")
        no_sums = release_from(release(assets=(PACKAGE,)), UBUNTU, current="0.2.0")
        assert no_sums is not None
        self.assertFalse(no_sums.installable)

    def test_a_catalogue_marked_as_the_latest_release_is_a_publishing_mistake(self) -> None:
        with self.assertRaises(UpdateError) as caught:
            release_from(release("catalogue-2026-09-13"), UBUNTU, current="0.2.0")
        self.assertIn("catalogue-2026-09-13, is not an application release", str(caught.exception))

    def test_long_notes_are_shortened(self) -> None:
        found = release_from(release(body="x" * 5000), UBUNTU, current="0.2.0")
        assert found is not None
        self.assertLess(len(found.notes), 2100)
        self.assertTrue(found.notes.endswith("The rest is on the release page."))

    def test_checksum_lines_are_read_as_sha256sum_writes_them(self) -> None:
        digest = "a" * 64
        sums = f"{'b' * 64}  other.deb\n{digest}  {PACKAGE}\n{'c' * 64} *binary.deb\n"
        self.assertEqual(published_sum(sums, PACKAGE), digest)
        self.assertEqual(published_sum(sums, "binary.deb"), "c" * 64)
        self.assertEqual(published_sum(sums, "absent.deb"), "")


class ServedTests(unittest.TestCase):
    """The check and the download against a local web server."""

    def setUp(self) -> None:
        self.site = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.site)
        self.cache = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.cache)
        context = serve(functools.partial(QuietHandler, directory=str(self.site)))
        self.base = context.__enter__()
        self.addCleanup(context.__exit__, None, None, None)
        self.downloader = Downloader(throttle=HostThrottle(0.0), sleep=lambda _s: None, retries=1)

    def publish(self, package: bytes = b"a package", sums: bytes | None = None) -> AppRelease:
        (self.site / PACKAGE).write_bytes(package)
        digest = hashlib.sha256(package).hexdigest()
        (self.site / "SHA256SUMS").write_bytes(sums or f"{digest}  {PACKAGE}\n".encode())
        (self.site / "latest").write_text(json.dumps(release(base=self.base)))
        found = check(
            UBUNTU, url=f"{self.base}/latest", current="0.2.0", downloader=self.downloader
        )
        assert found is not None
        return found

    def test_the_check_finds_the_newer_release(self) -> None:
        found = self.publish()
        self.assertEqual(found.version, "9.0.0")
        self.assertTrue(found.installable)
        self.assertIsNone(
            check(UBUNTU, url=f"{self.base}/latest", current="9.0.0", downloader=self.downloader)
        )

    def test_a_check_that_fails_says_why_and_never_says_newest(self) -> None:
        with self.assertRaises(UpdateError) as caught:
            check(UBUNTU, url="http://127.0.0.1:9/latest", downloader=self.downloader)
        self.assertIn("127.0.0.1:9 could not be reached", str(caught.exception))
        with self.assertRaises(UpdateError) as caught:
            check(UBUNTU, url=f"{self.base}/absent", downloader=self.downloader)
        self.assertEqual(
            str(caught.exception), "No application release has been published on GitHub yet."
        )
        (self.site / "odd").write_text('{"message": "API rate limit exceeded"}')
        with self.assertRaises(UpdateError) as caught:
            check(UBUNTU, url=f"{self.base}/odd", downloader=self.downloader)
        self.assertIn("did not send a release: API rate limit exceeded.", str(caught.exception))
        (self.site / "broken").write_text("<html>")
        with self.assertRaises(UpdateError) as caught:
            check(UBUNTU, url=f"{self.base}/broken", downloader=self.downloader)
        self.assertIn("could not be read", str(caught.exception))

    def test_the_package_is_downloaded_and_checked(self) -> None:
        found = self.publish()
        seen: list[tuple[int, int | None]] = []
        path = download(
            found,
            lambda done, total: seen.append((done, total)),
            folder=self.cache,
            downloader=self.downloader,
        )
        self.assertEqual(path, self.cache / PACKAGE)
        self.assertEqual(path.read_bytes(), b"a package")
        self.assertTrue(seen)

    def test_a_package_that_does_not_match_its_checksum_is_removed(self) -> None:
        found = self.publish(sums=f"{'0' * 64}  {PACKAGE}\n".encode())
        with self.assertRaises(UpdateError) as caught:
            download(found, folder=self.cache, downloader=self.downloader)
        self.assertIn("does not match its published checksum", str(caught.exception))
        self.assertEqual(list(self.cache.iterdir()), [])

    def test_a_package_the_checksums_do_not_list_is_not_downloaded(self) -> None:
        found = self.publish(sums=f"{'0' * 64}  other.deb\n".encode())
        with self.assertRaises(UpdateError) as caught:
            download(found, folder=self.cache, downloader=self.downloader)
        self.assertIn("has no line for the package", str(caught.exception))
        self.assertFalse((self.cache / PACKAGE).exists())

    def test_a_cancelled_download_says_so(self) -> None:
        found = self.publish()
        cancel = Cancellation()
        cancel.cancel()
        with self.assertRaises(UpdateCancelled):
            download(found, cancel=cancel, folder=self.cache, downloader=self.downloader)

    def test_a_release_without_this_systems_package_is_not_downloaded(self) -> None:
        with self.assertRaises(UpdateError):
            download(AppRelease("9.0.0", "v9.0.0", "PirateFinder 9.0.0", "http://x"))


class InstallTests(unittest.TestCase):
    def setUp(self) -> None:
        self.folder = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.folder)
        self.package = self.folder / PACKAGE
        self.package.write_bytes(b"a package")
        which = {"pkexec": "/usr/bin/pkexec", "apt-get": "/usr/bin/apt-get"}
        patcher = mock.patch.object(app_update.shutil, "which", side_effect=which.get)
        patcher.start()
        self.addCleanup(patcher.stop)

    def run_with(self, returncode: int, stderr: str = ""):
        return mock.Mock(
            return_value=subprocess.CompletedProcess([], returncode, stdout="", stderr=stderr)
        )

    def test_apt_installs_the_package_with_the_users_password(self) -> None:
        run = self.run_with(0)
        install(self.package, run=run)
        command = run.call_args.args[0]
        self.assertEqual(
            command, ["/usr/bin/pkexec", "/usr/bin/apt-get", "install", "--yes", str(self.package)]
        )
        self.assertFalse(self.package.exists())

    def test_the_install_waits_for_the_password_prompt_and_apt_however_long(self) -> None:
        # pkexec and apt run as root and cannot be stopped from here, so a time
        # limit would report a failure while apt went on to install.
        run = self.run_with(0)
        install(self.package, run=run)
        self.assertNotIn("timeout", run.call_args.kwargs)

    def test_a_dismissed_password_prompt_installs_nothing(self) -> None:
        with self.assertRaises(UpdateCancelled):
            install(self.package, run=self.run_with(126))
        self.assertTrue(self.package.exists())

    def test_a_refusal_or_an_apt_failure_says_what_to_run_by_hand(self) -> None:
        with self.assertRaises(UpdateError) as caught:
            install(self.package, run=self.run_with(127))
        self.assertIn("did not allow", str(caught.exception))
        self.assertIn(f"sudo apt install {self.package}", str(caught.exception))
        with self.assertRaises(UpdateError) as caught:
            install(self.package, run=self.run_with(100, "E: Unable to locate package\n"))
        self.assertIn("E: Unable to locate package", str(caught.exception))
        self.assertTrue(self.package.exists())

    def test_without_pkexec_the_command_is_given_instead(self) -> None:
        with (
            mock.patch.object(app_update.shutil, "which", return_value=None),
            self.assertRaises(UpdateError) as caught,
        ):
            install(self.package, run=self.run_with(0))
        self.assertIn("pkexec is not installed", str(caught.exception))
        self.assertIn("sudo apt install", str(caught.exception))


if __name__ == "__main__":
    unittest.main()

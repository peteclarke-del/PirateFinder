"""IPF support: the SPS Decoder Library, its install, status and the gw environment.

Nothing here touches the network. The download comes from a local HTTP
server serving a tar.xz built in the test around a fake library: the start
of this Python's own ELF header, marked as a shared object, and padding.
It passes the ELF check but cannot be loaded, so the separate load check is
replaced in the install tests and tested on its own with a library compiled
here when a C compiler is available.
"""

from __future__ import annotations

import contextlib
import dataclasses
import functools
import hashlib
import http.server
import io
import json
import os
import shutil
import stat
import struct
import subprocess
import sys
import tarfile
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from piratefinder.greaseweazle import caps, client
from piratefinder.greaseweazle.caps import CapsBuild, CapsError, CapsState, CapsStatus
from piratefinder.greaseweazle.runner import ProcessResult
from piratefinder.models import Geometry, Platform, PreparedImage
from piratefinder.online.http import DownloadCancelled, Downloader, HostThrottle
from tests.test_greaseweazle_runner import fake_gw

MEMBER = "CAPSImg/Linux/x86-64/capsimg.so"


def fake_library() -> bytes:
    """An ELF shared object header for this Python's processor, and padding."""
    with open(os.path.realpath(sys.executable), "rb") as handle:
        header = bytearray(handle.read(64))
    order = "<" if header[5] == 1 else ">"
    struct.pack_into(order + "H", header, 16, 3)  # ET_DYN
    return bytes(header) + b"fake libcapsimage " * 64


def tar_xz(members: list[tuple[str, bytes | None, str]]) -> bytes:
    """A tar.xz holding (name, data, symlink target) members; data None for a symlink."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:xz") as bundle:
        for name, data, target in members:
            info = tarfile.TarInfo(name)
            if data is None:
                info.type = tarfile.SYMTYPE
                info.linkname = target
                bundle.addfile(info)
            else:
                info.size = len(data)
                info.mode = 0o755
                bundle.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


class _QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        return None


@contextlib.contextmanager
def serve(folder: Path):
    """Serve ``folder`` on a local port; yield the base URL."""
    handler = functools.partial(_QuietHandler, directory=str(folder))
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, args=(0.05,), daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


class CapsTestCase(unittest.TestCase):
    """A data folder of its own, no library on the system, and a download server."""

    def setUp(self) -> None:
        self._folder = tempfile.TemporaryDirectory()
        self.root = Path(self._folder.name)
        self.web = self.root / "web"
        self.web.mkdir()
        environment = mock.patch.dict(
            os.environ, {"XDG_DATA_HOME": str(self.root / "data")}, clear=False
        )
        environment.start()
        self.addCleanup(environment.stop)
        os.environ.pop("LD_LIBRARY_PATH", None)
        # Whatever this computer has installed stays out of the tests.
        self.no_system = mock.patch.object(caps, "system_library", return_value=None)
        self.no_system.start()
        self.addCleanup(self.no_system.stop)
        server = serve(self.web)
        self.base = server.__enter__()
        self.addCleanup(server.__exit__, None, None, None)
        self.library = fake_library()

    def tearDown(self) -> None:
        self._folder.cleanup()

    def publish(self, members=None, name: str = "CAPSImg.tar.xz", **changes) -> CapsBuild:
        """Put an archive on the server and return a build that pins it."""
        members = members if members is not None else [(MEMBER, self.library, "")]
        archive = tar_xz(members)
        (self.web / name).write_bytes(archive)
        values = {
            "version": "5.1.3",
            "machines": (caps.machine(),),
            "description": "this computer",
            "url": f"{self.base}/{name}",
            "size": len(archive),
            "sha256": hashlib.sha256(archive).hexdigest(),
            "member": MEMBER,
            "library_size": len(self.library),
            "library_sha256": hashlib.sha256(self.library).hexdigest(),
        }
        values.update(changes)
        return CapsBuild(**values)

    def install(self, build: CapsBuild, **options) -> CapsStatus:
        downloader = Downloader(throttle=HostThrottle(0), retries=0)
        with mock.patch.object(caps, "load_check", return_value="5.1") as check:
            status = caps.install(build, downloader=downloader, **options)
        check.assert_called_once()
        return status

    def target(self) -> Path:
        return caps.folder() / "libcapsimage.so.5"


class InstallTests(CapsTestCase):
    def test_install_puts_the_library_in_place(self) -> None:
        status = self.install(self.publish())
        self.assertIs(status.state, CapsState.INSTALLED)
        self.assertEqual(status.version, "5.1.3")
        self.assertEqual(Path(status.path), self.target())
        self.assertEqual(self.target().read_bytes(), self.library)
        self.assertEqual(stat.S_IMODE(self.target().stat().st_mode), 0o644)
        self.assertEqual(self.target().parent, self.root / "data" / "piratefinder" / "caps")
        self.assertEqual(
            sorted(path.name for path in caps.folder().iterdir()),
            ["install.json", "libcapsimage.so.5"],
            "nothing is left behind from the download",
        )

    def test_progress_is_reported(self) -> None:
        build = self.publish()
        seen: list[tuple[int, int | None]] = []
        self.install(build, progress=lambda done, total: seen.append((done, total)))
        self.assertEqual(seen[-1], (build.size, build.size))

    def test_a_wrong_checksum_is_refused_before_anything_is_read(self) -> None:
        for change in ({"sha256": "0" * 64}, {"size": 12}):
            with self.subTest(change=change):
                build = self.publish(**change)
                with (
                    mock.patch.object(caps, "read_library") as read,
                    self.assertRaisesRegex(CapsError, "size or checksum is different"),
                ):
                    self.install(build)
                read.assert_not_called()
                self.assertIsNone(caps.installed_library())
                self.assertEqual(list(caps.folder().iterdir()), [])

    def test_a_library_that_is_not_the_pinned_one_is_refused(self) -> None:
        build = self.publish(library_sha256="0" * 64)
        with self.assertRaisesRegex(CapsError, "not the expected file"):
            self.install(build)
        self.assertIsNone(caps.installed_library())

    def test_only_the_pinned_member_is_read(self) -> None:
        members = [
            ("../../escape.so", b"outside", ""),
            ("/tmp/absolute-escape.so", b"outside", ""),
            (MEMBER, self.library, ""),
            ("CAPSImg/Licenses/CAPSImg.txt", b"licence", ""),
        ]
        self.install(self.publish(members))
        self.assertEqual(self.target().read_bytes(), self.library)
        self.assertFalse((self.root / "escape.so").exists())
        self.assertFalse((caps.folder().parent / "escape.so").exists())
        self.assertFalse(Path("/tmp/absolute-escape.so").exists())
        self.assertFalse((caps.folder() / "CAPSImg").exists())

    def test_a_link_in_place_of_the_library_is_refused(self) -> None:
        build = self.publish([(MEMBER, None, "/etc/passwd")])
        with self.assertRaisesRegex(CapsError, "not the expected file"):
            self.install(build)
        self.assertIsNone(caps.installed_library())

    def test_an_archive_without_the_library_is_refused(self) -> None:
        build = self.publish([("CAPSImg/ReadMe.txt", b"read me", "")])
        with self.assertRaisesRegex(CapsError, "does not hold the library"):
            self.install(build)

    def test_a_library_for_another_processor_is_refused(self) -> None:
        other = bytearray(self.library)
        other[18:20] = (183 if other[18] != 183 else 62).to_bytes(2, "little")
        self.library = bytes(other)
        with self.assertRaisesRegex(CapsError, "different processor"):
            self.install(self.publish())
        self.assertIsNone(caps.installed_library())

    def test_a_library_that_does_not_load_is_not_installed(self) -> None:
        build = self.publish()
        downloader = Downloader(throttle=HostThrottle(0), retries=0)
        refusal = CapsError("The downloaded library does not load on this computer.")
        with (
            mock.patch.object(caps, "load_check", side_effect=refusal),
            self.assertRaisesRegex(CapsError, "does not load"),
        ):
            caps.install(build, downloader=downloader)
        self.assertIsNone(caps.installed_library())
        self.assertEqual(list(caps.folder().iterdir()), [])

    def test_a_failed_download_says_why(self) -> None:
        build = self.publish()
        missing = dataclasses.replace(build, url=f"{self.base}/gone.tar.xz")
        with self.assertRaisesRegex(CapsError, r"could not be downloaded\. .*HTTP 404"):
            self.install(missing)

    def test_cancelling_installs_nothing(self) -> None:
        build = self.publish()

        class Cancel:
            cancelled = True

        with self.assertRaises(DownloadCancelled):
            caps.install(build, downloader=Downloader(throttle=HostThrottle(0)), cancel=Cancel())
        self.assertIsNone(caps.installed_library())

    def test_an_unsupported_processor_is_refused_with_a_sentence(self) -> None:
        with mock.patch.object(caps, "machine", return_value="aarch64"):
            self.assertIsNone(caps.build_for_machine())
            with self.assertRaises(CapsError) as caught:
                caps.install(downloader=mock.Mock())
            status = caps.status()
        self.assertEqual(
            str(caught.exception),
            "IPF support cannot be installed on this computer: there is no build of the SPS "
            "Decoder Library for its processor (aarch64). Builds exist for x86_64, armv7l "
            "and armv8l.",
        )
        self.assertIs(status.state, CapsState.UNSUPPORTED)
        self.assertIn("no build of it for this computer's processor (aarch64)", status.problem)

    def test_a_folder_the_library_path_cannot_hold_is_refused(self) -> None:
        with (
            mock.patch.dict(os.environ, {"XDG_DATA_HOME": str(self.root / "a:b")}),
            self.assertRaisesRegex(CapsError, "colon or a semicolon"),
        ):
            caps.install(self.publish(), downloader=mock.Mock())

    def test_remove(self) -> None:
        self.install(self.publish())
        status = caps.remove()
        self.assertIs(status.state, CapsState.MISSING)
        self.assertFalse(caps.folder().exists())
        self.assertIs(caps.remove().state, CapsState.MISSING, "removing twice is harmless")


class StatusTests(CapsTestCase):
    def test_missing(self) -> None:
        status = caps.status()
        self.assertIs(status.state, CapsState.MISSING)
        self.assertFalse(status.usable)
        self.assertEqual(status.build, caps.build_for_machine())
        self.assertEqual(
            status.problem,
            "Writing an IPF image needs the SPS Decoder Library (libcapsimage), which is not "
            "installed. Install it with IPF Support in Preferences, on the Greaseweazle page, "
            "or choose another dump of this disk.",
        )

    def test_installed(self) -> None:
        self.install(self.publish())
        status = caps.status()
        self.assertIs(status.state, CapsState.INSTALLED)
        self.assertTrue(status.usable)
        self.assertEqual(status.problem, "")
        self.assertEqual((status.version, status.path), ("5.1.3", str(self.target())))

    def test_found_on_the_system(self) -> None:
        system = self.root / "system"
        system.mkdir()
        (system / "libcapsimage.so.4").write_bytes(self.library)
        self.no_system.stop()
        with (
            mock.patch.dict(os.environ, {"LD_LIBRARY_PATH": f"relative:/nowhere:{system}"}),
            mock.patch("ctypes.util.find_library", return_value=None),
            mock.patch.object(caps, "DEFAULT_FOLDERS", ()),
        ):
            status = caps.status()
        self.assertIs(status.state, CapsState.SYSTEM)
        self.assertEqual(status.path, str(system / "libcapsimage.so.4"))
        self.assertTrue(status.usable)
        self.assertEqual(status.problem, "")

    def test_a_library_the_loader_knows_is_found_by_name(self) -> None:
        self.no_system.stop()
        with mock.patch("ctypes.util.find_library", return_value="libcapsimage.so.5"):
            status = caps.status()
        self.assertEqual((status.state, status.path), (CapsState.SYSTEM, "libcapsimage.so.5"))

    def test_pirate_finders_own_folder_is_not_the_system(self) -> None:
        self.install(self.publish())
        self.no_system.stop()
        with (
            mock.patch.dict(os.environ, {"LD_LIBRARY_PATH": str(caps.folder())}),
            mock.patch("ctypes.util.find_library", return_value=None),
            mock.patch.object(caps, "DEFAULT_FOLDERS", ()),
        ):
            self.assertIsNone(caps.system_library())

    def test_the_installed_copy_comes_first(self) -> None:
        """gw finds PirateFinder's folder before the system's, so status names that copy."""
        self.install(self.publish())
        with mock.patch.object(caps, "system_library", return_value="libcapsimage.so.5"):
            status = caps.status()
        self.assertEqual((status.state, status.path), (CapsState.INSTALLED, str(self.target())))

    def test_machine_names_follow_the_running_python(self) -> None:
        for kernel, pointer, expected in (
            ("x86_64", 8, "x86_64"),
            ("x86_64", 4, "i686"),
            ("aarch64", 8, "aarch64"),
            ("aarch64", 4, "armv8l"),
            ("armv7l", 4, "armv7l"),
        ):
            with (
                self.subTest(kernel=kernel, pointer=pointer),
                mock.patch.object(caps.platform, "machine", return_value=kernel),
                mock.patch.object(caps.struct, "calcsize", return_value=pointer),
            ):
                self.assertEqual(caps.machine(), expected)
        self.assertEqual(caps.build_for_machine("x86_64").member, MEMBER)
        self.assertEqual(caps.build_for_machine("armv7l"), caps.build_for_machine("armv8l"))
        self.assertIn("ARM", caps.build_for_machine("armv8l").description)
        for name in ("aarch64", "i686", "armv6l", "riscv64"):
            self.assertIsNone(caps.build_for_machine(name), name)


class PinnedBuildTests(unittest.TestCase):
    def test_the_pins_are_complete(self) -> None:
        builds = caps.builds()
        self.assertEqual(len(builds), 2)
        for build in builds:
            with self.subTest(build=build.description):
                self.assertTrue(build.url.startswith("https://fs-uae.net/files/CAPSImg/Stable/"))
                self.assertEqual(build.host, "fs-uae.net")
                self.assertRegex(build.sha256, "^[0-9a-f]{64}$")
                self.assertRegex(build.library_sha256, "^[0-9a-f]{64}$")
                self.assertTrue(build.member.endswith("/capsimg.so"))
                self.assertIn(build.version, build.url)

    def test_the_licence_is_shipped_for_display(self) -> None:
        text = caps.licence_text()
        self.assertTrue(text.startswith("This licence is based on the MAME licence"))
        self.assertIn("Redistributions may not be sold", text)


class ElfTests(unittest.TestCase):
    def header(self, elf_class: int, machine: int, flags: int = 0, kind: int = 3) -> bytes:
        data = bytearray(64)
        data[:6] = b"\x7fELF" + bytes([elf_class, 1])
        struct.pack_into("<HH", data, 16, kind, machine)
        struct.pack_into("<I", data, 36 if elf_class == 1 else 48, flags)
        return bytes(data)

    def test_a_library_must_match_the_reference(self) -> None:
        x86_64 = self.header(2, 62, kind=2)
        arm_hard = self.header(1, 40, 0x5000400, kind=2)
        self.assertEqual(caps.elf_problem(self.header(2, 62), x86_64), "")
        self.assertEqual(caps.elf_problem(self.header(1, 40, 0x5000400), arm_hard), "")
        self.assertIn("different processor", caps.elf_problem(self.header(1, 40), x86_64))
        self.assertIn("different processor", caps.elf_problem(self.header(2, 183), x86_64))
        soft = self.header(1, 40, 0x5000200)
        self.assertIn("different processor", caps.elf_problem(soft, arm_hard))

    def test_only_a_shared_object_will_do(self) -> None:
        reference = self.header(2, 62, kind=2)
        for data in (b"not an elf file" * 10, self.header(2, 62, kind=2), b"\x7fELF"):
            self.assertEqual(
                caps.elf_problem(data, reference),
                "The downloaded file is not a Linux shared library.",
            )

    def test_the_default_reference_is_this_python(self) -> None:
        self.assertEqual(caps.elf_problem(fake_library()), "")


class LoadCheckTests(unittest.TestCase):
    def setUp(self) -> None:
        self._folder = tempfile.TemporaryDirectory()
        self.folder = Path(self._folder.name)

    def tearDown(self) -> None:
        self._folder.cleanup()

    def test_a_file_that_is_not_a_library_does_not_load(self) -> None:
        path = self.folder / "libcapsimage.so.5"
        path.write_bytes(fake_library())
        with self.assertRaisesRegex(CapsError, "does not load on this computer"):
            caps.load_check(path)

    def compile(self, init_result: int) -> Path:
        source = self.folder / "caps.c"
        source.write_text(
            "struct V { unsigned type, release, revision, flag; };\n"
            f"int CAPSInit(void) {{ return {init_result}; }}\n"
            "int CAPSExit(void) { return 0; }\n"
            "int CAPSGetVersionInfo(struct V *v, unsigned f) "
            "{ v->release = 5; v->revision = 1; return 0; }\n"
        )
        path = self.folder / f"libcapsimage-{init_result}.so.5"
        subprocess.run(
            [shutil.which("cc") or "cc", "-shared", "-fPIC", "-o", str(path), str(source)],
            check=True,
            capture_output=True,
        )
        return path

    @unittest.skipUnless(shutil.which("cc"), "needs a C compiler")
    def test_a_library_that_starts_gives_its_version(self) -> None:
        self.assertEqual(caps.load_check(self.compile(0)), "5.1")

    @unittest.skipUnless(shutil.which("cc"), "needs a C compiler")
    def test_a_library_that_fails_to_start_is_refused(self) -> None:
        with self.assertRaisesRegex(CapsError, r"\(CAPSInit failed\)"):
            caps.load_check(self.compile(-1))


class GwEnvironmentTests(CapsTestCase):
    def test_nothing_is_added_without_the_library(self) -> None:
        self.assertNotIn("LD_LIBRARY_PATH", client.gw_environment())
        with mock.patch.dict(os.environ, {"LD_LIBRARY_PATH": "/opt/caps"}):
            self.assertEqual(client.gw_environment()["LD_LIBRARY_PATH"], "/opt/caps")

    def test_the_installed_library_folder_comes_first(self) -> None:
        self.install(self.publish())
        own = str(caps.folder())
        self.assertEqual(client.gw_environment()["LD_LIBRARY_PATH"], own)
        with mock.patch.dict(os.environ, {"LD_LIBRARY_PATH": f"/opt/caps:{own}"}):
            self.assertEqual(client.gw_environment()["LD_LIBRARY_PATH"], f"{own}:/opt/caps")
        environment = client.gw_environment()
        self.assertEqual(environment["PYTHONUNBUFFERED"], "1", "built on the minimal one")

    def test_probe_and_write_run_gw_with_it(self) -> None:
        self.install(self.publish())
        image = self.root / "Menu.st"
        image.write_bytes(bytes(819_200))
        prepared = PreparedImage("Menu", Platform.ATARI_ST, str(image), Geometry(80, 2, 10))
        result = ProcessResult(0, "All tracks verified", False, False)
        with mock.patch.object(client, "run_streaming", return_value=result) as run:
            client.probe(executable="gw")
            client.write(prepared, drive="A", executable="gw")
        self.assertEqual(run.call_count, 2)
        for call in run.call_args_list:
            environment = call.kwargs["environment"]
            self.assertEqual(environment["LD_LIBRARY_PATH"].split(":")[0], str(caps.folder()))

    def test_the_gw_child_process_gets_it(self) -> None:
        self.install(self.publish())
        command = fake_gw(self.root, ["Host Tools: 1.23"])
        client.probe(executable=command)
        recorded = json.loads((self.root / "env.json").read_text())
        self.assertEqual(recorded["LD_LIBRARY_PATH"], str(caps.folder()))


if __name__ == "__main__":
    unittest.main()

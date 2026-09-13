"""The gw client: command lines, progress and outcome rules, with a fake gw.

The canned output uses the message strings of Greaseweazle 1.23: the Ack
strings in ``usb.py``, the verification lines in ``tools/write.py`` and the
fatal error banner in ``cli.py``.
"""

from __future__ import annotations

import json
import socket
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from piratefinder.greaseweazle import client
from piratefinder.greaseweazle.runner import OperationController, ProcessResult
from piratefinder.models import Geometry, Platform, PreparedImage, WriteProgress, WriteStatus
from tests.test_greaseweazle_runner import fake_gw


def track_lines(cylinders: int, heads: int) -> list[str]:
    return [
        f"T{c}.{h}: Writing Track (MFM, 6250 bytes)" for c in range(cylinders) for h in range(heads)
    ]


class ClientTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._folder = tempfile.TemporaryDirectory()
        self.folder = Path(self._folder.name)
        self.image = self.folder / "Menu 12.st"
        self.image.write_bytes(bytes(819_200))
        self.prepared = PreparedImage(
            "Menu 12", Platform.ATARI_ST, str(self.image), Geometry(80, 2, 10), "atarist.800"
        )

    def tearDown(self) -> None:
        self._folder.cleanup()

    def write(self, lines: list[str], exit_code: int = 0, prepared=None, **options):
        command = fake_gw(self.folder, lines, exit_code=exit_code)
        progress: list[WriteProgress] = []
        outcome = client.write(
            prepared or self.prepared,
            drive=options.pop("drive", "A"),
            progress=progress.append,
            executable=command,
            **options,
        )
        return outcome, progress

    def argv(self) -> list[str]:
        return json.loads((self.folder / "argv.json").read_text())


class CommandTests(ClientTestCase):
    def test_command_line(self) -> None:
        cfg = self.folder / "st_82_2_10.cfg"
        cfg.write_text("disk st_82_2_10\nend\n")
        prepared = PreparedImage(
            "Menu",
            Platform.ATARI_ST,
            str(self.image),
            Geometry(82, 2, 10),
            "st_82_2_10",
            diskdefs_path=str(cfg),
        )
        self.write(
            ["All tracks verified"],
            prepared=prepared,
            drive="b",
            device="/dev/ttyACM0",
            retries=5,
            pre_erase=True,
        )
        self.assertEqual(
            self.argv(),
            [
                "write",
                "--drive=B",
                "--device=/dev/ttyACM0",
                f"--diskdefs={cfg}",
                "--format=st_82_2_10",
                "--retries=5",
                "--pre-erase",
                str(self.image),
            ],
        )

    def test_optional_arguments_are_left_out(self) -> None:
        prepared = PreparedImage("Flux", Platform.AMIGA, str(self.image), None, verifiable=False)
        self.write(["No tracks verified (Reason: Verify unavailable)"], prepared=prepared)
        self.assertEqual(self.argv(), ["write", "--drive=A", "--retries=3", str(self.image)])

    def test_explicit_tracks(self) -> None:
        prepared = PreparedImage(
            "Menu", Platform.ATARI_ST, str(self.image), None, "atarist.800", tracks="c=0-81"
        )
        self.write(["All tracks verified"], prepared=prepared)
        self.assertIn("--tracks=c=0-81", self.argv())

    def test_bad_drive_is_refused_without_running_gw(self) -> None:
        outcome, _ = self.write(["All tracks verified"], drive="C; rm -rf /")
        self.assertEqual(outcome.status, WriteStatus.FAILED)
        self.assertFalse((self.folder / "argv.json").exists())

    def test_a_path_with_double_colons_is_refused(self) -> None:
        # gw splits a file argument at "::" to read file options.
        odd = self.folder / "a::b.st"
        odd.write_bytes(bytes(819_200))
        prepared = PreparedImage("x", Platform.ATARI_ST, str(odd), None, "atarist.800")
        outcome, _ = self.write(["All tracks verified"], prepared=prepared)
        self.assertEqual(outcome.status, WriteStatus.FAILED)
        self.assertIn("::", outcome.summary)
        self.assertFalse((self.folder / "argv.json").exists())

    def test_missing_image(self) -> None:
        self.image.unlink()
        outcome, _ = self.write(["All tracks verified"])
        self.assertEqual(outcome.status, WriteStatus.FAILED)
        self.assertIn("missing", outcome.summary)

    def test_gw_not_installed(self) -> None:
        with mock.patch.object(client, "find_gw", return_value=None):
            outcome = client.write(self.prepared, drive="A")
        self.assertEqual(outcome.status, WriteStatus.FAILED)
        self.assertIn("not installed", outcome.summary)


class OutcomeTests(ClientTestCase):
    def test_all_tracks_verified(self) -> None:
        lines = ["Format atarist.800", "Writing c=0-79:h=0-1", *track_lines(80, 2)]
        outcome, progress = self.write([*lines, "All tracks verified"])
        self.assertEqual(outcome.status, WriteStatus.VERIFIED)
        self.assertEqual(outcome.summary, "Written and verified.")
        self.assertTrue(outcome.succeeded)
        self.assertEqual(outcome.retries, 0)
        self.assertEqual(len(progress), 161)
        self.assertEqual(progress[0].track_count, 160)
        self.assertEqual((progress[5].cylinder, progress[5].head), (2, 1))
        self.assertEqual(progress[5].track_number, 6)
        fractions = [item.fraction for item in progress]
        self.assertEqual(fractions, sorted(fractions))
        self.assertEqual(progress[-1].fraction, 1.0)
        self.assertIn("All tracks verified", outcome.diagnostic)
        self.assertTrue(outcome.diagnostic.startswith("$ "))

    def test_retries_are_counted(self) -> None:
        lines = [
            "Writing c=0-1:h=0-1",
            "T0.0: Writing Track (MFM)",
            "T0.1: Writing Track (MFM)",
            "T0.1: Writing Track (Verify Failure: Retry #1)",
            "T0.1: Writing Track (Verify Failure: Retry #2)",
            "T1.0: Writing Track (MFM)",
            "T1.1: Writing Track (MFM)",
            "All tracks verified",
        ]
        outcome, progress = self.write(lines)
        self.assertEqual(outcome.status, WriteStatus.VERIFIED)
        self.assertEqual(outcome.retries, 2)
        self.assertEqual(outcome.summary, "Written and verified after 2 retries.")
        retry = [item for item in progress if item.retry]
        self.assertEqual([item.retry for item in retry], [1, 2])
        self.assertEqual(retry[0].track_number, 2)
        self.assertEqual(progress[-1].track_count, 4)

    def test_total_falls_back_to_the_prepared_geometry(self) -> None:
        _outcome, progress = self.write([*track_lines(2, 2), "All tracks verified"])
        self.assertEqual(progress[0].track_count, 160)

    def test_flux_images_are_written_without_verification(self) -> None:
        outcome, _ = self.write(
            ["Writing c=0-81:h=0-1", "No tracks verified (Reason: Verify unavailable)"]
        )
        self.assertEqual(outcome.status, WriteStatus.WRITTEN)
        self.assertTrue(outcome.succeeded)
        self.assertIn("cannot verify", outcome.summary)

    def test_partly_verified(self) -> None:
        line = "150 tracks verified; 10 tracks *not* verified (Reason: Verify unavailable)"
        outcome, _ = self.write([line])
        self.assertEqual(outcome.status, WriteStatus.WRITTEN)
        self.assertIn("150 tracks were verified", outcome.summary)

    def test_verification_disabled(self) -> None:
        outcome, _ = self.write(["No tracks verified (Reason: Verify disabled)"])
        self.assertEqual(outcome.status, WriteStatus.WRITTEN)
        self.assertIn("switched off", outcome.summary)

    def test_write_protected_with_exit_status_zero(self) -> None:
        # gw prints Command Failed and exits 0 for hardware command errors.
        lines = ["Writing c=0-79:h=0-1", "Command Failed: WriteFlux: Disk is Write Protected"]
        outcome, _ = self.write(lines, exit_code=0)
        self.assertEqual(outcome.status, WriteStatus.WRITE_PROTECTED)
        self.assertFalse(outcome.succeeded)
        self.assertIn("write-protected", outcome.summary)

    def test_no_disk(self) -> None:
        for line in (
            "Command Failed: ReadFlux: No Index",
            "Command Failed: Seek: Track 0 not found",
        ):
            with self.subTest(line=line):
                outcome, _ = self.write(["Writing c=0-79:h=0-1", line], exit_code=0)
                self.assertEqual(outcome.status, WriteStatus.NO_DISK)
                self.assertIn("No disk", outcome.summary)

    def test_other_command_errors_fail_even_with_exit_status_zero(self) -> None:
        outcome, _ = self.write(["Command Failed: WriteFlux: Flux Underflow"], exit_code=0)
        self.assertEqual(outcome.status, WriteStatus.FAILED)
        self.assertIn("Flux Underflow", outcome.summary)

    def test_failed_verify_names_the_track(self) -> None:
        lines = [
            "Writing c=0-79:h=0-1",
            *track_lines(5, 2),
            "T5.0: Writing Track (MFM)",
            "T5.0: Writing Track (Verify Failure: Retry #1)",
            "T5.0: Writing Track (Verify Failure: Retry #2)",
            "T5.0: Writing Track (Verify Failure: Retry #3)",
            "** FATAL ERROR:",
            "Failed to verify Track 5.0",
        ]
        outcome, _ = self.write(lines, exit_code=1, retries=3)
        self.assertEqual(outcome.status, WriteStatus.FAILED)
        self.assertEqual(outcome.failed_tracks, ("5.0",))
        self.assertEqual(outcome.retries, 3)
        self.assertIn("Track 5.0 did not verify after 4 attempts", outcome.summary)

    def test_fatal_error_message_is_shown(self) -> None:
        lines = ["** FATAL ERROR:", "Sector image requires a disk format to be specified"]
        outcome, _ = self.write(lines, exit_code=1)
        self.assertEqual(outcome.status, WriteStatus.FAILED)
        self.assertEqual(
            outcome.summary,
            "Greaseweazle stopped with an error: Sector image requires a disk format to be "
            "specified.",
        )

    def test_device_not_found(self) -> None:
        outcome, _ = self.write(["** FATAL ERROR:", "Cannot find the Greaseweazle device"], 1)
        self.assertEqual(outcome.status, WriteStatus.FAILED)
        self.assertIn("No Greaseweazle was found", outcome.summary)

    def test_nonzero_exit_without_a_message(self) -> None:
        outcome, _ = self.write([], exit_code=2)
        self.assertEqual(outcome.status, WriteStatus.FAILED)

    def test_success_is_never_assumed(self) -> None:
        outcome, _ = self.write(["Writing c=0-79:h=0-1", *track_lines(2, 2)], exit_code=0)
        self.assertEqual(outcome.status, WriteStatus.FAILED)
        self.assertIn("without confirming", outcome.summary)

    def test_a_file_name_mentioning_protection_is_not_an_error(self) -> None:
        lines = ["Format atarist.800", "Opening Write Protected Menu.st", "All tracks verified"]
        outcome, _ = self.write(lines)
        self.assertEqual(outcome.status, WriteStatus.VERIFIED)

    def test_cancel(self) -> None:
        command = fake_gw(self.folder, ["Writing c=0-79:h=0-1", "T0.0: Writing Track"], hang=30)
        controller = OperationController()

        def progress(item: WriteProgress) -> None:
            threading.Thread(target=controller.cancel).start()

        outcome = client.write(
            self.prepared, drive="A", progress=progress, controller=controller, executable=command
        )
        self.assertEqual(outcome.status, WriteStatus.CANCELLED)
        self.assertIn("cancelled", outcome.summary)

    def test_timeout(self) -> None:
        command = fake_gw(self.folder, ["Writing c=0-79:h=0-1"], hang=30)
        outcome = client.write(self.prepared, drive="A", timeout=0.5, executable=command)
        self.assertEqual(outcome.status, WriteStatus.FAILED)
        self.assertIn("did not finish within 1 second and", outcome.summary)


class ProbeTests(unittest.TestCase):
    def result(self, text: str, code: int = 0) -> ProcessResult:
        return ProcessResult(code, text, False, False)

    def test_not_found_exits_zero_but_is_not_connected(self) -> None:
        status = client.parse_info(self.result("Host Tools: 1.23\nDevice:\n  Not found"))
        self.assertFalse(status.connected)
        self.assertEqual(status.host_tools, "1.23")
        self.assertEqual(status.message, "No Greaseweazle is connected.")

    def test_connected(self) -> None:
        text = (
            "Host Tools: 1.23\n"
            "Device:\n"
            "  Port:       /dev/ttyACM0\n"
            "  Model:      Greaseweazle V4.1\n"
            "  MCU:        AT32F403A, 216MHz, 224kB SRAM\n"
            "  Firmware:   1.6\n"
            "  Serial:     GW0000000000000000001\n"
            "  USB:        Full Speed (12 Mbit/s), 128kB Buffer\n"
        )
        status = client.parse_info(self.result(text))
        self.assertTrue(status.connected)
        self.assertEqual(status.model, "Greaseweazle V4.1")
        self.assertEqual(status.firmware, "1.6")
        self.assertEqual(status.port, "/dev/ttyACM0")
        self.assertEqual(status.host_tools, "1.23")
        self.assertEqual(status.message, "Greaseweazle V4.1 connected on /dev/ttyACM0.")

    def test_failed_online_firmware_check_does_not_hide_the_device(self) -> None:
        text = (
            "Host Tools: 1.23\nDevice:\n  Port:       /dev/ttyACM0\n"
            "  Model:      Greaseweazle V4\n  Firmware:   1.5\n"
            "** FATAL ERROR:\nHTTPSConnectionPool(host='api.github.com', port=443)\n"
        )
        status = client.parse_info(self.result(text, 1))
        self.assertTrue(status.connected)

    def test_newer_firmware_is_mentioned(self) -> None:
        text = (
            "Host Tools: 1.23\nDevice:\n  Model:      Greaseweazle F7\n  Firmware:   1.2\n\n"
            "*** New firmware version 1.6 is available\nTo perform an Update:\n"
        )
        status = client.parse_info(self.result(text))
        self.assertIn("Firmware 1.6 is available.", status.message)

    def test_tool_failure(self) -> None:
        status = client.parse_info(self.result("Traceback\nImportError: serial", 1))
        self.assertFalse(status.connected)
        self.assertIn("ImportError", status.message)

    def test_probe_runs_gw_info(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            command = fake_gw(Path(folder), ["Host Tools: 1.23", "Device:", "  Not found"])
            status = client.probe(executable=command)
            arguments = json.loads((Path(folder) / "argv.json").read_text())
        self.assertEqual(arguments, ["info"])
        self.assertFalse(status.connected)
        self.assertEqual(status.host_tools, "1.23")

    def test_probe_passes_the_chosen_device(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            command = fake_gw(Path(folder), ["Host Tools: 1.23", "Device:", "  Not found"])
            client.probe(5, device="/dev/ttyACM1", executable=command)
            arguments = json.loads((Path(folder) / "argv.json").read_text())
        self.assertEqual(arguments, ["info", "--device=/dev/ttyACM1"])

    def test_probe_refuses_a_device_name_with_a_newline(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            command = fake_gw(Path(folder), [])
            status = client.probe(device="/dev/tty\n--x", executable=command)
            self.assertFalse((Path(folder) / "argv.json").exists())
        self.assertFalse(status.connected)

    def test_probe_with_online_use_off_keeps_the_firmware_lookup_on_this_computer(self) -> None:
        # Like gw info: print the device, then look up the newest firmware over HTTPS,
        # which honours the proxy variables, and end with a fatal error when that fails.
        with tempfile.TemporaryDirectory() as folder:
            script = Path(folder) / "gw"
            script.write_text(
                f"#!{sys.executable}\n"
                "import json, os, sys, urllib.request\n"
                "print('Host Tools: 1.23\\nDevice:\\n  Model:      Greaseweazle V4\\n"
                "  Firmware:   1.5', file=sys.stderr)\n"
                f"json.dump(dict(os.environ), open({str(Path(folder) / 'env.json')!r}, 'w'))\n"
                "if not os.environ.get('HTTPS_PROXY'):\n"
                "    sys.exit(0)  # a test never reaches the real GitHub API\n"
                "try:\n"
                "    urllib.request.urlopen('https://api.github.com/repos/keirf/"
                "greaseweazle-firmware/releases/latest', timeout=5)\n"
                "except Exception as error:\n"
                "    print('** FATAL ERROR:', error, file=sys.stderr)\n"
                "    sys.exit(1)\n"
                "print('the request went out', file=sys.stderr)\n"
            )
            script.chmod(0o755)
            offline = client.probe(executable=str(script), online=False)
            environment = json.loads((Path(folder) / "env.json").read_text())
            self.assertTrue(environment["https_proxy"].startswith("http://127.0.0.1:"))
            online = client.probe(executable=str(script))
            self.assertNotIn("HTTPS_PROXY", json.loads((Path(folder) / "env.json").read_text()))
        self.assertTrue(offline.connected)
        self.assertEqual(offline.message, "Greaseweazle V4 connected.")
        self.assertTrue(online.connected)

    def test_the_refusing_proxy_refuses(self) -> None:
        with client.refusing_proxy() as proxy:
            port = int(proxy.rsplit(":", 1)[1])
            with self.assertRaises(ConnectionRefusedError):
                socket.create_connection(("127.0.0.1", port), timeout=5)

    def test_probe_without_gw(self) -> None:
        with mock.patch.object(client, "find_gw", return_value=None):
            status = client.probe()
        self.assertFalse(status.connected)
        self.assertIn("not installed", status.message)

    @unittest.skipUnless(client.find_gw(), "the Greaseweazle host tools (gw) are not installed")
    def test_real_gw_reports_its_version(self) -> None:
        self.assertTrue(client.probe().host_tools)


class DevicePresentTests(unittest.TestCase):
    """The file-only check the window repeats: a fake sysfs and /dev in a temporary folder."""

    def setUp(self) -> None:
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.sysfs = Path(folder.name) / "sys/bus/usb/devices"
        self.dev = Path(folder.name) / "dev"
        self.sysfs.mkdir(parents=True)
        self.dev.mkdir()
        self.usb_device("usb1", "1d6b", "0002", "EHCI Host Controller")
        self.usb_device("1-4", "046d", "c52b", "USB Receiver")

    def usb_device(self, name: str, vendor: str, product: str, product_name: str = "") -> None:
        folder = self.sysfs / name
        folder.mkdir()
        (folder / "idVendor").write_text(f"{vendor}\n")
        (folder / "idProduct").write_text(f"{product}\n")
        if product_name:
            (folder / "product").write_text(f"{product_name}\n")

    def present(self, device: str = "") -> bool:
        return client.device_present(device, sysfs=self.sysfs, dev=self.dev)

    def test_nothing_plugged_in(self) -> None:
        self.assertFalse(self.present())

    def test_the_greaseweazle_usb_id(self) -> None:
        self.usb_device("1-2", "1209", "4d69")
        self.assertTrue(self.present())

    def test_a_usb_interface_folder_without_ids_is_passed_over(self) -> None:
        (self.sysfs / "1-2:1.0").mkdir()
        self.assertFalse(self.present())

    def test_the_product_name_gw_accepts(self) -> None:
        # The old shared test id 1209:0001, named by its product string.
        self.usb_device("1-3", "1209", "0001", "Greaseweazle")
        self.assertTrue(self.present())

    def test_another_device_on_the_shared_test_id_is_not_taken(self) -> None:
        self.usb_device("1-3", "1209", "0001", "Some Other Gadget")
        self.assertFalse(self.present())

    def test_the_udev_link(self) -> None:
        (self.dev / "greaseweazle").write_text("")
        self.assertTrue(self.present())

    def test_a_serial_port_named_by_id(self) -> None:
        by_id = self.dev / "serial/by-id"
        by_id.mkdir(parents=True)
        (by_id / "usb-Keir_Fraser_Greaseweazle_GW0001-if00").write_text("")
        self.assertTrue(self.present())

    def test_the_chosen_device_must_exist(self) -> None:
        self.usb_device("1-2", "1209", "4d69")
        (self.dev / "ttyACM1").write_text("")
        self.assertTrue(self.present(str(self.dev / "ttyACM1")))
        self.assertTrue(self.present("ttyACM1"), "a bare name is looked up in /dev")
        self.assertFalse(self.present(str(self.dev / "ttyACM5")), "the chosen port decides")

    def test_an_unreadable_sysfs_is_not_an_error(self) -> None:
        missing = self.sysfs.parent / "missing"
        self.assertFalse(client.device_present(sysfs=missing, dev=self.dev))

    def test_no_gw_runs(self) -> None:
        self.usb_device("1-2", "1209", "4d69")
        with mock.patch.object(client, "run_streaming") as run:
            self.assertTrue(self.present())
        run.assert_not_called()


class FindTests(unittest.TestCase):
    def test_environment_override(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            command = fake_gw(Path(folder), [])
            with mock.patch.dict("os.environ", {"PIRATEFINDER_GW": command}):
                self.assertEqual(client.find_gw(), command)


if __name__ == "__main__":
    unittest.main()

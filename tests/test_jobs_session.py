from __future__ import annotations

import shutil
import tempfile
import threading
import time
import unittest
from dataclasses import replace
from pathlib import Path

from piratefinder.jobs.session import QuietEvents, WriteSession, download_for_item
from piratefinder.models import (
    Disk,
    DiskKind,
    ImageSource,
    LocalFile,
    Location,
    Platform,
    PreparedImage,
    QueueItem,
    SessionSummary,
    WriteOutcome,
    WriteProgress,
    WriteStatus,
)
from piratefinder.online.fetch import FetchError
from piratefinder.online.http import DownloadCancelled
from piratefinder.settings import Settings

VERIFIED = WriteOutcome(WriteStatus.VERIFIED, "The disk was written and verified.")


def local_source(path: str, label: str = "") -> ImageSource:
    return ImageSource(label=label or path, platform=Platform.ATARI_ST, local=LocalFile(path=path))


def online_source(location_id: int, label: str = "") -> ImageSource:
    location = Location(location_id, 1, "host", f"https://host.invalid/{location_id}.st")
    return ImageSource(
        label=label or f"online {location_id}", platform=Platform.ATARI_ST, location=location
    )


class FakeCatalogue:
    def disk(self, disk_id: int) -> Disk:
        return Disk(
            disk_id, f"Crew {disk_id}", Platform.ATARI_ST, DiskKind.MENU, series_name="Crew"
        )


class FakeFinder:
    def __init__(self, sources: dict[str, list[ImageSource]]) -> None:
        self.sources = sources
        self.catalogue = FakeCatalogue()

    def sources_for(self, item: QueueItem) -> list[ImageSource]:
        return list(self.sources.get(item.id, []))

    def archive_folders(self, disk_id: int) -> tuple[str, str]:
        return ("Games", "Crew")


class FakeLibrary:
    def __init__(self, files: dict[str, bytes]) -> None:
        self.files = files
        self.added: list[Path] = []

    def read_bytes(self, local: LocalFile) -> bytes:
        if local.path not in self.files:
            raise OSError(f"{local.path} is on a network share that is not mounted")
        return self.files[local.path]

    def add_file(self, path: Path) -> list[LocalFile]:
        self.added.append(Path(path))
        return []


class FakeHistory:
    def __init__(self) -> None:
        self.recorded: list[SessionSummary] = []

    def record(self, summary: SessionSummary) -> int:
        self.recorded.append(summary)
        return 1


class Recorder(QuietEvents):
    def __init__(self, answers: list[str] | None = None) -> None:
        self.answers = list(answers or [])
        self.stages: list[tuple[str, str]] = []
        self.prompts: list[tuple[str, str]] = []
        self.downloads: list[tuple[str, int, int | None]] = []
        self.progress: list[tuple[str, WriteProgress]] = []
        self.finished: list[tuple[str, WriteStatus]] = []

    def on_stage(self, item, index, total, stage, message) -> None:
        self.stages.append((item.label, stage))

    def on_download(self, item, done, total) -> None:
        self.downloads.append((item.label, done, total))

    def on_write_progress(self, item, progress) -> None:
        self.progress.append((item.label, progress))

    def ask_insert(self, item, index, total, reason="") -> str:
        self.prompts.append((item.label, reason))
        return self.answers.pop(0) if self.answers else "write"

    def on_item_finished(self, item, outcome) -> None:
        self.finished.append((item.label, outcome.status))


class FakeWriter:
    def __init__(self, outcomes: list[WriteOutcome] | None = None) -> None:
        self.outcomes = list(outcomes or [])
        self.calls: list[tuple[PreparedImage, dict]] = []
        self.before_return: list = []

    def __call__(self, prepared: PreparedImage, **options) -> WriteOutcome:
        self.calls.append((prepared, options))
        options["progress"](WriteProgress(0.5, 40, 0, 80, 160))
        for hook in self.before_return:
            hook(prepared, options)
        return self.outcomes.pop(0) if self.outcomes else VERIFIED


class FakePreparer:
    def __init__(self, refuse: set[str] | None = None) -> None:
        self.refuse = refuse or set()
        self.workdirs: list[Path] = []
        self.names: list[str] = []
        self.clean_virus: list[bool] = []

    def __call__(
        self, data: bytes, name: str, workdir: Path, *, label: str, platform, clean_virus: bool
    ) -> PreparedImage:
        self.workdirs.append(Path(workdir))
        self.names.append(name)
        self.clean_virus.append(clean_virus)
        if name in self.refuse:
            raise ValueError(f"{name} holds copy protection that a sector image cannot hold.")
        path = Path(workdir) / "disk.st"
        path.write_bytes(data)
        return PreparedImage(
            label, platform or Platform.ATARI_ST, str(path), None, notes=("Checked.",)
        )


class FakeFetcher:
    def __init__(self, folder: Path, fail: set[int] | None = None) -> None:
        self.folder = folder
        self.fail = fail or set()
        self.calls: list[tuple[int, str, dict]] = []
        self.called = threading.Event()

    def __call__(self, location: Location, image, **options) -> Path:
        self.calls.append((location.id, threading.current_thread().name, options))
        self.called.set()
        if location.id in self.fail:
            raise FetchError("The downloaded image does not match the catalogue MD5 checksum.")
        if options.get("progress"):
            options["progress"](10, 20)
            options["progress"](20, 20)
        path = self.folder.joinpath(*options["folders"]) / f"{location.id}.st"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"downloaded %d" % location.id)
        if options.get("notes") is not None:
            options["notes"].append("No checksum is known for this image.")
        return path


class SessionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.folder = Path(tempfile.mkdtemp(prefix="pf-session-"))
        self.addCleanup(shutil.rmtree, self.folder, True)
        self.settings = Settings(
            download_folder=str(self.folder / "downloads"), drive="B", retries=5
        )
        self.writer = FakeWriter()
        self.preparer = FakePreparer()
        self.fetcher = FakeFetcher(self.folder / "downloads")
        self.history = FakeHistory()
        self.library = FakeLibrary({"/nas/one.st": b"one", "/nas/two.st": b"two"})

    def item(self, number: int, copies: int = 1) -> QueueItem:
        return QueueItem(
            id=f"i{number}",
            label=f"Crew {number}",
            platform=Platform.ATARI_ST,
            disk_id=number,
            copies=copies,
        )

    def session(self, items, sources, events) -> WriteSession:
        return WriteSession(
            FakeFinder(sources),
            self.library,
            self.settings,
            items,
            events,
            writer=self.writer,
            preparer=self.preparer,
            fetcher=self.fetcher,
            history=self.history,
        )

    def assert_workdirs_removed(self) -> None:
        self.assertTrue(self.preparer.workdirs)
        self.assertEqual([path for path in self.preparer.workdirs if path.exists()], [])

    def test_local_disk_is_written(self) -> None:
        events = Recorder()
        item = self.item(1)
        summary = self.session([item], {"i1": [local_source("/nas/one.st")]}, events).run()
        self.assertEqual(summary.drive, "B")
        # The notes made on the way go into the result, and so into the history.
        written = replace(VERIFIED, notes=("Checked.",))
        self.assertEqual(summary.items, (("Crew 1", written, "/nas/one.st"),))
        self.assertEqual(
            [stage for _label, stage in events.stages], ["resolve", "prepare", "insert", "write"]
        )
        _prepared, options = self.writer.calls[0]
        self.assertEqual(
            (options["drive"], options["retries"], options["pre_erase"]), ("B", 5, False)
        )
        self.assertEqual(events.progress[0][1].track_number, 80)
        self.assertEqual(events.finished, [("Crew 1", WriteStatus.VERIFIED)])
        self.assertEqual(item.outcome, written)
        self.assertEqual(item.source_used, "/nas/one.st")
        self.assertEqual(item.notes, ["Checked."])
        self.assertEqual(self.history.recorded, [summary])
        self.assert_workdirs_removed()

    def test_the_item_decides_whether_a_boot_virus_is_removed(self) -> None:
        keep = self.item(2)
        keep.clean_virus = False
        sources = {"i1": [local_source("/nas/one.st")], "i2": [local_source("/nas/two.st")]}
        self.session([self.item(1), keep], sources, Recorder()).run()
        self.assertEqual(self.preparer.clean_virus, [True, False])

    def test_falls_back_when_a_local_file_cannot_be_read(self) -> None:
        sources = {"i1": [local_source("/offline/one.st"), local_source("/nas/two.st")]}
        summary = self.session([self.item(1)], sources, Recorder()).run()
        self.assertEqual(summary.items[0][2], "/nas/two.st")

    def test_failed_download_tries_the_next_source(self) -> None:
        self.fetcher.fail = {1}
        events = Recorder()
        item = self.item(1)
        summary = self.session([item], {"i1": [online_source(1), online_source(2)]}, events).run()
        self.assertEqual(summary.items[0][1].status, WriteStatus.VERIFIED)
        self.assertEqual(summary.items[0][2], "online 2")
        self.assertEqual([call[0] for call in self.fetcher.calls], [1, 2])
        options = self.fetcher.calls[1][2]
        self.assertEqual(options["download_folder"], self.settings.download_folder)
        self.assertEqual(options["folders"], ("Games", "Crew"))
        self.assertEqual(events.downloads, [("Crew 1", 10, 20), ("Crew 1", 20, 20)])
        self.assertEqual(
            self.library.added, [self.folder / "downloads" / "Games" / "Crew" / "2.st"]
        )
        self.assertIn("No checksum is known for this image.", item.notes)
        self.assertIn("download", [stage for _l, stage in events.stages])

    def test_prepare_failure_tries_the_next_source(self) -> None:
        self.preparer.refuse = {"one.st"}
        sources = {"i1": [local_source("/nas/one.st"), local_source("/nas/two.st")]}
        summary = self.session([self.item(1)], sources, Recorder()).run()
        self.assertEqual(summary.items[0][2], "/nas/two.st")
        self.assert_workdirs_removed()

    def test_nothing_usable(self) -> None:
        self.fetcher.fail = {1}
        self.preparer.refuse = {"one.st"}
        events = Recorder()
        summary = self.session(
            [self.item(1), self.item(2)],
            {"i1": [local_source("/nas/one.st"), online_source(1)]},
            events,
        ).run()
        first, second = (outcome for _label, outcome, _source in summary.items)
        self.assertEqual(first.status, WriteStatus.UNAVAILABLE)
        self.assertIn("copy protection", first.diagnostic)
        self.assertIn("MD5", first.diagnostic)
        self.assertEqual(second.status, WriteStatus.UNAVAILABLE)
        self.assertIn("no enabled provider", second.summary)
        self.assertEqual(events.prompts, [])
        self.assertEqual(self.writer.calls, [])
        self.assert_workdirs_removed()

    def test_write_protected_disk_prompts_again(self) -> None:
        self.writer.outcomes = [
            WriteOutcome(WriteStatus.WRITE_PROTECTED, "The disk is write-protected."),
            WriteOutcome(WriteStatus.NO_DISK, ""),
            VERIFIED,
        ]
        events = Recorder()
        summary = self.session([self.item(1)], {"i1": [local_source("/nas/one.st")]}, events).run()
        self.assertEqual(summary.items[0][1], replace(VERIFIED, notes=("Checked.",)))
        self.assertEqual(
            [reason for _label, reason in events.prompts],
            ["", "The disk is write-protected.", "There is no disk in the drive. Insert a disk."],
        )
        self.assertEqual(len(self.writer.calls), 3)

    def test_write_protected_then_skip(self) -> None:
        self.writer.outcomes = [WriteOutcome(WriteStatus.WRITE_PROTECTED, "Protected.")]
        events = Recorder(["write", "skip"])
        summary = self.session([self.item(1)], {"i1": [local_source("/nas/one.st")]}, events).run()
        self.assertEqual(summary.items[0][1].status, WriteStatus.SKIPPED)

    def test_skip_and_stop(self) -> None:
        events = Recorder(["skip", "write", "stop"])
        items = [self.item(n) for n in range(1, 5)]
        sources = {item.id: [local_source("/nas/one.st")] for item in items}
        summary = self.session(items, sources, events).run()
        self.assertEqual(
            [outcome.status for _l, outcome, _s in summary.items],
            [WriteStatus.SKIPPED, WriteStatus.VERIFIED, WriteStatus.SKIPPED, WriteStatus.SKIPPED],
        )
        self.assertEqual(len(self.writer.calls), 1)
        self.assertIn("stopped", summary.items[3][1].summary)
        self.assertEqual(len(events.finished), 4)
        self.assert_workdirs_removed()

    def test_notes_describe_this_session_only(self) -> None:
        events = Recorder(["write", "stop"])
        items = [self.item(n) for n in range(1, 4)]
        items[2].notes = ["A note from an earlier session."]
        sources = {item.id: [local_source("/nas/one.st")] for item in items}
        summary = self.session(items, sources, events).run()
        notes = [outcome.notes for _label, outcome, _source in summary.items]
        # Written, then stopped at the prompt with the image prepared, then never reached.
        self.assertEqual(notes, [("Checked.",), ("Checked.",), ()])
        self.assertEqual(items[2].notes, [])

    def test_prompt_only_for_the_first_disk_when_asked(self) -> None:
        self.settings.prompt_between_disks = False
        events = Recorder()
        items = [self.item(1), self.item(2, copies=2)]
        sources = {item.id: [local_source("/nas/one.st")] for item in items}
        summary = self.session(items, sources, events).run()
        self.assertEqual(len(events.prompts), 1)
        self.assertEqual(len(summary.items), 3)

    def test_copies(self) -> None:
        events = Recorder()
        summary = self.session(
            [self.item(1, copies=2)], {"i1": [local_source("/nas/one.st")]}, events
        ).run()
        self.assertEqual(
            [label for label, _o, _s in summary.items],
            ["Crew 1 (copy 1 of 2)", "Crew 1 (copy 2 of 2)"],
        )
        self.assertEqual(len(events.prompts), 2)
        self.assertEqual(len(self.preparer.workdirs), 1, "one preparation serves every copy")

    def test_cancel_during_a_write(self) -> None:
        controllers = []

        def cancel_now(prepared, options) -> None:
            controllers.append(options["controller"])
            session.cancel()

        self.writer.before_return.append(cancel_now)
        self.writer.outcomes = [WriteOutcome(WriteStatus.FAILED, "Interrupted.")]
        items = [self.item(1), self.item(2)]
        sources = {item.id: [local_source("/nas/one.st")] for item in items}
        session = self.session(items, sources, Recorder())
        summary = session.run()
        self.assertTrue(controllers[0].cancelled)
        self.assertEqual(
            [outcome.status for _l, outcome, _s in summary.items],
            [WriteStatus.CANCELLED, WriteStatus.SKIPPED],
        )
        self.assertIn("cancelled", summary.items[1][1].summary)
        self.assert_workdirs_removed()

    def test_writer_fault_is_a_failure(self) -> None:
        def explode(prepared, options) -> None:
            raise RuntimeError("usb disconnected")

        self.writer.before_return.append(explode)
        summary = self.session(
            [self.item(1)], {"i1": [local_source("/nas/one.st")]}, Recorder()
        ).run()
        self.assertEqual(summary.items[0][1].status, WriteStatus.FAILED)
        self.assertIn("usb disconnected", summary.items[0][1].summary)

    def test_next_online_disk_downloads_while_writing(self) -> None:
        seen_during_write: list[bool] = []

        def wait_for_prefetch(prepared, options) -> None:
            if not seen_during_write:
                seen_during_write.append(self.fetcher.called.wait(5))

        self.writer.before_return.append(wait_for_prefetch)
        events = Recorder()
        items = [self.item(1), self.item(2)]
        sources = {"i1": [local_source("/nas/one.st")], "i2": [online_source(7)]}
        summary = self.session(items, sources, events).run()
        self.assertEqual(seen_during_write, [True])
        self.assertEqual(len(self.fetcher.calls), 1, "the prefetched image is not fetched again")
        location_id, thread_name, _options = self.fetcher.calls[0]
        self.assertEqual(location_id, 7)
        self.assertNotEqual(thread_name, threading.current_thread().name)
        self.assertEqual(summary.items[1][2], "online 7")
        self.assertEqual(summary.items[1][1].status, WriteStatus.VERIFIED)
        self.assertEqual(self.preparer.names[-1], "7.st")

    def test_a_member_is_prepared_under_its_own_file_name(self) -> None:
        local = LocalFile(path="/nas/one.st", member="set.zip::menus\\Crew 1.ST")
        source = ImageSource(label="set", platform=Platform.ATARI_ST, local=local)
        self.session([self.item(1)], {"i1": [source]}, Recorder()).run()
        self.assertEqual(self.preparer.names, ["Crew 1.ST"])

    def test_failed_prefetch_falls_back_to_the_remaining_sources(self) -> None:
        self.fetcher.fail = {7}
        items = [self.item(1), self.item(2)]
        sources = {"i1": [local_source("/nas/one.st")], "i2": [online_source(7), online_source(8)]}
        summary = self.session(items, sources, Recorder()).run()
        self.assertEqual(summary.items[1][2], "online 8")
        self.assertEqual(sorted(call[0] for call in self.fetcher.calls), [7, 8])

    def test_background_download_stops_when_the_session_stops(self) -> None:
        observed: list[bool] = []

        def slow_fetch(location, image, **options) -> Path:
            cancel = options["cancel"]
            for _step in range(100):
                if cancel.cancelled:
                    observed.append(True)
                    raise DownloadCancelled("The download was cancelled.")
                time.sleep(0.05)
            observed.append(False)
            raise FetchError("The download took too long.")

        self.fetcher = slow_fetch
        self.writer.outcomes = [WriteOutcome(WriteStatus.WRITE_PROTECTED, "Protected.")]
        events = Recorder(["write", "stop"])
        items = [self.item(1), self.item(2)]
        sources = {"i1": [local_source("/nas/one.st")], "i2": [online_source(7)]}
        started = time.monotonic()
        summary = self.session(items, sources, events).run()
        self.assertLess(time.monotonic() - started, 3.0)
        self.assertEqual(observed, [True])
        self.assertEqual(
            [outcome.status for _l, outcome, _s in summary.items],
            [WriteStatus.SKIPPED, WriteStatus.SKIPPED],
        )

    def test_download_for_item(self) -> None:
        self.fetcher.fail = {1}
        finder = FakeFinder(
            {"i1": [local_source("/nas/one.st"), online_source(1), online_source(2)]}
        )
        path = download_for_item(
            finder, self.library, self.settings, self.item(1), fetcher=self.fetcher
        )
        self.assertEqual(path.name, "2.st")
        with self.assertRaises(FetchError):
            download_for_item(
                FakeFinder({}), self.library, self.settings, self.item(1), fetcher=self.fetcher
            )


if __name__ == "__main__":
    unittest.main()

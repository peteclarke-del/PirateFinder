from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from piratefinder.jobs.queue import (
    MAX_COPIES,
    WriteQueue,
    clamp_copies,
    copy_labels,
    fresh_copy,
    item_from_disk,
    item_from_local,
    new_item,
    queue_key,
)
from piratefinder.models import (
    Disk,
    DiskKind,
    LocalFile,
    Platform,
    QueueItem,
    WriteOutcome,
    WriteStatus,
)


def disk(disk_id: int, label: str = "") -> Disk:
    return Disk(disk_id, label or f"Crew {disk_id}", Platform.ATARI_ST, DiskKind.MENU)


def queued(disk_id: int) -> QueueItem:
    """A queue item for the best dump of a catalogue disk."""
    return item_from_disk(disk(disk_id))


class QueueTests(unittest.TestCase):
    def setUp(self) -> None:
        self.folder = Path(tempfile.mkdtemp(prefix="pf-queue-"))
        self.addCleanup(shutil.rmtree, self.folder, True)
        self.path = self.folder / "queue.json"
        self.queue = WriteQueue(self.path)

    def test_default_path_is_in_the_data_folder(self) -> None:
        with mock.patch.dict(os.environ, {"XDG_DATA_HOME": str(self.folder)}):
            queue = WriteQueue()
        self.assertEqual(queue.path, self.folder / "piratefinder" / "queue.json")

    def test_items_from_disks_and_files(self) -> None:
        best = queued(1)
        self.assertEqual(
            (best.label, best.disk_id, best.image_id, best.platform, best.clean_virus),
            ("Crew 1", 1, None, Platform.ATARI_ST, True),
        )
        chosen = item_from_disk(disk(2), 7, copies=2, clean_virus=False)
        self.assertEqual(
            (chosen.disk_id, chosen.image_id, chosen.copies, chosen.clean_virus), (2, 7, 2, False)
        )
        local = LocalFile(path="/nas/Mystery.adf", format="adf", display_name="Mystery")
        from_file = item_from_local(local)
        self.assertEqual(
            (from_file.label, from_file.platform, from_file.local),
            ("Mystery", Platform.AMIGA, local),
        )
        self.assertNotEqual(best.id, chosen.id)
        self.assertEqual(item_from_local(LocalFile(path="/nas/x.st")).label, "x.st")
        nested = LocalFile(path="/nas/set.7z", member="inner.zip::x.st")
        self.assertEqual(item_from_local(nested).label, "x.st")
        self.assertEqual(new_item("Many", None, disk_id=3, copies=500).copies, 99)

    def test_a_flux_image_is_queued_without_a_platform(self) -> None:
        # IPF, SCP and HFE files hold either platform; the writer reads it from the header.
        for suffix in ("ipf", "scp", "hfe"):
            with self.subTest(suffix=suffix):
                local = LocalFile(path=f"/nas/Game.{suffix}", format=suffix)
                self.assertIsNone(item_from_local(local).platform)

    def test_a_fresh_copy_is_a_new_entry_with_the_same_choices(self) -> None:
        item = item_from_disk(disk(3), 31, copies=2, clean_virus=False)
        item.outcome = WriteOutcome(WriteStatus.FAILED, "failed")
        item.notes.append("Converted from MSA.")
        again = fresh_copy(item)
        self.assertNotEqual(again.id, item.id)
        self.assertEqual(
            (again.label, again.disk_id, again.image_id, again.copies, again.clean_virus),
            ("Crew 3", 3, 31, 2, False),
        )
        self.assertEqual((again.outcome, again.notes), (None, []))

    def test_the_queue_key_is_the_disk_else_the_file(self) -> None:
        self.assertEqual(queue_key(item_from_disk(disk(3), 31)), queue_key(queued(3)))
        local = LocalFile(path="/x/y.zip", member="a.adf")
        self.assertEqual(queue_key(item_from_local(local)), ("file", "/x/y.zip", "a.adf"))

    def test_add_deduplicates_by_disk_and_file(self) -> None:
        self.assertTrue(self.queue.add(queued(1)))
        self.assertFalse(self.queue.add(item_from_disk(disk(1), 3)))
        local = LocalFile(path="/nas/a.zip", member="x.st")
        self.assertTrue(self.queue.add(item_from_local(local)))
        self.assertFalse(
            self.queue.add(item_from_local(LocalFile(path="/nas/a.zip", member="x.st")))
        )
        self.assertTrue(
            self.queue.add(item_from_local(LocalFile(path="/nas/a.zip", member="y.st")))
        )
        added = self.queue.extend([queued(n) for n in (1, 2, 2, 3)])
        self.assertEqual(added, 2)
        self.assertEqual(len(self.queue), 5)

    def test_move_remove_copies_and_clear(self) -> None:
        items = [queued(n) for n in range(1, 5)]
        self.queue.extend(items)
        self.queue.move(items[3].id, -2)
        self.assertEqual([i.disk_id for i in self.queue.items()], [1, 4, 2, 3])
        self.queue.move(items[0].id, 10)
        self.assertEqual([i.disk_id for i in self.queue.items()], [4, 2, 3, 1])
        self.queue.move(items[3].id, -10)
        self.assertEqual([i.disk_id for i in self.queue.items()], [4, 2, 3, 1])
        self.queue.remove(items[1].id)
        self.queue.remove("unknown")
        self.assertEqual([i.disk_id for i in self.queue.items()], [4, 3, 1])
        self.queue.set_copies(items[2].id, 500)
        self.assertEqual(self.queue.get(items[2].id).copies, 99)
        self.queue.set_copies(items[2].id, 0)
        self.assertEqual(self.queue.get(items[2].id).copies, 1)
        self.queue.clear()
        self.assertEqual(self.queue.items(), [])

    def test_listeners(self) -> None:
        calls: list[int] = []
        handler = self.queue.connect(lambda queue: calls.append(len(queue)))
        self.queue.add(queued(1))
        self.queue.add(queued(1))  # duplicate
        self.queue.clear()
        self.queue.clear()  # already empty
        self.assertEqual(calls, [1, 0])
        self.queue.disconnect(handler)
        self.queue.add(queued(2))
        self.assertEqual(calls, [1, 0])

    def test_persisted_between_runs_without_outcomes(self) -> None:
        local = LocalFile(path="/nas/a.zip", member="x.st", listing=("A.PRG",), format="st")
        first = queued(1)
        first.outcome = WriteOutcome(WriteStatus.VERIFIED, "done")
        first.notes.append("Converted from MSA.")
        first.clean_virus = False  # the user chose to keep the boot block
        self.queue.extend([first, item_from_local(local, copies=3)])
        again = WriteQueue(self.path)
        items = again.items()
        self.assertEqual([i.id for i in items], [i.id for i in self.queue.items()])
        self.assertIsNone(items[0].outcome)
        self.assertEqual(items[0].notes, ["Converted from MSA."])
        self.assertFalse(items[0].clean_virus)
        self.assertTrue(items[1].clean_virus)
        self.assertEqual(items[1].local, local)
        self.assertEqual(items[1].copies, 3)
        self.assertEqual(items[1].platform, Platform.ATARI_ST)

    def test_damaged_queue_file_is_ignored(self) -> None:
        self.path.write_text("{broken")
        self.assertEqual(WriteQueue(self.path).items(), [])
        self.path.write_text(
            json.dumps(
                {
                    "items": [
                        {"label": "ok", "disk_id": 5},
                        {"label": "no target"},
                        {"disk_id": 6},
                        {"label": "bad platform", "disk_id": 7, "platform": "c64"},
                        "nonsense",
                        {"label": "duplicate", "disk_id": 5},
                    ]
                }
            )
        )
        items = WriteQueue(self.path).items()
        self.assertEqual([(i.label, i.disk_id) for i in items], [("ok", 5)])
        self.assertTrue(items[0].id)

    def test_queue_items_are_the_shared_model(self) -> None:
        item = QueueItem(id="x", label="X", platform=None, disk_id=1)
        self.queue.add(item)
        self.assertIs(self.queue.items()[0], item)


class CopiesTests(unittest.TestCase):
    """One rule for how many floppies an item may ask for, and what each is called."""

    def test_the_rule(self) -> None:
        self.assertEqual([clamp_copies(n) for n in (-3, 0, 1, 7, 500)], [1, 1, 1, 7, MAX_COPIES])
        item = new_item("Crew 1", None, disk_id=1)
        self.assertEqual(copy_labels(item), ["Crew 1"])
        item.copies = 2
        self.assertEqual(copy_labels(item), ["Crew 1 (copy 1 of 2)", "Crew 1 (copy 2 of 2)"])
        item.copies = 0
        self.assertEqual(copy_labels(item), ["Crew 1"])
        item.copies = 500
        self.assertEqual(len(copy_labels(item)), MAX_COPIES)

    def test_every_backend_keeps_to_it(self) -> None:
        from piratefinder.settings import Settings
        from piratefinder.ui.backend import UnavailableBackend
        from piratefinder.ui.fake_backend import FakeBackend

        for backend in (
            FakeBackend(Settings(path=None)),
            UnavailableBackend("no catalogue", Settings(path=None)),
        ):
            with self.subTest(backend=type(backend).__name__):
                backend.queue_clear()
                item = new_item("Crew 1", None, disk_id=1)
                backend.queue_add([item])
                for asked, kept in ((500, MAX_COPIES), (0, 1), (3, 3)):
                    backend.queue_set_copies(item.id, asked)
                    self.assertEqual(backend.queue_items()[0].copies, kept)


if __name__ == "__main__":
    unittest.main()

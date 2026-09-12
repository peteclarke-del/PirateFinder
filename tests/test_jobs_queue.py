from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from piratefinder.jobs.queue import (
    WriteQueue,
    item_from_detail,
    item_from_local,
    item_from_result,
)
from piratefinder.models import (
    Availability,
    Disk,
    DiskDetail,
    DiskKind,
    LocalFile,
    Platform,
    QueueItem,
    SearchResult,
    WriteOutcome,
    WriteStatus,
)


def disk(disk_id: int, label: str = "") -> Disk:
    return Disk(disk_id, label or f"Crew {disk_id}", Platform.ATARI_ST, DiskKind.MENU)


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

    def test_items_from_results_details_and_files(self) -> None:
        from_result = item_from_result(SearchResult(Availability.LOCAL, disk=disk(1)))
        self.assertEqual(
            (from_result.label, from_result.disk_id, from_result.platform),
            ("Crew 1", 1, Platform.ATARI_ST),
        )
        from_detail = item_from_detail(DiskDetail(disk=disk(2)), image_id=7, copies=2)
        self.assertEqual((from_detail.disk_id, from_detail.image_id, from_detail.copies), (2, 7, 2))
        local = LocalFile(path="/nas/Mystery.adf", format="adf", display_name="Mystery")
        from_file = item_from_result(SearchResult(Availability.LOCAL, local=local))
        self.assertEqual(
            (from_file.label, from_file.platform, from_file.local),
            ("Mystery", Platform.AMIGA, local),
        )
        self.assertNotEqual(from_result.id, from_detail.id)
        self.assertEqual(item_from_local(LocalFile(path="/nas/x.st")).label, "x.st")

    def test_add_deduplicates_by_disk_and_file(self) -> None:
        self.assertTrue(
            self.queue.add(item_from_result(SearchResult(Availability.LOCAL, disk=disk(1))))
        )
        self.assertFalse(self.queue.add(item_from_detail(DiskDetail(disk=disk(1)), image_id=3)))
        local = LocalFile(path="/nas/a.zip", member="x.st")
        self.assertTrue(self.queue.add(item_from_local(local)))
        self.assertFalse(
            self.queue.add(item_from_local(LocalFile(path="/nas/a.zip", member="x.st")))
        )
        self.assertTrue(
            self.queue.add(item_from_local(LocalFile(path="/nas/a.zip", member="y.st")))
        )
        added = self.queue.extend(
            [item_from_result(SearchResult(Availability.LOCAL, disk=disk(n))) for n in (1, 2, 2, 3)]
        )
        self.assertEqual(added, 2)
        self.assertEqual(len(self.queue), 5)

    def test_move_remove_copies_and_clear(self) -> None:
        items = [
            item_from_result(SearchResult(Availability.LOCAL, disk=disk(n))) for n in range(1, 5)
        ]
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
        self.queue.add(item_from_result(SearchResult(Availability.LOCAL, disk=disk(1))))
        self.queue.add(
            item_from_result(SearchResult(Availability.LOCAL, disk=disk(1)))
        )  # duplicate
        self.queue.clear()
        self.queue.clear()  # already empty
        self.assertEqual(calls, [1, 0])
        self.queue.disconnect(handler)
        self.queue.add(item_from_result(SearchResult(Availability.LOCAL, disk=disk(2))))
        self.assertEqual(calls, [1, 0])

    def test_persisted_between_runs_without_outcomes(self) -> None:
        local = LocalFile(path="/nas/a.zip", member="x.st", listing=("A.PRG",), format="st")
        first = item_from_result(SearchResult(Availability.LOCAL, disk=disk(1)))
        first.outcome = WriteOutcome(WriteStatus.VERIFIED, "done")
        first.notes.append("Converted from MSA.")
        self.queue.extend([first, item_from_local(local, copies=3)])
        again = WriteQueue(self.path)
        items = again.items()
        self.assertEqual([i.id for i in items], [i.id for i in self.queue.items()])
        self.assertIsNone(items[0].outcome)
        self.assertEqual(items[0].notes, ["Converted from MSA."])
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


if __name__ == "__main__":
    unittest.main()

"""The write queue: disks waiting to be written, kept between runs.

The queue is saved to ``queue.json`` in the data folder after every change.
A disk is queued at most once, and so is an unmatched local file. Listeners
added with ``connect`` are called with the queue after every change.
"""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
import threading
import uuid
from collections.abc import Callable, Iterable
from dataclasses import asdict, fields
from pathlib import Path
from typing import Any

from .. import paths
from ..images.inspect import local_platform
from ..library.library import local_name
from ..models import Disk, LocalFile, Platform, QueueItem

QUEUE_FILE = "queue.json"
MAX_COPIES = 99

Listener = Callable[["WriteQueue"], None]


def new_item_id() -> str:
    """A fresh identifier for a queue item."""
    return uuid.uuid4().hex


def clamp_copies(value: int) -> int:
    """How many floppies to write, from 1 to MAX_COPIES."""
    return min(max(1, int(value)), MAX_COPIES)


def copy_labels(item: QueueItem) -> list[str]:
    """What each floppy written for ``item`` is called: "Crew 1 (copy 2 of 3)" among several."""
    copies = clamp_copies(item.copies)
    if copies == 1:
        return [item.label]
    return [f"{item.label} (copy {number} of {copies})" for number in range(1, copies + 1)]


def new_item(
    label: str,
    platform: Platform | None,
    *,
    disk_id: int | None = None,
    image_id: int | None = None,
    local: LocalFile | None = None,
    copies: int = 1,
    clean_virus: bool = True,
) -> QueueItem:
    """A new queue item with a fresh id and no outcome; the other builders use this."""
    return QueueItem(
        id=new_item_id(),
        label=label,
        platform=platform,
        disk_id=disk_id,
        image_id=image_id,
        local=local,
        copies=clamp_copies(copies),
        clean_virus=clean_virus,
    )


def item_from_disk(
    disk: Disk, image_id: int | None = None, *, copies: int = 1, clean_virus: bool = True
) -> QueueItem:
    """A queue item for a catalogue disk: its best dump, or ``image_id`` when one is chosen."""
    return new_item(
        disk.label,
        disk.platform,
        disk_id=disk.id,
        image_id=image_id,
        copies=copies,
        clean_virus=clean_virus,
    )


def item_from_local(local: LocalFile, copies: int = 1) -> QueueItem:
    """A queue item for one local image file, with the platform its format says."""
    return new_item(
        local_name(local),
        local_platform(local),
        disk_id=local.disk_id,
        image_id=local.image_id,
        local=local,
        copies=copies,
    )


def fresh_copy(item: QueueItem) -> QueueItem:
    """The same disk or file as a new queue item with no outcome, for Retry Failed."""
    return new_item(
        item.label,
        item.platform,
        disk_id=item.disk_id,
        image_id=item.image_id,
        local=item.local,
        copies=item.copies,
        clean_virus=item.clean_virus,
    )


def queue_key(item: QueueItem) -> tuple[str, ...]:
    """What makes two queue items the same entry: the disk, else the local file.

    The queue holds each key once, whichever dump of the disk was chosen.
    """
    if item.disk_id is not None:
        return ("disk", str(item.disk_id))
    if item.local is not None:
        return ("file", item.local.path, item.local.member)
    return ("item", item.id)


def item_to_dict(item: QueueItem) -> dict[str, Any]:
    """A queue item as plain JSON data, without its session outcome."""
    return {
        "id": item.id,
        "label": item.label,
        "platform": str(item.platform) if item.platform is not None else None,
        "disk_id": item.disk_id,
        "image_id": item.image_id,
        "local": _local_to_dict(item.local) if item.local is not None else None,
        "copies": item.copies,
        "clean_virus": item.clean_virus,
        "notes": list(item.notes),
    }


def item_from_dict(data: dict[str, Any]) -> QueueItem | None:
    """A queue item read back from JSON, or None when the data is unusable."""
    try:
        platform = Platform(data["platform"]) if data.get("platform") else None
        local = _local_from_dict(data["local"]) if data.get("local") else None
        disk_id = data.get("disk_id")
        image_id = data.get("image_id")
        item = QueueItem(
            id=str(data.get("id") or new_item_id()),
            label=str(data["label"]),
            platform=platform,
            disk_id=int(disk_id) if disk_id is not None else None,
            image_id=int(image_id) if image_id is not None else None,
            local=local,
            copies=clamp_copies(data.get("copies", 1)),
            clean_virus=bool(data.get("clean_virus", True)),
            notes=[str(note) for note in data.get("notes", [])],
        )
    except (KeyError, TypeError, ValueError):
        return None
    if item.disk_id is None and item.local is None:
        return None
    return item


def _local_to_dict(local: LocalFile) -> dict[str, Any]:
    data = asdict(local)
    data["listing"] = list(local.listing)
    return data


def _local_from_dict(data: dict[str, Any]) -> LocalFile:
    known = {item.name for item in fields(LocalFile)}
    values = {key: value for key, value in data.items() if key in known}
    values["listing"] = tuple(values.get("listing") or ())
    return LocalFile(**values)


class WriteQueue:
    """An ordered, persisted list of queue items."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = Path(path) if path is not None else paths.data_dir() / QUEUE_FILE
        self._items: list[QueueItem] = []
        self._listeners: dict[int, Listener] = {}
        self._next_listener = 1
        self._lock = threading.RLock()
        self._load()

    def items(self) -> list[QueueItem]:
        """The queued items in order."""
        with self._lock:
            return list(self._items)

    def get(self, item_id: str) -> QueueItem | None:
        """One item by id, or None."""
        with self._lock:
            return next((item for item in self._items if item.id == item_id), None)

    def __len__(self) -> int:
        with self._lock:
            return len(self._items)

    def add(self, item: QueueItem) -> bool:
        """Append ``item`` unless its disk or file is already queued; True when added."""
        with self._lock:
            key = queue_key(item)
            if any(queue_key(existing) == key for existing in self._items):
                return False
            self._items.append(item)
        self._changed()
        return True

    def extend(self, items: Iterable[QueueItem]) -> int:
        """Append several items, skipping duplicates; return how many were added."""
        added = 0
        with self._lock:
            keys = {queue_key(existing) for existing in self._items}
            for item in items:
                key = queue_key(item)
                if key in keys:
                    continue
                keys.add(key)
                self._items.append(item)
                added += 1
        if added:
            self._changed()
        return added

    def remove(self, item_id: str) -> None:
        """Remove one item; unknown ids are ignored."""
        with self._lock:
            before = len(self._items)
            self._items = [item for item in self._items if item.id != item_id]
            changed = len(self._items) != before
        if changed:
            self._changed()

    def move(self, item_id: str, offset: int) -> None:
        """Move one item ``offset`` places, negative for earlier, clamped to the ends."""
        with self._lock:
            index = next((i for i, item in enumerate(self._items) if item.id == item_id), None)
            if index is None or offset == 0:
                return
            target = min(max(index + offset, 0), len(self._items) - 1)
            if target == index:
                return
            self._items.insert(target, self._items.pop(index))
        self._changed()

    def set_copies(self, item_id: str, copies: int) -> None:
        """Set how many floppies to write for one item, from 1 to MAX_COPIES."""
        with self._lock:
            item = next((item for item in self._items if item.id == item_id), None)
            if item is None:
                return
            item.copies = clamp_copies(copies)
        self._changed()

    def clear(self) -> None:
        """Remove every item."""
        with self._lock:
            if not self._items:
                return
            self._items = []
        self._changed()

    def connect(self, callback: Listener) -> int:
        """Call ``callback(queue)`` after every change; return an id for ``disconnect``."""
        with self._lock:
            handler = self._next_listener
            self._next_listener += 1
            self._listeners[handler] = callback
        return handler

    def disconnect(self, handler: int) -> None:
        """Stop calling a listener added with ``connect``."""
        with self._lock:
            self._listeners.pop(handler, None)

    def save(self) -> None:
        """Write the queue to disk atomically."""
        with self._lock:
            data = {"version": 1, "items": [item_to_dict(item) for item in self._items]}
        text = json.dumps(data, indent=2) + "\n"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle, temporary = tempfile.mkstemp(prefix=f".{self.path.name}.", dir=self.path.parent)
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as stream:
                stream.write(text)
            os.replace(temporary, self.path)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(temporary)
            raise

    def _load(self) -> None:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        records = data.get("items", []) if isinstance(data, dict) else []
        items: list[QueueItem] = []
        keys: set[tuple[str, ...]] = set()
        for record in records if isinstance(records, list) else []:
            item = item_from_dict(record) if isinstance(record, dict) else None
            if item is None or queue_key(item) in keys:
                continue
            keys.add(queue_key(item))
            items.append(item)
        self._items = items

    def _changed(self) -> None:
        with contextlib.suppress(OSError):
            self.save()
        with self._lock:
            listeners = list(self._listeners.values())
        for listener in listeners:
            listener(self)

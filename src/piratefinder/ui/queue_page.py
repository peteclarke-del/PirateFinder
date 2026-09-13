"""The Queue page: disks waiting to be written, the running session and its summary."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gtk, Pango  # noqa: E402

from ..jobs.queue import MAX_COPIES, fresh_copy  # noqa: E402
from ..models import (  # noqa: E402
    QueueItem,
    SessionSummary,
    WriteOutcome,
    WriteProgress,
    WriteStatus,
)
from . import formatting as fmt  # noqa: E402
from .bridge import PendingAnswer, SessionEventsBridge, run_in_thread  # noqa: E402
from .log import LOG  # noqa: E402
from .widgets import (  # noqa: E402
    GroupRows,
    alert,
    copy_text,
    icon_button,
    plain_row,
    set_fraction,
    status_icon,
    text_button,
    toast,
)


def drive_index(code: str) -> int:
    codes = [drive for drive, _name, _explanation in fmt.DRIVES]
    return codes.index(code) if code in codes else 0


def drive_row(on_changed) -> Adw.ComboRow:
    """The drive choice, shared by the Queue page and Preferences."""
    model = Gtk.StringList.new([name for _code, name, _explanation in fmt.DRIVES])
    row = Adw.ComboRow(title="_Drive", model=model, use_underline=True, use_subtitle=False)

    def changed(combo: Adw.ComboRow, _property) -> None:
        index = combo.get_selected()
        if 0 <= index < len(fmt.DRIVES):
            code, _name, explanation = fmt.DRIVES[index]
            combo.set_subtitle(explanation)
            on_changed(code)

    row.connect("notify::selected", changed)
    return row


def set_drive_row(row: Adw.ComboRow, code: str) -> None:
    index = drive_index(code)
    row.set_subtitle(fmt.DRIVES[index][2])
    if row.get_selected() != index:
        row.set_selected(index)


class QueueRow(Adw.ActionRow):
    def __init__(self, page: QueuePage, item: QueueItem, position: int, count: int) -> None:
        super().__init__()
        self.set_use_markup(False)
        self.set_title(item.label)
        self.set_subtitle(page.item_subtitle(item))
        self.item = item
        number = Gtk.Label(label=str(position + 1), width_chars=2, valign=Gtk.Align.CENTER)
        number.add_css_class("dim-label")
        number.add_css_class("numeric")
        self.add_prefix(number)

        copies_label = Gtk.Label(label="Copies", valign=Gtk.Align.CENTER)
        copies_label.add_css_class("caption")
        copies_label.add_css_class("dim-label")
        self.add_suffix(copies_label)
        copies = Gtk.SpinButton.new_with_range(1, MAX_COPIES, 1)
        copies.set_value(item.copies)
        copies.set_valign(Gtk.Align.CENTER)
        copies.set_tooltip_text("How many floppies to write of this disk")
        copies.update_property([Gtk.AccessibleProperty.LABEL], [f"Copies of {item.label}"])
        copies.connect(
            "value-changed", lambda spin: page.set_copies(item.id, spin.get_value_as_int())
        )
        self.copies = copies
        self.add_suffix(copies)
        self.up = icon_button("go-up-symbolic", "Move Up", lambda _b: page.move(item.id, -1))
        self.up.set_sensitive(position > 0)
        self.add_suffix(self.up)
        self.down = icon_button("go-down-symbolic", "Move Down", lambda _b: page.move(item.id, 1))
        self.down.set_sensitive(position < count - 1)
        self.add_suffix(self.down)
        self.add_suffix(
            icon_button("user-trash-symbolic", "Remove from Queue", lambda _b: page.remove(item.id))
        )


class QueuePage(Gtk.Stack):
    """``host`` is the main window: it provides ``backend``, ``ensure_device``,
    ``session_started``, ``session_finished``, ``queue_changed`` and ``toast``.
    """

    def __init__(self, host) -> None:
        super().__init__(transition_type=Gtk.StackTransitionType.CROSSFADE, vexpand=True)
        self._host = host
        self._session = None
        self._bridge: SessionEventsBridge | None = None
        self._session_items: list[QueueItem] = []
        self._from_queue = False
        self._finished: list[tuple[QueueItem, WriteOutcome]] = []
        self._retries = 0
        self._cancelling = False
        self.summary: SessionSummary | None = None
        self.insert_dialog: Adw.AlertDialog | None = None
        self._pending: PendingAnswer | None = None

        self._build_empty()
        self._build_list()
        self._build_running()
        self._build_summary()
        self.refresh()

    # Building

    def _build_empty(self) -> None:
        page = Adw.StatusPage(
            icon_name="view-list-symbolic",
            title="Queue Is Empty",
            description=(
                "Tick disks on the Find page and choose Add to Queue. Queued disks are "
                "written in one session, one floppy after another."
            ),
        )
        button = text_button("_Find Disks", None, style="pill")
        button.set_action_name("win.show-page")
        button.set_action_target_value(_variant("find"))
        button.set_halign(Gtk.Align.CENTER)
        page.set_child(button)
        self.add_named(page, "empty")

    def _build_list(self) -> None:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        preferences = Adw.PreferencesPage(vexpand=True)
        box.append(preferences)

        self.items_group = Adw.PreferencesGroup(title="Disks to Write")
        self.clear_button = text_button("C_lear Queue", self._on_clear, style="flat")
        self.items_group.set_header_suffix(self.clear_button)
        self.item_rows = GroupRows(self.items_group)
        preferences.add(self.items_group)

        drive_group = Adw.PreferencesGroup(
            title="Greaseweazle",
            description="Choose the drive that matches how it is cabled.",
        )
        self.drive_row = drive_row(self._on_drive_changed)
        drive_group.add(self.drive_row)
        preferences.add(drive_group)

        start_group = Adw.PreferencesGroup()
        start = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, halign=Gtk.Align.CENTER)
        self.start_button = text_button(
            "_Start Writing", self._on_start, style="suggested-action pill"
        )
        start.append(self.start_button)
        self.count_label = Gtk.Label()
        self.count_label.add_css_class("dim-label")
        self.count_label.add_css_class("caption")
        start.append(self.count_label)
        start_group.add(start)
        preferences.add(start_group)
        self.add_named(box, "list")

    def _build_running(self) -> None:
        self.running_page = Adw.StatusPage(icon_name="media-floppy-symbolic", title="Writing Disks")
        box = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=8,
            halign=Gtk.Align.CENTER,
            width_request=360,
        )
        box.set_size_request(420, -1)
        self.stage_label = Gtk.Label(xalign=0, wrap=True)
        self.stage_label.add_css_class("heading")
        box.append(self.stage_label)
        self.progress = Gtk.ProgressBar(show_text=True)
        box.append(self.progress)
        self.track_label = Gtk.Label(xalign=0, wrap=True)
        self.track_label.add_css_class("dim-label")
        self.track_label.add_css_class("numeric")
        box.append(self.track_label)
        self.retries_label = Gtk.Label(xalign=0, wrap=True, visible=False)
        self.retries_label.add_css_class("dim-label")
        box.append(self.retries_label)
        self.notes_label = Gtk.Label(
            xalign=0, wrap=True, visible=False, wrap_mode=Pango.WrapMode.WORD_CHAR
        )
        self.notes_label.add_css_class("dim-label")
        box.append(self.notes_label)
        self.cancel_button = text_button("_Cancel", self._on_cancel, style="pill")
        self.cancel_button.set_halign(Gtk.Align.CENTER)
        self.cancel_button.set_margin_top(12)
        box.append(self.cancel_button)
        self.running_page.set_child(box)
        self.add_named(self.running_page, "running")

    def _build_summary(self) -> None:
        self.summary_page = Adw.StatusPage()
        self.summary_page.add_css_class("compact")
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        self.summary_list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        self.summary_list.add_css_class("boxed-list")
        self.summary_list.update_property([Gtk.AccessibleProperty.LABEL], ["Disks written"])
        box.append(self.summary_list)
        buttons = Gtk.Box(spacing=12, halign=Gtk.Align.CENTER)
        self.retry_button = text_button("_Retry Failed", self._on_retry_failed, style="pill")
        buttons.append(self.retry_button)
        buttons.append(text_button("Copy _Report", self._on_copy_report, style="pill"))
        self.done_button = text_button("_Done", self._on_done, style="suggested-action pill")
        buttons.append(self.done_button)
        box.append(buttons)
        self.summary_page.set_child(Adw.Clamp(maximum_size=640, child=box))
        self.add_named(self.summary_page, "summary")

    # The list of queued disks

    @property
    def running(self) -> bool:
        return self._session is not None

    def item_subtitle(self, item: QueueItem) -> str:
        parts = [fmt.platform_name(item.platform)]
        if item.local is not None:
            parts.append(fmt.local_file_location(item.local))
        elif item.image_id is not None:
            parts.append("chosen dump")
        else:
            parts.append("best available dump")
        if not item.clean_virus:
            parts.append("boot block virus left in place")
        return " • ".join(part for part in parts if part)

    def refresh(self) -> None:
        """Rebuild the list from the backend's queue."""
        items = self._host.backend.queue_items()
        self._host.queue_changed(len(items))
        set_drive_row(self.drive_row, self._host.backend.settings.drive)
        if self.running or self.get_visible_child_name() == "summary":
            return
        self.item_rows.clear()
        for position, item in enumerate(items):
            self.item_rows.add(QueueRow(self, item, position, len(items)))
        floppies = sum(item.copies for item in items)
        text = fmt.plural(len(items), "disk")
        if floppies != len(items):
            text += f", {fmt.plural(floppies, 'floppy', 'floppies')}"
        self.count_label.set_text(text)
        self.set_visible_child_name("list" if items else "empty")

    def refresh_settings(self) -> None:
        set_drive_row(self.drive_row, self._host.backend.settings.drive)

    def add(self, items: Sequence[QueueItem]) -> None:
        self._host.backend.queue_add(items)
        self.refresh()

    def move(self, item_id: str, offset: int) -> None:
        self._host.backend.queue_move(item_id, offset)
        self.refresh()

    def remove(self, item_id: str) -> None:
        self._host.backend.queue_remove([item_id])
        self.refresh()

    def set_copies(self, item_id: str, copies: int) -> None:
        self._host.backend.queue_set_copies(item_id, copies)
        items = self._host.backend.queue_items()
        floppies = sum(item.copies for item in items)
        text = fmt.plural(len(items), "disk")
        if floppies != len(items):
            text += f", {fmt.plural(floppies, 'floppy', 'floppies')}"
        self.count_label.set_text(text)

    def _on_clear(self, _button) -> None:
        count = len(self._host.backend.queue_items())

        def respond(response: str) -> None:
            if response == "clear":
                self._host.backend.queue_clear()
                self.refresh()

        alert(
            self,
            "Clear the Queue?",
            f"{fmt.plural(count, 'disk')} will be taken off the queue.",
            (("cancel", "_Cancel", ""), ("clear", "C_lear Queue", "destructive")),
            respond,
            default="cancel",
        )

    def _on_drive_changed(self, code: str) -> None:
        settings = self._host.backend.settings
        if settings.drive != code:
            settings.drive = code
            self._host.backend.save_settings()
            self._host.settings_changed("drive")

    # Running a session

    def _on_start(self, _button) -> None:
        items = self._host.backend.queue_items()
        if items:
            self.start(items, from_queue=True)

    def start(self, items: Sequence[QueueItem], *, from_queue: bool) -> None:
        """Write ``items``, after checking a Greaseweazle is connected."""
        if self.running or not items:
            return
        self._host.ensure_device(lambda: self._begin(list(items), from_queue))

    def _begin(self, items: list[QueueItem], from_queue: bool) -> None:
        if self.running:
            return
        self._session_items = items
        self._from_queue = from_queue
        self._finished = []
        self._retries = 0
        self._cancelling = False
        self.summary = None
        self.running_page.set_title("Writing Disks")
        self.running_page.set_description(fmt.escape(items[0].label))
        self.stage_label.set_text("Starting")
        set_fraction(self.progress, 0.0)
        self.track_label.set_text("")
        self.retries_label.set_visible(False)
        self.notes_label.set_visible(False)
        self.cancel_button.set_sensitive(True)
        self.set_visible_child_name("running")
        self._bridge = SessionEventsBridge(self)
        try:
            self._session = self._host.backend.create_session(items, self._bridge)
        except Exception as error:  # noqa: BLE001 - shown to the user
            LOG.add("session", f"Could not start writing: {error}")
            self._session = None
            self.refresh()
            alert(self, "Could Not Start Writing", str(error))
            return
        self._host.session_started()
        LOG.add("session", f"Writing {fmt.plural(len(items), 'disk')}")
        session = self._session
        run_in_thread(session.run, self._on_session_done, self._on_session_error, name="session")

    # SessionEventsBridge target, all called on the main loop. ``index`` counts
    # from 0, as the session reports it.

    def session_stage(self, item: QueueItem, index: int, total: int, stage: str, message: str):
        self.running_page.set_title(f"Writing Disk {index + 1} of {total}")
        self.running_page.set_description(fmt.escape(item.label))
        self.stage_label.set_text(fmt.stage_text(stage, message))
        if stage in ("resolve", "find", "prepare", "write"):
            set_fraction(self.progress, 0.0)
            self.track_label.set_text("")
        if item.notes:
            self.notes_label.set_text("\n".join(item.notes))
            self.notes_label.set_visible(True)
        else:
            self.notes_label.set_visible(False)

    def session_download(self, item: QueueItem, done: int, total: int | None) -> None:
        text = fmt.download_text(done, total)
        self.stage_label.set_text(text)
        set_fraction(self.progress, done / total if total else None, text)

    def session_progress(self, item: QueueItem, progress: WriteProgress) -> None:
        set_fraction(self.progress, progress.fraction)
        self.track_label.set_text(fmt.track_text(progress))
        if progress.retry:
            self._retries += 1
            self.retries_label.set_text(
                f"{fmt.plural(self._retries, 'retry', 'retries')} so far on this disk"
            )
            self.retries_label.set_visible(True)

    def session_ask_insert(
        self, item: QueueItem, index: int, total: int, reason: str, pending: PendingAnswer
    ) -> None:
        if self._cancelling:
            pending.answer("stop")
            return
        self._retries = 0
        self.retries_label.set_visible(False)
        self.running_page.set_title(f"Writing Disk {index + 1} of {total}")
        self.running_page.set_description(fmt.escape(item.label))
        self.stage_label.set_text("Waiting for the disk")
        self._pending = pending
        drive = self._host.backend.settings.drive
        self.insert_dialog = alert(
            self,
            fmt.insert_heading(item.label),
            fmt.insert_body(index + 1, total, drive, reason),
            (
                ("stop", "_Stop", "destructive"),
                ("skip", "S_kip", ""),
                ("write", "_Write", "suggested"),
            ),
            self._insert_answered,
            default="write",
            close="stop",
        )

    def _insert_answered(self, response: str) -> None:
        pending = self._pending
        if pending is None:
            return  # already answered, for example by Cancel
        self._pending = None
        self.insert_dialog = None
        pending.answer(response)
        if response == "stop":
            self._cancelling = True
            self.cancel_button.set_sensitive(False)
            self.stage_label.set_text("Stopping")

    def answer_insert_prompt(self, response: str) -> bool:
        """Answer the open insert prompt as if a button was pressed (used by tests)."""
        dialog = self.insert_dialog
        if dialog is None:
            return False
        self._insert_answered(response)
        dialog.force_close()
        return True

    def session_item_finished(self, item: QueueItem, outcome: WriteOutcome) -> None:
        self._finished.append((item, outcome))
        if not outcome.succeeded and outcome.status not in (
            WriteStatus.SKIPPED,
            WriteStatus.CANCELLED,
        ):
            self._host.toast(f"{item.label}: {fmt.outcome_text(outcome)}")

    def _on_cancel(self, _button) -> None:
        def respond(response: str) -> None:
            if response == "stop":
                self.cancel_session()

        alert(
            self,
            "Stop Writing?",
            "The disk being written now will be left incomplete. Disks not yet written are "
            "left in the queue.",
            (("keep", "_Keep Writing", ""), ("stop", "_Stop Writing", "destructive")),
            respond,
            default="keep",
            close="keep",
        )

    def cancel_session(self) -> None:
        if self._session is None:
            return
        self._cancelling = True
        self.cancel_button.set_sensitive(False)
        self.stage_label.set_text("Stopping safely…")
        if self.insert_dialog is not None:
            self.insert_dialog.force_close()
            self.insert_dialog = None
        if self._pending is not None:
            self._pending.answer("stop")
            self._pending = None
        if self._bridge is not None:
            self._bridge.stop()
        try:
            self._session.cancel()
        except Exception as error:  # noqa: BLE001
            LOG.add("session", f"Cancel failed: {error}")

    def _on_session_error(self, error: BaseException) -> None:
        now = datetime.now().astimezone().isoformat(timespec="seconds")
        done = {id(item): outcome for item, outcome in self._finished}
        items = []
        for item in self._session_items:
            outcome = done.get(id(item)) or WriteOutcome(
                WriteStatus.FAILED,
                "Writing stopped because of an unexpected error.",
                diagnostic=f"{type(error).__name__}: {error}",
            )
            items.append((item.label, outcome, item.source_used))
        self._on_session_done(
            SessionSummary(now, now, self._host.backend.settings.drive, tuple(items))
        )

    def _on_session_done(self, summary: SessionSummary) -> None:
        self._session = None
        self._bridge = None
        if self.insert_dialog is not None:
            self.insert_dialog.force_close()
            self.insert_dialog = None
        self.summary = summary
        if self._from_queue:
            written = [
                item.id for item in self._session_items if item.outcome and item.outcome.succeeded
            ]
            if written:
                self._host.backend.queue_remove(written)
        self._show_summary(summary)
        self._host.session_finished(summary)
        self.refresh()

    # Summary

    def _show_summary(self, summary: SessionSummary) -> None:
        done = summary.count(WriteStatus.VERIFIED, WriteStatus.WRITTEN)
        total = len(summary.items)
        self.summary_page.set_title(fmt.session_title(summary))
        self.summary_page.set_description(fmt.escape(fmt.session_headline(summary)))
        self.summary_page.set_icon_name(
            "emblem-ok-symbolic" if total and done == total else "dialog-warning-symbolic"
        )
        child = self.summary_list.get_first_child()
        while child is not None:
            following = child.get_next_sibling()
            self.summary_list.remove(child)
            child = following
        for label, outcome, source in summary.items:
            row = plain_row(title=label, subtitle=fmt.outcome_subtitle(outcome, source))
            row.set_subtitle_lines(0)  # every note, however many
            row.add_prefix(
                status_icon(
                    fmt.STATUS_ICONS.get(outcome.status, "dialog-question-symbolic"),
                    fmt.STATUS_STYLE.get(outcome.status, "dim-label"),
                    fmt.STATUS_TEXT.get(outcome.status, ""),
                )
            )
            self.summary_list.append(row)
        self.retry_button.set_visible(bool(self._failed_items()))
        self.set_visible_child_name("summary")
        self.done_button.grab_focus()

    def _failed_items(self) -> list[QueueItem]:
        failed = []
        for item in self._session_items:
            outcome = item.outcome
            if outcome is None or (not outcome.succeeded and outcome.status != WriteStatus.SKIPPED):
                failed.append(item)
        return failed

    def _on_retry_failed(self, _button) -> None:
        failed = [fresh_copy(item) for item in self._failed_items()]
        if not failed:
            return
        from_queue = self._from_queue
        if from_queue:
            # The failed disks are still in the queue; write those entries again.
            queued = {item.id for item in self._host.backend.queue_items()}
            failed = [item for item in self._failed_items() if item.id in queued] or failed
            for item in failed:
                item.outcome = None
                item.source_used = ""
                item.notes = []
        self.start(failed, from_queue=from_queue)

    def _on_copy_report(self, _button) -> None:
        if self.summary is None:
            return
        copy_text(self, self._host.backend.report_text(self.summary))
        toast(self, "Report copied")

    def _on_done(self, _button) -> None:
        self.summary = None
        self.set_visible_child_name("empty")
        self.refresh()


def _variant(text: str):
    from gi.repository import GLib

    return GLib.Variant("s", text)

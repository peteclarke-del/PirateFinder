"""The History page: past writing sessions and their summaries."""

from __future__ import annotations

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gtk  # noqa: E402

from ..models import SessionSummary, WriteStatus  # noqa: E402
from . import formatting as fmt  # noqa: E402
from .bridge import run_in_thread  # noqa: E402
from .widgets import GroupRows, copy_text, icon_button, plain_row, status_icon  # noqa: E402

MAX_SESSIONS = 200
_SUCCESS = (WriteStatus.VERIFIED, WriteStatus.WRITTEN)


class HistoryPage(Gtk.Stack):
    """``host`` is the main window: ``backend`` and ``toast``."""

    def __init__(self, host) -> None:
        super().__init__(transition_type=Gtk.StackTransitionType.CROSSFADE, vexpand=True)
        self._host = host
        self.sessions: list[SessionSummary] = []
        self._generation = 0

        empty = Adw.StatusPage(
            icon_name="document-open-recent-symbolic",
            title="No Writing History",
            description="Sessions appear here after you write disks.",
        )
        self.add_named(empty, "empty")

        self.page = Adw.PreferencesPage()
        self.group = Adw.PreferencesGroup(title="Past Sessions")
        self.rows = GroupRows(self.group)
        self.page.add(self.group)
        self.add_named(self.page, "sessions")
        self.connect("map", lambda _page: self.refresh())

    def refresh(self) -> None:
        backend = self._host.backend
        self._generation += 1
        generation = self._generation

        def shown(sessions: list[SessionSummary]) -> None:
            if generation == self._generation:
                self.show_sessions(sessions)

        run_in_thread(lambda: backend.history()[:MAX_SESSIONS], shown, name="history")

    def show_sessions(self, sessions: list[SessionSummary]) -> None:
        self.sessions = list(sessions)
        self.rows.clear()
        for summary in self.sessions:
            self.rows.add(self._session_row(summary))
        self.set_visible_child_name("sessions" if self.sessions else "empty")

    def _session_row(self, summary: SessionSummary) -> Adw.ExpanderRow:
        row = plain_row(
            Adw.ExpanderRow,
            title=fmt.format_timestamp(summary.started) or "Unknown time",
            subtitle=f"{fmt.session_headline(summary)} • drive {summary.drive}",
        )
        total = len(summary.items)
        done = summary.count(*_SUCCESS)
        row.add_prefix(
            status_icon(
                "emblem-ok-symbolic" if total and done == total else "dialog-warning-symbolic",
                "success" if total and done == total else "warning",
            )
        )
        row.add_suffix(
            icon_button(
                "edit-copy-symbolic",
                "Copy Report",
                lambda _button, s=summary: self._copy_report(s),
            )
        )
        for label, outcome, source in summary.items:
            item = plain_row(title=label)
            item.set_subtitle_lines(3)
            details = fmt.outcome_details(outcome, source)
            text = fmt.outcome_text(outcome)
            item.set_subtitle(f"{text}\n{details}" if details else text)
            item.add_prefix(
                status_icon(
                    fmt.STATUS_ICONS.get(outcome.status, "dialog-question-symbolic"),
                    fmt.STATUS_STYLE.get(outcome.status, "dim-label"),
                    fmt.STATUS_TEXT.get(outcome.status, ""),
                )
            )
            row.add_row(item)
        return row

    def _copy_report(self, summary: SessionSummary) -> None:
        copy_text(self, self._host.backend.report_text(summary))
        self._host.toast("Report copied")

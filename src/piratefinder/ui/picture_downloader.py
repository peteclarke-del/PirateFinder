"""Download All Pictures: fetching the details pane's pictures ahead of time.

The window keeps one ``PictureDownloader``, so a download carries on when
Preferences is closed, shows again when it is reopened, and cannot be started
twice. It stops when PirateFinder closes; the next start carries on from the
pictures already in the cache.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass

from ..jobs.cancellation import Cancellation
from ..models import Platform
from ..online.prefetch import PictureCount, PictureProgress, PictureSummary
from . import formatting as fmt
from .bridge import Latest, run_in_thread
from .log import LOG

# The platform choices offered, as (label, platforms); no platforms is every one.
PLATFORM_CHOICES: tuple[tuple[str, tuple[Platform, ...]], ...] = (
    ("Atari ST and Amiga", ()),
    ("Atari ST", (Platform.ATARI_ST,)),
    ("Amiga", (Platform.AMIGA,)),
)
SWITCHED_OFF = (
    "Switch on Online Downloads and Download Screenshots and Background Information to "
    "download pictures"
)


@dataclass(frozen=True, slots=True)
class PictureState:
    # "idle", "counting", "ready", "downloading", "done", "stopped" or "failed"
    phase: str
    message: str = ""
    fraction: float | None = None
    count: PictureCount | None = None

    @property
    def busy(self) -> bool:
        return self.phase in ("counting", "downloading")


def platform_label(platforms: Sequence[Platform]) -> str:
    for label, choice in PLATFORM_CHOICES:
        if tuple(platforms) == choice:
            return label
    return ", ".join(str(platform) for platform in platforms)


def count_text(count: PictureCount) -> str:
    """ "1,234 of 78,241 pictures are on this computer (35 MB). ..." """
    if not count.total:
        return "The catalogue lists no pictures of these discs."
    size = f" ({fmt.human_size(count.size)})" if count.cached else ""
    text = f"{count.cached:,} of {count.total:,} pictures are on this computer{size}."
    if count.remaining:
        text += (
            f" The other {count.remaining:,} take {fmt.duration_text(count.seconds)}, "
            "at one a second from each site"
        )
        estimate = count.estimated_size
        text += f", and about {fmt.human_size(estimate)}." if estimate else "."
    return text


def summary_text(summary: PictureSummary) -> str:
    if summary.stopped:
        text = f"Stopped after {summary.done:,} of {summary.total:,} pictures."
    else:
        text = f"Finished: all {summary.total:,} pictures were checked."
    text += f" {summary.fetched:,} were downloaded"
    if summary.unavailable:
        text += f", and {summary.unavailable:,} are no longer on their sites"
    return text + "."


class PictureDownloader:
    def __init__(self, host) -> None:
        self._host = host
        self.state = PictureState("idle")
        self._listeners: list[Callable[[PictureState], None]] = []
        self._cancel: Cancellation | None = None
        self.platforms: tuple[Platform, ...] = ()

    def subscribe(self, listener: Callable[[PictureState], None]) -> None:
        self._listeners.append(listener)

    def unsubscribe(self, listener: Callable[[PictureState], None]) -> None:
        if listener in self._listeners:
            self._listeners.remove(listener)

    def _set(self, state: PictureState) -> None:
        self.state = state
        for listener in list(self._listeners):
            listener(state)

    def count(self, platforms: Sequence[Platform] = (), *, after: str = "") -> None:
        """Count the pictures of ``platforms`` on this computer; ``after`` leads the message."""
        if self.state.busy:
            return
        self.platforms = tuple(platforms)
        chosen = self.platforms
        self._set(PictureState("counting", "Counting the pictures on this computer"))

        def counted(count: PictureCount) -> None:
            if chosen != self.platforms or self.state.phase != "counting":
                return
            message = count_text(count)
            if after:
                message = f"{after} {message}"
            self._set(PictureState("ready", message, count=count))

        def failed(error: BaseException) -> None:
            self._set(PictureState("failed", f"The pictures could not be counted: {error}"))

        backend = self._host.backend
        run_in_thread(lambda: backend.picture_count(chosen), counted, failed, name="picture-count")

    def start(self) -> None:
        if self.state.busy:
            return
        backend = self._host.backend
        if not backend.media_enabled:
            self._set(PictureState("ready", SWITCHED_OFF, count=self.state.count))
            return
        platforms = self.platforms
        label = platform_label(platforms)
        self._cancel = Cancellation()
        cancel = self._cancel
        self._set(PictureState("downloading", "Starting", 0.0, self.state.count))
        LOG.add("pictures", "Download All Pictures started")

        def progress(step: PictureProgress) -> None:
            if self.state.phase == "downloading":
                fraction = step.done / step.total if step.total else None
                message = (
                    f"{label}: {step.done:,} of {step.total:,} pictures checked, "
                    f"{step.fetched:,} downloaded"
                )
                self._set(PictureState("downloading", message, fraction, self.state.count))

        latest = Latest(progress)

        def finished(summary: PictureSummary) -> None:
            self._cancel = None
            text = summary_text(summary)
            LOG.add("pictures", text)
            self._set(PictureState("stopped" if summary.stopped else "done", text))
            self.count(platforms, after=text)

        def failed(error: BaseException) -> None:
            self._cancel = None
            message = f"The pictures could not be downloaded: {error}"
            LOG.add("pictures", message)
            self._set(PictureState("failed", message))

        run_in_thread(
            lambda: backend.download_pictures(platforms, latest.post, cancel),
            finished,
            failed,
            name="picture-download",
        )

    def stop(self) -> None:
        if self._cancel is not None:
            self._cancel.cancel()

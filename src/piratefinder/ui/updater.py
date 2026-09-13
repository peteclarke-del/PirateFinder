"""Checking for and installing a newer catalogue, shared by every place that offers it.

The main menu, the "No Catalogue Installed" page, the start-up check and
Preferences all drive one ``CatalogueUpdater``, so an update in progress is
shown the same way wherever the user looks, and cannot be started twice.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from ..jobs.cancellation import Cancellation
from . import formatting as fmt
from .backend import UpdateOffer
from .bridge import Latest, run_in_thread
from .log import LOG


@dataclass(frozen=True, slots=True)
class UpdateState:
    phase: str  # "idle", "checking", "current", "available", "installing", "done", "failed"
    message: str = ""
    fraction: float | None = None
    offer: UpdateOffer | None = None

    @property
    def busy(self) -> bool:
        return self.phase in ("checking", "installing")


class CatalogueUpdater:
    def __init__(self, host) -> None:
        self._host = host
        self.state = UpdateState("idle")
        self._listeners: list[Callable[[UpdateState], None]] = []
        self._cancel: Cancellation | None = None

    def subscribe(self, listener: Callable[[UpdateState], None]) -> None:
        self._listeners.append(listener)

    def unsubscribe(self, listener: Callable[[UpdateState], None]) -> None:
        if listener in self._listeners:
            self._listeners.remove(listener)

    def _set(self, state: UpdateState) -> None:
        self.state = state
        for listener in list(self._listeners):
            listener(state)

    def check(self, on_offer: Callable[[UpdateOffer], None] | None = None) -> None:
        """Look for a newer catalogue; ``on_offer`` is called when there is one."""
        if self.state.busy:
            return
        self._set(UpdateState("checking", "Checking for a newer catalogue"))
        backend = self._host.backend

        def found(offer: UpdateOffer | None) -> None:
            if offer is None:
                info = backend.catalogue_info()
                built = fmt.format_timestamp(info.built_at) if info.available else ""
                message = "The catalogue is up to date"
                if built:
                    message += f" (built {built})"
                self._set(UpdateState("current", message))
                return
            size = f", {fmt.human_size(offer.size)}" if offer.size else ""
            self._set(
                UpdateState(
                    "available",
                    f"A catalogue built {fmt.format_timestamp(offer.built_at)} is available{size}",
                    offer=offer,
                )
            )
            if on_offer is not None:
                on_offer(offer)

        def failed(error: BaseException) -> None:
            # Never "up to date": the check did not happen.
            message = f"Could not check for a newer catalogue: {error}"
            LOG.add("catalogue", message)
            self._set(UpdateState("failed", message))

        run_in_thread(backend.check_for_update, found, failed, name="catalogue-check")

    def install(self, offer: UpdateOffer) -> None:
        if self.state.busy:
            return
        if self._host.session_running():
            # The session reads the catalogue; it is not replaced under it.
            message = "The catalogue can be updated once the disks have been written"
            self._set(UpdateState("available", message, offer=offer))
            return
        self._cancel = Cancellation()
        cancel = self._cancel
        self._set(UpdateState("installing", "Downloading the catalogue", 0.0, offer))
        backend = self._host.backend

        def progress(done: int, total: int | None) -> None:
            fraction = done / total if total else None
            if self.state.phase == "installing":
                self._set(
                    UpdateState("installing", fmt.download_text(done, total), fraction, offer)
                )

        latest = Latest(progress)

        def done(_result) -> None:
            self._cancel = None
            LOG.add("catalogue", f"Installed the catalogue built {offer.built_at}")
            self._set(UpdateState("done", "The catalogue was updated"))
            self._host.catalogue_updated()

        def failed(error: BaseException) -> None:
            self._cancel = None
            message = (
                "The update was cancelled" if cancel.cancelled else f"The update failed: {error}"
            )
            self._set(UpdateState("failed", message, offer=offer))

        run_in_thread(
            lambda: backend.install_update(offer, latest.post, cancel),
            done,
            failed,
            name="catalogue-install",
        )

    def cancel(self) -> None:
        if self._cancel is not None:
            self._cancel.cancel()

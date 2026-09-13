"""Reading the repository's GitHub releases, for the catalogue and application updates.

The repository is public, so the release list and the assets are read without
signing in. A reply that cannot be fetched or read raises UpdateError with
the reason as a sentence; neither update ever reads a failed check as "up to
date".
"""

from __future__ import annotations

import json
import urllib.parse
from typing import Any

from .http import Downloader, DownloadError

GITHUB_JSON = {"Accept": "application/vnd.github+json"}


class UpdateError(RuntimeError):
    """The update could not be checked or installed; the message is for the user."""


class UpdateCancelled(UpdateError):
    """The user cancelled the update."""


def _host(url: str) -> str:
    return urllib.parse.urlsplit(url).netloc or url


def _refused(url: str, reply: Any, what: str) -> UpdateError:
    message = reply.get("message") if isinstance(reply, dict) else None
    reason = f"{_host(url)} did not send {what}"
    return UpdateError(f"{reason}: {message}." if message else f"{reason}.")


def release_list(url: str, downloader: Downloader) -> list[Any]:
    """Every release in the list at ``url``, newest first as GitHub sends them."""
    try:
        releases = downloader.get_json(url, headers=GITHUB_JSON)
    except DownloadError as error:
        raise UpdateError(str(error)) from error
    if not isinstance(releases, list):
        raise _refused(url, releases, "a list of releases")
    return releases


def latest_release(url: str, downloader: Downloader) -> dict[str, Any] | None:
    """The release GitHub marks as the latest, or None when none is published.

    GitHub never marks a draft or a prerelease as the latest release.
    """
    try:
        reply = downloader.get_reply(url, headers=GITHUB_JSON)
    except DownloadError as error:
        raise UpdateError(str(error)) from error
    if reply.status == 404:
        return None
    if reply.status != 200:
        raise UpdateError(f"{_host(url)} sent an unexpected reply (HTTP {reply.status}).")
    try:
        release = json.loads(reply.data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as error:
        raise UpdateError(f"The reply from {_host(url)} could not be read.") from error
    if not isinstance(release, dict) or "tag_name" not in release:
        raise _refused(url, release, "a release")
    return release

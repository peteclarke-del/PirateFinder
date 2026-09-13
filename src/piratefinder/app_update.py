"""Check for, download and install a newer release of PirateFinder itself.

The check runs only when the user asks for it, from the About window. It
reads the release GitHub marks as the latest, which is always the newest
application release (catalogue releases are never marked as the latest), and
compares its tag, ``vX.Y.Z``, with ``__version__``.

A release carries one package for each supported system,
``PirateFinder_<version>_<distro>_<arch>.deb``, and a ``SHA256SUMS`` file for
all of them. An installed package records the distribution and architecture
it was built for in ``package-target`` beside the application, so the update
takes the package made for the same system. The package is downloaded to the
cache folder, checked against ``SHA256SUMS`` and installed with
``pkexec apt-get install``, which asks for the user's password. A copy run
from the source tree has no ``package-target`` and cannot update itself; the
release page is offered instead.
"""

from __future__ import annotations

import hashlib
import re
import shlex
import shutil
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import __version__, paths
from .branding import APPLICATION_NAME, RELEASES_API
from .online.http import DownloadCancelled, Downloader, DownloadError
from .online.releases import UpdateCancelled, UpdateError, latest_release

LATEST_URL = f"{RELEASES_API}/latest"
SUMS_NAME = "SHA256SUMS"
# The file build-deb.sh writes beside the installed application.
PACKAGE_TARGET = Path(__file__).resolve().parents[1] / "package-target"
_TAG = re.compile(r"^v(\d+)\.(\d+)\.(\d+)$")
_SUM_LINE = re.compile(r"^([0-9a-fA-F]{64})\s+\*?(\S.*)$")
# pkexec's exit statuses when the password prompt is dismissed or refused.
_PKEXEC_DISMISSED = 126
_PKEXEC_REFUSED = 127
NOTES_LIMIT = 2000

Progress = Callable[[int, int | None], None]


@dataclass(frozen=True, slots=True)
class PackageTarget:
    """The system an installed package was built for: "ubuntu-24.04", "amd64"."""

    distro: str
    arch: str

    def package_name(self, version: str) -> str:
        return f"{APPLICATION_NAME}_{version}_{self.distro}_{self.arch}.deb"


@dataclass(frozen=True, slots=True)
class AppRelease:
    """A published application release newer than the running version."""

    version: str
    tag: str
    name: str
    page_url: str
    notes: str = ""
    package_name: str = ""  # empty when the release has no package for this system
    package_url: str = ""
    package_size: int | None = None
    sums_url: str = ""

    @property
    def installable(self) -> bool:
        return bool(self.package_url and self.sums_url)


def installed_target(path: Path = PACKAGE_TARGET) -> PackageTarget | None:
    """The system this package was built for, or None when run from the source tree."""
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    values = {}
    for line in text.splitlines():
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip()
    distro, arch = values.get("distro", ""), values.get("arch", "")
    return PackageTarget(distro, arch) if distro and arch else None


def parse_version(text: str) -> tuple[int, int, int] | None:
    """(0, 2, 0) for "v0.2.0" or "0.2.0"; None for anything else, such as a catalogue tag."""
    match = _TAG.match(text if text.startswith("v") else f"v{text}")
    return tuple(int(part) for part in match.groups()) if match else None  # type: ignore[return-value]


def is_newer(tag: str, current: str = __version__) -> bool:
    theirs, ours = parse_version(tag), parse_version(current)
    return theirs is not None and (ours is None or theirs > ours)


def shorten(notes: str, limit: int = NOTES_LIMIT) -> str:
    notes = notes.strip()
    if len(notes) <= limit:
        return notes
    return notes[:limit].rstrip() + "\n\nThe rest is on the release page."


def release_from(
    release: dict[str, Any], target: PackageTarget | None, current: str = __version__
) -> AppRelease | None:
    """The release as an AppRelease when it is newer than ``current``, else None.

    Raises UpdateError when the latest release is not an application release,
    which would be a mistake in publishing it.
    """
    tag = str(release.get("tag_name", ""))
    version = parse_version(tag)
    if version is None:
        raise UpdateError(
            f"The latest release on GitHub, {tag or 'without a tag'}, "
            "is not an application release."
        )
    if not is_newer(tag, current):
        return None
    text = ".".join(str(part) for part in version)
    assets = {
        str(asset.get("name", "")): asset
        for asset in release.get("assets") or []
        if isinstance(asset, dict)
    }
    wanted = target.package_name(text) if target else ""
    package, sums = assets.get(wanted), assets.get(SUMS_NAME)
    size = package.get("size") if package else None
    return AppRelease(
        version=text,
        tag=tag,
        name=str(release.get("name") or f"{APPLICATION_NAME} {text}"),
        page_url=str(release.get("html_url") or ""),
        notes=shorten(str(release.get("body") or "")),
        package_name=wanted if package else "",
        package_url=str(package.get("browser_download_url", "")) if package else "",
        package_size=size if isinstance(size, int) else None,
        sums_url=str(sums.get("browser_download_url", "")) if sums else "",
    )


def check(
    target: PackageTarget | None,
    *,
    url: str = LATEST_URL,
    current: str = __version__,
    downloader: Downloader | None = None,
) -> AppRelease | None:
    """A newer release than ``current``, or None when this is the newest.

    Raises UpdateError, with the reason, when the check cannot be made.
    """
    downloader = downloader or Downloader(timeout=20.0, retries=1)
    release = latest_release(url, downloader)
    if release is None:
        raise UpdateError("No application release has been published on GitHub yet.")
    return release_from(release, target, current)


def published_sum(sums: str, name: str) -> str:
    """The SHA-256 ``SHA256SUMS`` gives for ``name``, or ""."""
    for line in sums.splitlines():
        match = _SUM_LINE.match(line.strip())
        if match and match.group(2).strip() == name:
            return match.group(1).lower()
    return ""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def download_folder() -> Path:
    return paths.cache_dir() / "updates"


def download(
    release: AppRelease,
    progress: Progress | None = None,
    cancel: object | None = None,
    *,
    folder: Path | None = None,
    downloader: Downloader | None = None,
) -> Path:
    """Download the release's package for this system and check it against SHA256SUMS."""
    if not release.installable:
        raise UpdateError(f"{release.name} has no package for this system.")
    downloader = downloader or Downloader(timeout=60.0)
    folder = folder or download_folder()
    try:
        sums = downloader.get_bytes(release.sums_url, max_bytes=64 * 1024, cancel=cancel)
        expected = published_sum(sums.decode("utf-8", "replace"), release.package_name)
        if not expected:
            raise UpdateError(f"{SUMS_NAME} in {release.name} has no line for the package.")
        package = downloader.download(
            release.package_url, folder / release.package_name, progress=progress, cancel=cancel
        )
    except DownloadCancelled as error:
        raise UpdateCancelled("The update was cancelled.") from error
    except DownloadError as error:
        raise UpdateError(str(error)) from error
    if _sha256(package) != expected:
        package.unlink(missing_ok=True)
        raise UpdateError(
            "The downloaded package does not match its published checksum, so it was not "
            "installed. Try again."
        )
    return package


def install_command(package: Path) -> list[str] | None:
    """The command that installs ``package`` with the user's password, or None without pkexec."""
    pkexec, apt_get = shutil.which("pkexec"), shutil.which("apt-get")
    if not (pkexec and apt_get):
        return None
    return [pkexec, apt_get, "install", "--yes", str(package)]


def manual_command(package: Path) -> str:
    """The command a user can run in a terminal instead."""
    return f"sudo apt install {shlex.quote(str(package))}"


def install(package: Path, *, run: Callable[..., Any] = subprocess.run) -> None:
    """Install the downloaded package, then remove the download.

    Raises UpdateCancelled when the password prompt is dismissed, and
    UpdateError with the reason and a command to run by hand otherwise.

    There is no time limit. pkexec waits for as long as the password prompt
    is open, and once it is answered apt runs as root, where this process
    cannot stop it: giving up would report a failure while apt went on to
    install the package.
    """
    command = install_command(package)
    by_hand = f"Install it in a terminal with: {manual_command(package)}"
    if command is None:
        raise UpdateError(
            f"pkexec is not installed, so the package cannot be installed here. {by_hand}"
        )
    try:
        result = run(command, capture_output=True, text=True, check=False)
    except OSError as error:
        raise UpdateError(f"The package could not be installed: {error}. {by_hand}") from error
    if result.returncode == _PKEXEC_DISMISSED:
        raise UpdateCancelled("The password prompt was dismissed, so nothing was installed.")
    if result.returncode == _PKEXEC_REFUSED:
        raise UpdateError(f"The system did not allow the installation. {by_hand}")
    if result.returncode != 0:
        lines = [line for line in (result.stderr or result.stdout or "").splitlines() if line]
        reason = lines[-1] if lines else f"apt-get stopped with status {result.returncode}"
        raise UpdateError(f"The package could not be installed: {reason}. {by_hand}")
    package.unlink(missing_ok=True)

"""The SPS Decoder Library (CAPSImg, libcapsimage) that gw needs to write IPF images.

gw 1.23 opens the library with ctypes by its soname, ``libcapsimage.so.5``
first (``greaseweazle/image/caps.py``, ``open_libcaps``), so the dynamic
loader of the gw process has to find it. The loader reads
``LD_LIBRARY_PATH`` when a process starts, so a folder added to it inside
PirateFinder would come too late; it has to be in the environment gw is
started with (``client.gw_environment``).

The library cannot be shipped with PirateFinder: its licence allows only
non-commercial use and redistribution. Instead PirateFinder offers, after
showing the licence, to download FS-UAE's build for this computer and install
it for the user as ``~/.local/share/piratefinder/caps/libcapsimage.so.5``.
Every gw run then has that folder at the front of ``LD_LIBRARY_PATH``.

A library installed by other means, such as a distribution package or one
built from the SPS source, is used when the loader can find it: through
``ctypes.util.find_library``, or under one of the names gw tries in the
loader's default folders or in the ``LD_LIBRARY_PATH`` PirateFinder was
started with, which gw inherits.

The builds are pinned in ``data/caps/capsimg.toml`` with their addresses,
sizes and SHA-256 checksums, the path of the library inside each archive and
the checksum of the library itself. An install checks the archive's size and
checksum before anything is read from it, reads only the pinned member (no
path from the archive is used on disk), checks the library's checksum, that
it is an ELF shared object for this Python's processor, and that it loads
and starts (``CAPSInit``) in a separate process, and then moves it into place
with a rename.
"""

from __future__ import annotations

import contextlib
import ctypes.util
import functools
import hashlib
import json
import lzma
import os
import platform
import re
import signal
import struct
import subprocess
import sys
import sysconfig
import tarfile
import tempfile
import tomllib
import urllib.parse
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

from .. import paths
from .runner import minimal_environment

DATA_DIR = Path(__file__).resolve().parent.parent / "data" / "caps"
BUILDS_FILE = DATA_DIR / "capsimg.toml"
LIBRARY_NAME = "libcapsimage.so.5"
#: The names gw 1.23 tries on Linux, in its order.
GW_NAMES = (
    "libcapsimage.so.5",
    "libcapsimage.so.5.1",
    "libcapsimage.so.4",
    "libcapsimage.so.4.2",
    "libcapsimage.so",
)
#: Folders the dynamic loader searches without being told, the multiarch ones first.
_TRIPLET = sysconfig.get_config_var("MULTIARCH") or ""
DEFAULT_FOLDERS = (
    *((Path("/lib") / _TRIPLET, Path("/usr/lib") / _TRIPLET) if _TRIPLET else ()),
    Path("/lib"),
    Path("/usr/lib"),
    Path("/lib64"),
    Path("/usr/lib64"),
)
LOAD_TIMEOUT = 30
_INSTALL_INFO = "install.json"

# ELF header fields.
_ELF_MAGIC = b"\x7fELF"
_ET_DYN = 3
_EM_ARM = 40
_EF_ARM_ABI_FLOAT_HARD = 0x400

# Run in a separate Python by load_check: load the library by its path, start
# it, and print its version.
_LOAD_CHECK = """\
import ctypes, sys
library = ctypes.CDLL(sys.argv[1])
if library.CAPSInit() != 0:
    sys.exit("CAPSInit failed")
class Version(ctypes.Structure):
    _fields_ = [(name, ctypes.c_uint) for name in ("type", "release", "revision", "flag")]
version = Version()
library.CAPSGetVersionInfo(ctypes.byref(version), 0)
library.CAPSExit()
print(f"{version.release}.{version.revision}")
"""


class CapsError(RuntimeError):
    """IPF support could not be installed or removed; the message is a sentence for the user."""


class CapsState(StrEnum):
    SYSTEM = "system"  # the loader finds a copy installed by other means
    INSTALLED = "installed"  # PirateFinder installed it
    MISSING = "missing"  # not installed; a build exists for this computer
    UNSUPPORTED = "unsupported"  # not installed, and no build exists for this computer


@dataclass(frozen=True, slots=True)
class CapsBuild:
    """One pinned build: where it is downloaded from and what it must be."""

    version: str
    machines: tuple[str, ...]  # platform.machine() values that can run it
    description: str
    url: str
    size: int
    sha256: str
    member: str  # the library's path inside the archive
    library_size: int
    library_sha256: str

    @property
    def host(self) -> str:
        return urllib.parse.urlsplit(self.url).netloc


@dataclass(frozen=True, slots=True)
class CapsStatus:
    """Whether gw can write IPF images, and where its library comes from."""

    state: CapsState
    version: str = ""  # the release PirateFinder installed
    path: str = ""  # the library gw loads: the installed file, or the one found on the system
    machine: str = ""  # this computer's processor, as ``machine()`` names it
    build: CapsBuild | None = None  # the build PirateFinder can install here

    @property
    def usable(self) -> bool:
        return self.state in (CapsState.SYSTEM, CapsState.INSTALLED)

    @property
    def problem(self) -> str:
        """Why an IPF image cannot be written, in a sentence; "" when it can."""
        needed = (
            "Writing an IPF image needs the SPS Decoder Library (libcapsimage), which is not "
            "installed."
        )
        if self.state is CapsState.MISSING:
            return (
                f"{needed} Install it with IPF Support in Preferences, on the Greaseweazle "
                "page, or choose another dump of this disk."
            )
        if self.state is CapsState.UNSUPPORTED:
            return (
                f"{needed} PirateFinder has no build of it for this computer's processor "
                f"({self.machine}), so install it by other means, such as from the SPS source, "
                "or choose another dump of this disk."
            )
        return ""


# Pinned builds -----------------------------------------------------------------


@functools.cache
def _pinned() -> dict[str, Any]:
    with BUILDS_FILE.open("rb") as handle:
        return tomllib.load(handle)


@functools.cache
def builds() -> tuple[CapsBuild, ...]:
    """The builds pinned in ``data/caps/capsimg.toml``."""
    data = _pinned()
    return tuple(
        CapsBuild(
            version=str(data["version"]),
            machines=tuple(str(name) for name in entry["machines"]),
            description=str(entry["description"]),
            url=str(entry["url"]),
            size=int(entry["size"]),
            sha256=str(entry["sha256"]).lower(),
            member=str(entry["member"]),
            library_size=int(entry["library_size"]),
            library_sha256=str(entry["library_sha256"]).lower(),
        )
        for entry in data["build"]
    )


def licence_text() -> str:
    """The licence of the SPS Decoder Library, shown before it is downloaded."""
    return (DATA_DIR / str(_pinned()["licence"])).read_text(encoding="utf-8")


def machine() -> str:
    """This computer's processor as ``platform.machine()`` names it, for the running Python.

    A 32-bit Python on a 64-bit kernel can load only 32-bit libraries, but
    the kernel reports its own processor: aarch64 then counts as armv8l, the
    name the kernel gives a 32-bit process on such a processor, and x86_64 as
    i686.
    """
    name = platform.machine().lower()
    if struct.calcsize("P") == 4:
        if name in ("aarch64", "arm64"):
            return "armv8l"
        if name in ("x86_64", "amd64"):
            return "i686"
    return name


def build_for_machine(name: str | None = None) -> CapsBuild | None:
    """The pinned build for ``name`` (this computer by default), or None."""
    name = machine() if name is None else name
    return next((build for build in builds() if name in build.machines), None)


def supported_machines() -> tuple[str, ...]:
    return tuple(name for build in builds() for name in build.machines)


# Where the library is ----------------------------------------------------------


def folder() -> Path:
    """The folder PirateFinder installs the library in, and adds to gw's library path."""
    return paths.data_dir() / "caps"


def installed_library() -> Path | None:
    """The library PirateFinder installed, when it is there."""
    path = folder() / LIBRARY_NAME
    return path if path.is_file() else None


def system_library() -> str | None:
    """A library the loader finds without PirateFinder's folder: its name or path."""
    found = ctypes.util.find_library("capsimage")
    if found:
        return found
    own = folder()
    for directory in _search_folders():
        if directory == own:
            continue
        for name in GW_NAMES:
            candidate = directory / name
            if candidate.is_file():
                return str(candidate)
    return None


def _search_folders() -> list[Path]:
    """``LD_LIBRARY_PATH``, then the folders the loader searches by default."""
    listed = re.split(r"[:;]", os.environ.get("LD_LIBRARY_PATH", ""))
    folders = [Path(entry) for entry in listed if entry and Path(entry).is_absolute()]
    return folders + list(DEFAULT_FOLDERS)


def status() -> CapsStatus:
    """Where gw's library comes from: PirateFinder's copy first, as gw loads that first."""
    name = machine()
    build = build_for_machine(name)
    installed = installed_library()
    if installed is not None:
        return CapsStatus(CapsState.INSTALLED, _installed_version(), str(installed), name, build)
    found = system_library()
    if found:
        return CapsStatus(CapsState.SYSTEM, path=found, machine=name, build=build)
    state = CapsState.MISSING if build is not None else CapsState.UNSUPPORTED
    return CapsStatus(state, machine=name, build=build)


def _installed_version() -> str:
    try:
        info = json.loads((folder() / _INSTALL_INFO).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    return str(info.get("version", "")) if isinstance(info, dict) else ""


def library_path(existing: str = "") -> str:
    """The ``LD_LIBRARY_PATH`` gw runs with: PirateFinder's folder first when it holds the
    library, then the folders of ``existing``."""
    if installed_library() is None:
        return existing
    own = str(folder())
    rest = [entry for entry in existing.split(":") if entry and entry != own]
    return ":".join([own, *rest])


# Installing --------------------------------------------------------------------


def install(
    build: CapsBuild | None = None,
    downloader: Any = None,
    progress: Callable[[int, int | None], None] | None = None,
    cancel: object | None = None,
) -> CapsStatus:
    """Download the build for this computer and install its library; return the new status.

    Raises CapsError with a sentence for the user, and lets the downloader's
    cancellation error through. Nothing is installed unless every check
    passes.
    """
    from ..online.http import DownloadCancelled, Downloader, DownloadError

    build = build or build_for_machine()
    if build is None:
        raise CapsError(
            "IPF support cannot be installed on this computer: there is no build of the SPS "
            f"Decoder Library for its processor ({machine()}). Builds exist for "
            f"{_listed(supported_machines())}."
        )
    target = folder()
    if any(character in str(target) for character in ":;"):
        raise CapsError(
            f"IPF support cannot be installed in {target}, because a library search path "
            "cannot hold a folder whose name contains a colon or a semicolon."
        )
    try:
        target.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=".caps-", dir=target) as scratch:
            archive = Path(scratch) / "capsimg.tar.xz"
            try:
                (downloader or Downloader()).download(
                    build.url, archive, progress=progress, cancel=cancel
                )
            except DownloadCancelled:
                raise
            except DownloadError as error:
                raise CapsError(
                    f"The SPS Decoder Library could not be downloaded. {error}"
                ) from error
            check_archive(archive, build)
            data = read_library(archive, build)
            problem = elf_problem(data)
            if problem:
                raise CapsError(f"{problem} It was not installed.")
            staged = Path(scratch) / LIBRARY_NAME
            staged.write_bytes(data)
            staged.chmod(0o644)
            library_version = load_check(staged)
            info = Path(scratch) / _INSTALL_INFO
            info.write_text(
                json.dumps(
                    {
                        "version": build.version,
                        "library_version": library_version,
                        "url": build.url,
                        "sha256": build.sha256,
                        "machine": machine(),
                    },
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
            os.replace(staged, target / LIBRARY_NAME)
            os.replace(info, target / _INSTALL_INFO)
    except OSError as error:
        raise CapsError(
            f"IPF support could not be installed in {target}: {error.strerror or error}."
        ) from error
    return status()


def check_archive(archive: Path, build: CapsBuild) -> None:
    """Refuse a download whose size or SHA-256 is not the pinned one."""
    digest = hashlib.sha256()
    size = 0
    with archive.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 16), b""):
            digest.update(chunk)
            size += len(chunk)
    if size != build.size or digest.hexdigest() != build.sha256:
        raise CapsError(
            f"The file downloaded from {build.host} is not the one PirateFinder expects: its "
            "size or checksum is different, so nothing was installed."
        )


def read_library(archive: Path, build: CapsBuild) -> bytes:
    """The pinned member of a checked archive, itself checked against its pinned checksum."""
    try:
        with tarfile.open(archive, "r:xz") as bundle:
            member = bundle.getmember(build.member)
            if not member.isreg() or member.size != build.library_size:
                raise CapsError("The library in the downloaded archive is not the expected file.")
            handle = bundle.extractfile(member)
            data = handle.read(build.library_size + 1) if handle is not None else b""
    except KeyError as error:
        raise CapsError("The downloaded archive does not hold the library.") from error
    except (tarfile.TarError, lzma.LZMAError, EOFError) as error:
        raise CapsError(f"The downloaded archive could not be unpacked: {error}.") from error
    if hashlib.sha256(data).hexdigest() != build.library_sha256:
        raise CapsError("The library in the downloaded archive is not the expected file.")
    return data


def _elf_header(data: bytes) -> tuple[int, int, int, int, int] | None:
    """(class, byte order, file type, machine, flags) of an ELF file, or None."""
    if len(data) < 52 or data[:4] != _ELF_MAGIC or data[4] not in (1, 2) or data[5] not in (1, 2):
        return None
    order = "<" if data[5] == 1 else ">"
    kind, elf_machine = struct.unpack_from(order + "HH", data, 16)
    (flags,) = struct.unpack_from(order + "I", data, 36 if data[4] == 1 else 48)
    return data[4], data[5], kind, elf_machine, flags


def _interpreter_header() -> bytes:
    try:
        with open(os.path.realpath(sys.executable), "rb") as handle:
            return handle.read(64)
    except OSError:
        return b""


def elf_problem(data: bytes, reference: bytes | None = None) -> str:
    """Why ``data`` is not a library this Python can load, or "" when it looks right.

    ``reference`` is the start of an ELF file built for this computer; by
    default the running Python's own executable. The class, byte order and
    machine must match, and on ARM the floating point convention as well.
    """
    header = _elf_header(data)
    if header is None or header[2] != _ET_DYN:
        return "The downloaded file is not a Linux shared library."
    own = _elf_header(_interpreter_header() if reference is None else reference)
    if own is None:
        return ""  # nothing to compare with; load_check decides
    float_abi = header[3] == _EM_ARM and (header[4] ^ own[4]) & _EF_ARM_ABI_FLOAT_HARD
    if header[:2] != own[:2] or header[3] != own[3] or float_abi:
        return (
            "The downloaded library is built for a different processor than this "
            f"computer's ({machine()})."
        )
    return ""


def load_check(path: Path) -> str:
    """Load the library at ``path`` in a separate Python and start it; return its version.

    A separate process, so a library that does not suit this processor, or
    crashes, cannot take PirateFinder down with it. Raises CapsError.
    """
    try:
        result = subprocess.run(
            [sys.executable, "-I", "-c", _LOAD_CHECK, str(path)],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=LOAD_TIMEOUT,
            env=minimal_environment(),
        )
    except subprocess.TimeoutExpired as error:
        raise CapsError(
            "The downloaded library did not finish starting, so it was not installed."
        ) from error
    except OSError as error:
        raise CapsError(f"The downloaded library could not be checked: {error}.") from error
    if result.returncode < 0:
        with contextlib.suppress(ValueError):
            name = signal.Signals(-result.returncode).name
            raise CapsError(
                f"The downloaded library stopped with {name} when it was started, so it does "
                "not work on this computer and was not installed."
            )
    if result.returncode != 0:
        lines = [line.strip() for line in result.stderr.splitlines() if line.strip()]
        detail = lines[-1].rstrip(".") if lines else f"exit status {result.returncode}"
        raise CapsError(
            f"The downloaded library does not load on this computer ({detail}), so it was not "
            "installed."
        )
    return result.stdout.strip()


def remove() -> CapsStatus:
    """Remove the library PirateFinder installed; return the new status."""
    target = folder()
    try:
        (target / LIBRARY_NAME).unlink(missing_ok=True)
        (target / _INSTALL_INFO).unlink(missing_ok=True)
    except OSError as error:
        raise CapsError(
            f"IPF support could not be removed from {target}: {error.strerror or error}."
        ) from error
    with contextlib.suppress(OSError):
        target.rmdir()
    return status()


def _listed(names: tuple[str, ...]) -> str:
    if len(names) < 2:
        return "".join(names)
    return f"{', '.join(names[:-1])} and {names[-1]}"

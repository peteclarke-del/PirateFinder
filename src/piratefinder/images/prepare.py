"""Turn any supported image into a file ``gw write`` accepts as it stands.

Containers are decoded to raw sectors first (MSA and unprotected STX to
``.st``, DMS and ADZ to ``.adf``) so every sector image takes the same path.
An ST image's layout comes from its boot sector checked against its size.
The standard 80-cylinder layouts use gw's built-in formats; anything else,
such as an 82-cylinder disk, gets a generated disk definition, because gw
silently drops the cylinders a built-in format does not name. Flux images
and IPF are written directly.

Every refusal is a :class:`PrepareError` whose message is a sentence fit to
show the user, and every conversion made is listed in ``notes``.
"""

from __future__ import annotations

import ctypes.util
import re
from pathlib import Path, PurePosixPath

from ..greaseweazle.diskdefs import (
    DiskDefError,
    builtin_format,
    custom_definition,
    write_definition,
)
from ..models import Geometry, Platform, PreparedImage
from .inspect import (
    AMIGA_DD_TRACK,
    DecodeError,
    amiga_geometry,
    detect_format,
    dms_to_adf,
    flux_details,
    gunzip,
    gzip_member_name,
    msa_to_st,
    st_geometry,
    stx_to_st,
    suffix_of,
)

CAPS_DOWNLOAD = "softpres.org"
#: Cylinders gw writes from an image that has no format (tools/write.py).
GW_DEFAULT_CYLINDERS = 82
_PLATFORM_NAMES = {Platform.AMIGA: "Amiga", Platform.ATARI_ST: "Atari ST"}


class PrepareError(Exception):
    """An image cannot be written; ``message`` says why in a sentence."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


def caps_library() -> str | None:
    """The SPS CAPS library gw needs for IPF images, when installed."""
    return ctypes.util.find_library("capsimage")


def prepare(
    data: bytes,
    name: str,
    workdir: str | Path,
    *,
    label: str,
    platform: Platform | None,
) -> PreparedImage:
    """Write a gw-ready copy of the image ``data`` (named ``name``) into ``workdir``."""
    folder = Path(workdir)
    folder.mkdir(parents=True, exist_ok=True)
    job = _Job(folder, _file_stem(label, name), label or _display(name), platform)
    kind = detect_format(data, name)
    if kind in ("gz", "adz"):
        try:
            data = gunzip(data)
        except DecodeError as error:
            raise PrepareError(f"{_display(name)} could not be decompressed. {error}") from error
        name = gzip_member_name(name)
        inner = detect_format(data, name)
        if kind == "adz" or inner == "adf":
            job.notes.append("Decompressed the ADZ file to an ADF image.")
        else:
            job.notes.append("Decompressed the gzip file.")
        kind = inner
        if kind in ("gz", "adz"):
            raise PrepareError(f"{_display(name)} is compressed twice, which is not supported.")
    return job.run(kind, data, name)


class _Job:
    def __init__(self, folder: Path, stem: str, label: str, platform: Platform | None) -> None:
        self.folder = folder
        self.stem = stem
        self.label = label
        self.requested = platform
        self.notes: list[str] = []

    def run(self, kind: str, data: bytes, name: str) -> PreparedImage:
        if not kind:
            # A sector image of the wrong size is still named for what it
            # should be, and the size message explains more than a refusal.
            kind = {".st": "st", ".adf": "adf"}.get(suffix_of(name), "")
        if kind == "adf":
            return self.adf(data)
        if kind == "adf-ext":
            raise PrepareError(
                "This is an extended ADF, which stores raw tracks for copy-protected disks. "
                "Greaseweazle cannot write it, so choose another dump of this disk, such as an "
                "IPF or a standard ADF."
            )
        if kind == "dms":
            try:
                adf = dms_to_adf(data)
            except DecodeError as error:
                raise PrepareError(f"The DMS archive could not be decoded. {error}") from error
            self.notes.append("Decoded the DMS archive to an ADF image.")
            return self.adf(adf)
        if kind == "st":
            return self.st(data)
        if kind == "msa":
            try:
                raw, geometry = msa_to_st(data)
            except DecodeError as error:
                raise PrepareError(f"The MSA archive could not be unpacked. {error}") from error
            self.notes.append("Unpacked the MSA archive to a plain sector image.")
            return self.st(raw, geometry)
        if kind == "stx":
            return self.stx(data)
        if kind == "ipf":
            return self.ipf(data)
        if kind in ("scp", "hfe"):
            return self.flux(kind, data)
        raise PrepareError(f"{_display(name)} is not a disk image PirateFinder can write.")

    def platform(self, detected: Platform | None) -> Platform:
        if detected is not None and self.requested is not None and detected != self.requested:
            self.notes.append(
                f"The disk is listed for the {_PLATFORM_NAMES[self.requested]}, but the image "
                f"is an {_PLATFORM_NAMES[detected]} image and is written as one."
            )
        chosen = detected or self.requested
        if chosen is None:
            self.notes.append(
                "PirateFinder could not tell whether this image is for an Amiga or an "
                "Atari ST. The disk is written the same way either way."
            )
            return Platform.AMIGA
        return chosen

    def adf(self, adf: bytes) -> PreparedImage:
        platform = self.platform(Platform.AMIGA)
        geometry = amiga_geometry(len(adf))
        if geometry is None and len(adf) % AMIGA_DD_TRACK == 0 and adf:
            cylinders = len(adf) // (2 * AMIGA_DD_TRACK)
            if cylinders < 80:
                self.notes.append(
                    f"The ADF holds only {cylinders} cylinders, so cylinders "
                    f"{cylinders} to 79 are written blank."
                )
                adf += bytes(80 * 2 * AMIGA_DD_TRACK - len(adf))
                geometry = amiga_geometry(len(adf))
        if geometry is None:
            raise PrepareError(
                f"The ADF is {len(adf):,} bytes, which is not a whole Amiga disk of "
                "80 to 84 cylinders. It may be damaged or cut short."
            )
        return self.sectors(adf, ".adf", geometry, platform)

    def st(self, raw: bytes, hint: Geometry | None = None) -> PreparedImage:
        platform = self.platform(Platform.ATARI_ST)
        geometry, source = st_geometry(raw, hint)
        if geometry is None:
            raise PrepareError(
                f"The image is {len(raw):,} bytes, which does not match any Atari ST floppy "
                "layout. It may be damaged or cut short."
            )
        if source == "guess":
            self.notes.append(
                "The boot sector does not describe this disk, so its layout was taken from "
                f"the file size: {_describe(geometry)}."
            )
        return self.sectors(raw, ".st", geometry, platform)

    def stx(self, data: bytes) -> PreparedImage:
        try:
            raw, geometry, protection = stx_to_st(data)
        except DecodeError as error:
            raise PrepareError(f"The Pasti STX image could not be read. {error}") from error
        if protection.get("protected"):
            raise PrepareError(
                "This Pasti STX image records copy protection that a plain sector image cannot "
                "hold, and Greaseweazle cannot write STX files. Choose another dump of this "
                "disk, such as an IPF, SCP or HFE image."
            )
        self.notes.append(
            "Converted the Pasti STX image to a plain sector image. It carries no copy "
            "protection, so nothing is lost."
        )
        return self.st(raw, geometry)

    def ipf(self, data: bytes) -> PreparedImage:
        if caps_library() is None:
            raise PrepareError(
                "Writing an IPF image needs the SPS CAPS library (libcapsimage), which is not "
                "installed. It cannot be shipped with PirateFinder, so download it from "
                f"{CAPS_DOWNLOAD} and install it, or choose another dump of this disk."
            )
        detected, geometry = flux_details(data, "ipf")
        platform = self.platform(detected)
        tracks = self.track_range(geometry)
        path = self._write(data, ".ipf")
        return PreparedImage(
            self.label, platform, str(path), geometry, tracks=tracks, notes=tuple(self.notes)
        )

    def flux(self, kind: str, data: bytes) -> PreparedImage:
        detected, geometry = flux_details(data, kind)
        platform = self.platform(detected)
        tracks = self.track_range(geometry)
        self.notes.append(
            "Greaseweazle cannot verify a flux image after writing it, so the disk is written "
            "without a read-back check."
        )
        path = self._write(data, f".{kind}")
        return PreparedImage(
            self.label,
            platform,
            str(path),
            geometry,
            tracks=tracks,
            verifiable=False,
            notes=tuple(self.notes),
        )

    def track_range(self, geometry: Geometry | None) -> str:
        """A --tracks value when the image has more cylinders than gw writes by default.

        Without a format gw writes cylinders 0 to 81 of a flux or IPF image
        and silently leaves the rest out.
        """
        if geometry is None or geometry.cylinders <= GW_DEFAULT_CYLINDERS:
            return ""
        self.notes.append(
            f"The image has {geometry.cylinders} cylinders, so all of them are written "
            f"rather than Greaseweazle's default of {GW_DEFAULT_CYLINDERS}."
        )
        return f"c=0-{geometry.cylinders - 1}"

    def sectors(
        self, raw: bytes, suffix: str, geometry: Geometry, platform: Platform
    ) -> PreparedImage:
        path = self._write(raw, suffix)
        gw_format = builtin_format(geometry, platform)
        diskdefs = ""
        if gw_format is None:
            try:
                definition = custom_definition(geometry, platform)
            except DiskDefError as error:
                raise PrepareError(f"This disk cannot be written. {error}") from error
            diskdefs = str(write_definition(definition, self.folder / f"{self.stem}.cfg"))
            gw_format = definition.name
            self.notes.append(
                f"The disk has {_describe(geometry)}, which no built-in Greaseweazle format "
                "covers, so a disk definition was generated to write every track."
            )
        return PreparedImage(
            self.label,
            platform,
            str(path),
            geometry,
            gw_format=gw_format,
            diskdefs_path=diskdefs,
            notes=tuple(self.notes),
        )

    def _write(self, data: bytes, suffix: str) -> Path:
        path = self.folder / f"{self.stem}{suffix}"
        path.write_bytes(data)
        return path


def _describe(geometry: Geometry) -> str:
    sides = "1 side" if geometry.heads == 1 else f"{geometry.heads} sides"
    return f"{geometry.cylinders} cylinders, {sides}, {geometry.sectors} sectors per track"


def _display(name: str) -> str:
    return PurePosixPath(name.replace("\\", "/")).name or "The image"


def _file_stem(label: str, name: str) -> str:
    """A file name stem that is safe on disk and in a gw command line.

    gw splits a file argument at "::" to read options, so colons never
    reach the name.
    """
    text = label or PurePosixPath(name.replace("\\", "/")).stem
    stem = re.sub(r"[^A-Za-z0-9._ -]+", "_", text).strip(" ._-")
    stem = re.sub(r"\s+", " ", stem)[:80].strip()
    return stem or "disk"

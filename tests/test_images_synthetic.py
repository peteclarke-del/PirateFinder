"""Synthetic disk images built at run time for the image and writer tests.

Nothing here comes from a real disk: every image is generated from a seeded
pattern, with just enough filing system to be listed. The other
``test_images_*`` and ``test_greaseweazle_*`` modules import these builders.
"""

from __future__ import annotations

import gzip
import io
import random
import struct
import unittest
import zipfile

from piratefinder.images.vendor.dms import crc16, simple_sum
from piratefinder.images.vendor.floppy_geometry import geometry_for_layout
from piratefinder.images.vendor.msa import st_to_msa

SECTOR = 512
AMIGA_CYLINDER = 2 * 11 * SECTOR


def pattern(size: int, seed: int = 1) -> bytes:
    return random.Random(seed).randbytes(size)


def st_image(
    cylinders: int = 80,
    heads: int = 2,
    sectors: int = 10,
    *,
    files: dict[str, bytes] | None = None,
    label: str = "",
    bpb: bool = True,
    seed: int = 1,
) -> bytes:
    """An Atari ST FAT12 floppy with a parameter block, files and a label.

    Paths may hold one directory level, as in ``"GAMES/RUN.PRG"``.
    """
    total = cylinders * heads * sectors
    image = bytearray(pattern(total * SECTOR, seed))
    reserved, fats, per_fat, root_entries, per_cluster = 1, 2, 5, 112, 2
    boot = bytearray(SECTOR)
    boot[0:2] = b"\x60\x38"
    if bpb:
        struct.pack_into("<H", boot, 11, SECTOR)
        boot[13] = per_cluster
        struct.pack_into("<H", boot, 14, reserved)
        boot[16] = fats
        struct.pack_into("<H", boot, 17, root_entries)
        struct.pack_into("<H", boot, 19, total)
        boot[21] = 0xF9
        struct.pack_into("<H", boot, 22, per_fat)
        struct.pack_into("<H", boot, 24, sectors)
        struct.pack_into("<H", boot, 26, heads)
    # Bytes 43-53 hold boot code on an ST; fill them with text that must
    # not be mistaken for the volume label.
    boot[43:54] = b"BOOTCODE123"
    image[0:SECTOR] = boot
    fat = bytearray(per_fat * SECTOR)
    fat[0:3] = b"\xf9\xff\xff"
    root_start = (reserved + fats * per_fat) * SECTOR
    root = bytearray(root_entries * 32)
    data_start = root_start + root_entries * 32
    cluster_size = per_cluster * SECTOR
    next_cluster = 2

    def set_fat(cluster: int, value: int) -> None:
        offset = cluster + cluster // 2
        if cluster & 1:
            fat[offset] = (fat[offset] & 0x0F) | ((value << 4) & 0xF0)
            fat[offset + 1] = (value >> 4) & 0xFF
        else:
            fat[offset] = value & 0xFF
            fat[offset + 1] = (fat[offset + 1] & 0xF0) | ((value >> 8) & 0x0F)

    def allocate(content: bytes) -> int:
        nonlocal next_cluster
        count = max(1, -(-len(content) // cluster_size))
        first = next_cluster
        for index in range(count):
            cluster = first + index
            set_fat(cluster, cluster + 1 if index < count - 1 else 0xFFF)
            offset = data_start + (cluster - 2) * cluster_size
            chunk = content[index * cluster_size : (index + 1) * cluster_size]
            image[offset : offset + cluster_size] = chunk.ljust(cluster_size, b"\0")
        next_cluster += count
        return first

    def entry(name: str, attributes: int, cluster: int, size: int) -> bytes:
        base, _, extension = name.upper().partition(".")
        raw = base.ljust(8)[:8].encode() + extension.ljust(3)[:3].encode()
        record = bytearray(32)
        record[0:11] = raw
        record[11] = attributes
        struct.pack_into("<HI", record, 26, cluster, size)
        return bytes(record)

    root_records: list[bytes] = []
    if label:
        record = bytearray(32)
        record[0:11] = label.upper().ljust(11)[:11].encode()
        record[11] = 0x08
        root_records.append(bytes(record))
    directories: dict[str, list[tuple[str, bytes]]] = {}
    for path, content in (files or {}).items():
        folder, _, leaf = path.rpartition("/")
        directories.setdefault(folder, []).append((leaf, content))
    for leaf, content in directories.pop("", []):
        root_records.append(entry(leaf, 0x20, allocate(content), len(content)))
    for folder, items in directories.items():
        records = [entry(leaf, 0x20, allocate(content), len(content)) for leaf, content in items]
        cluster = next_cluster
        dot = entry(".", 0x10, cluster, 0)
        dotdot = entry("..", 0x10, 0, 0)
        allocate(b"".join([dot, dotdot, *records]))
        root_records.append(entry(folder, 0x10, cluster, 0))
    for index, record in enumerate(root_records):
        root[index * 32 : (index + 1) * 32] = record
    for copy in range(fats):
        offset = (reserved + copy * per_fat) * SECTOR
        image[offset : offset + len(fat)] = fat
    image[root_start : root_start + len(root)] = root
    return bytes(image)


def adf_image(
    cylinders: int = 80,
    *,
    name: str = "",
    files: dict[str, bytes] | None = None,
    sectors: int = 11,
    seed: int = 2,
) -> bytes:
    """An AmigaDOS OFS floppy with a volume name and files, one directory level deep."""
    blocks = cylinders * 2 * sectors
    image = bytearray(pattern(blocks * SECTOR, seed))
    image[0:4] = b"DOS\0"
    root_key = blocks // 2
    next_block = root_key + 2

    def block(number: int) -> memoryview:
        return memoryview(image)[number * SECTOR : (number + 1) * SECTOR]

    def put_long(number: int, offset: int, value: int) -> None:
        struct.pack_into(">I", image, number * SECTOR + offset, value & 0xFFFFFFFF)

    def header(number: int, title: str, secondary: int, parent: int) -> None:
        block(number)[:] = bytes(SECTOR)
        put_long(number, 0, 2)
        put_long(number, 4, number)
        put_long(number, 500, parent)
        put_long(number, 508, secondary)
        encoded = title.encode("latin-1")
        image[number * SECTOR + 432] = len(encoded)
        image[number * SECTOR + 433 : number * SECTOR + 433 + len(encoded)] = encoded

    def add_file(title: str, content: bytes, parent: int, slot: int) -> None:
        nonlocal next_block
        key = next_block
        next_block += 1
        header(key, title, -3, parent)
        put_long(key, 324, len(content))
        chunks = [content[i : i + 488] for i in range(0, len(content), 488)] or [b""]
        keys = list(range(next_block, next_block + len(chunks)))
        next_block += len(chunks)
        put_long(key, 16, keys[0])
        for sequence, (data_key, chunk) in enumerate(zip(keys, chunks, strict=True), 1):
            block(data_key)[:] = bytes(SECTOR)
            put_long(data_key, 0, 8)
            put_long(data_key, 4, key)
            put_long(data_key, 8, sequence)
            put_long(data_key, 12, len(chunk))
            put_long(data_key, 16, keys[sequence] if sequence < len(keys) else 0)
            image[data_key * SECTOR + 24 : data_key * SECTOR + 24 + len(chunk)] = chunk
        put_long(parent, 24 + 4 * slot, key)

    header(root_key, name, 1, 0)
    put_long(root_key, 12, 72)
    groups: dict[str, list[tuple[str, bytes]]] = {}
    for path, content in (files or {}).items():
        folder, _, leaf = path.rpartition("/")
        groups.setdefault(folder, []).append((leaf, content))
    slot = 0
    for leaf, content in groups.pop("", []):
        add_file(leaf, content, root_key, slot)
        slot += 1
    for folder, items in groups.items():
        key = next_block
        next_block += 1
        header(key, folder, 2, root_key)
        put_long(root_key, 24 + 4 * slot, key)
        slot += 1
        for index, (leaf, content) in enumerate(items):
            add_file(leaf, content, key, index)
    return bytes(image)


def msa_archive(raw: bytes, cylinders: int, heads: int, sectors: int) -> bytes:
    return st_to_msa(raw, geometry_for_layout(cylinders, heads, sectors))


def rle_encode(data: bytes) -> bytes:
    """DMS run-length coding: 0x90 escapes, runs of four or more are packed."""
    out = bytearray()
    index = 0
    while index < len(data):
        value = data[index]
        run = 1
        while index + run < len(data) and data[index + run] == value and run < 0xFFFF:
            run += 1
        if run >= 4:
            if run < 0xFF:
                out += bytes((0x90, run, value))
            else:
                out += bytes((0x90, 0xFF, value)) + run.to_bytes(2, "big")
        else:
            for _ in range(run):
                out += b"\x90\x00" if value == 0x90 else bytes((value,))
        index += run
    return bytes(out)


def quick_literals(data: bytes) -> bytes:
    """A DMS QUICK stream made only of literals: a one bit, then eight."""
    bits = 0
    count = 0
    out = bytearray()
    for value in data:
        bits = (bits << 9) | 0x100 | value
        count += 9
        while count >= 8:
            count -= 8
            out.append((bits >> count) & 0xFF)
            bits &= (1 << count) - 1
    if count:
        out.append((bits << (8 - count)) & 0xFF)
    return bytes(out)


def dms_track(number: int, data: bytes, mode: int = 0) -> bytes:
    if mode == 0:
        payload, middle = data, len(data)
    elif mode == 1:
        payload = rle_encode(data)
        middle = len(payload)
    elif mode == 2:
        packed = rle_encode(data)
        payload, middle = quick_literals(packed), len(packed)
    else:
        raise ValueError(mode)
    head = bytearray(20)
    head[0:2] = b"TR"
    struct.pack_into(
        ">HHHHHBBHH",
        head,
        2,
        number,
        0,
        len(payload),
        middle,
        len(data),
        0,
        mode,
        simple_sum(data),
        crc16(payload),
    )
    struct.pack_into(">H", head, 18, crc16(bytes(head[:18])))
    return bytes(head) + payload


def dms_archive(
    adf: bytes,
    *,
    mode: int | tuple[int, ...] = 0,
    extra: tuple[tuple[int, bytes], ...] = (),
) -> bytes:
    """A DiskMasher archive of ``adf``, one track per cylinder, as xDMS reads them."""
    size = AMIGA_CYLINDER if len(adf) < 2 * 901120 else 2 * AMIGA_CYLINDER
    cylinders = len(adf) // size
    modes = mode if isinstance(mode, tuple) else (mode,)
    body = b"".join(
        dms_track(n, adf[n * size : (n + 1) * size], modes[n % len(modes)])
        for n in range(cylinders)
    )
    body += b"".join(dms_track(number, data) for number, data in extra)
    head = bytearray(56)
    head[0:4] = b"DMS!"
    struct.pack_into(
        ">IIIHHIIHHHHHHHIHHHHH",
        head,
        4,
        0,
        0,
        0,
        0,
        cylinders - 1,
        len(body),
        len(adf),
        39,
        106,
        0,
        0,
        0,
        0,
        0,
        0,
        0x0207,
        0x0100,
        2,
        modes[0],
        0,
    )
    return bytes(head) + body


def stx_archive(
    raw: bytes, cylinders: int, heads: int, sectors: int, *, protect: bool = False
) -> bytes:
    """A Pasti image of plain tracks; ``protect`` adds an extra sector to track 0."""
    records = []
    for cylinder in range(cylinders):
        for head in range(heads):
            offset = (cylinder * heads + head) * sectors * SECTOR
            data = raw[offset : offset + sectors * SECTOR]
            count = sectors
            if protect and cylinder == 0 and head == 0:
                data += bytes(SECTOR)
                count += 1
            record = bytearray(16)
            struct.pack_into("<IIHHH", record, 0, 16 + len(data), 0, count, 0, 6250)
            record[14] = cylinder | (head << 7)
            records.append(bytes(record) + data)
    header = bytearray(16)
    header[0:4] = b"RSY\0"
    struct.pack_into("<HH", header, 4, 3, 1)
    header[10] = len(records)
    header[11] = 2
    return bytes(header) + b"".join(records)


def ipf_header(platform: int = 1, cylinders: int = 84) -> bytes:
    """The CAPS and INFO records that open an IPF file, and some padding."""
    caps = b"CAPS" + struct.pack(">II", 12, 0)
    fields = [1, 1, 1, 0, 0, 0, 0, cylinders - 1, 0, 1, 0, 0, platform, 0, 0, 0, 1, 0, 0, 0, 0]
    info = b"INFO" + struct.pack(">II", 96, 0) + struct.pack(">21I", *fields)
    return caps + info + bytes(512)


def scp_header(disk_type: int = 0x15, end_track: int = 163) -> bytes:
    header = bytearray(0x2B0)
    header[0:3] = b"SCP"
    header[3] = 0x19
    header[4] = disk_type
    header[5] = 3
    header[6] = 0
    header[7] = end_track
    header[10] = 0
    return bytes(header) + bytes(1024)


def hfe_header(interface: int = 2, encoding: int = 0, cylinders: int = 82) -> bytes:
    header = bytearray(512)
    header[0:8] = b"HXCPICFE"
    header[9] = cylinders
    header[10] = 2
    header[11] = encoding
    header[16] = interface
    header[18:20] = (1).to_bytes(2, "little")
    return bytes(header) + bytes(1024)


def gzip_bytes(data: bytes) -> bytes:
    return gzip.compress(data, mtime=0)


def zip_bytes(members: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in members.items():
            archive.writestr(name, data)
    return buffer.getvalue()


def lzh_bytes(members: dict[str, bytes]) -> bytes:
    """An LZH file storing each member uncompressed (method -lh0-, header level 0).

    No LZH packer is at hand, and 7-Zip reads but does not write LZH; the
    stored method exercises the same headers and 16-bit CRC as -lh5-.
    """
    out = bytearray()
    for name, data in members.items():
        encoded = name.encode("ascii")
        header = (
            b"-lh0-"
            + struct.pack("<IIIBB", len(data), len(data), 0x21A40000, 0x20, 0)
            + bytes((len(encoded),))
            + encoded
            + struct.pack("<H", crc16(data))
        )
        out += bytes((len(header), sum(header) & 0xFF)) + header + data
    return bytes(out + b"\x00")


class SyntheticImageTests(unittest.TestCase):
    """The builders produce images of the sizes the formats define."""

    def test_sizes(self) -> None:
        self.assertEqual(len(st_image(80, 2, 9)), 737_280)
        self.assertEqual(len(st_image(82, 2, 10)), 839_680)
        self.assertEqual(len(adf_image()), 901_120)
        self.assertEqual(len(adf_image(sectors=22)), 1_802_240)

    def test_quick_literals_round_trip_through_the_vendored_decoder(self) -> None:
        from piratefinder.images.vendor.dms_codec import DMSDecoder, unpack_rle

        data = pattern(3000, 5) + bytes(500) + b"\x90" * 7
        packed = rle_encode(data)
        stream = quick_literals(packed)
        decoded = DMSDecoder().unpack_track(stream, 2, len(packed), len(data), 0)
        self.assertEqual(decoded, data)
        self.assertEqual(unpack_rle(packed, len(data)), data)


if __name__ == "__main__":
    unittest.main()

"""Version 1 SD layout and checksummed U-Boot scripts, shared by builder/updater."""
from __future__ import annotations

import re
import struct
import zlib

IMAGE_SIZE = 256 * 1024 * 1024
FAT_START = 2048
FAT_SECTORS = IMAGE_SIZE // 512 - FAT_START - 4096
SLOT_SECTORS = 2048
SLOT_BYTES = 128 * 1024
SLOT_STARTS = (FAT_START + FAT_SECTORS, FAT_START + FAT_SECTORS + SLOT_SECTORS)
MARKER = b"HPX1"


def mbr() -> bytes:
    block = bytearray(512)
    block[440:444] = MARKER
    for index, (kind, start, count) in enumerate(((12, FAT_START, FAT_SECTORS),
                                                (218, SLOT_STARTS[0], SLOT_SECTORS),
                                                (218, SLOT_STARTS[1], SLOT_SECTORS))):
        block[446 + index * 16:462 + index * 16] = struct.pack(
            "<B3sB3sII", 128 if index == 0 else 0, b"\xfe\xff\xff", kind,
            b"\xfe\xff\xff", start, count)
    block[510:] = b"\x55\xaa"
    return bytes(block)


def script_image(script: str, model: str, sequence: int = 0) -> bytes:
    if model not in {"pi2", "pi3", "pi3plus"} or not 0 <= sequence < 0x7fffffff:
        raise ValueError("Unsupported board or boot sequence")
    text = (f"setenv pxe_slot_seq {sequence}\n"
            'if test "${pxe_probe}" = yes; then exit; fi\n' + script).encode()
    data = struct.pack(">II", len(text), 0) + text
    header = struct.pack(">7I4B32s", 0x27051956, 0, 0, len(data), 0, 0,
                         zlib.crc32(data), 5, 2 if model == "pi2" else 22, 6, 0, b"HA-PXE boot instructions v1")
    header = header[:4] + struct.pack(">I", zlib.crc32(header)) + header[8:]
    if len(header + data) > SLOT_BYTES:
        raise ValueError("Boot script exceeds slot capacity")
    return header + data


def parse_script(image: bytes) -> tuple[int, str, str]:
    if len(image) < 72:
        raise ValueError("Truncated boot script")
    magic, crc, _, size, _, _, data_crc, os_id, arch, kind, compression, _ = struct.unpack(
        ">7I4B32s", image[:64])
    if (magic != 0x27051956 or size > SLOT_BYTES - 64 or size < 8 or
            (os_id, kind, compression) != (5, 6, 0) or arch not in {2, 22}):
        raise ValueError("Invalid boot script header")
    if zlib.crc32(image[:4] + bytes(4) + image[8:64]) != crc:
        raise ValueError("Boot script header checksum mismatch")
    data = image[64:64 + size]
    if len(data) != size or zlib.crc32(data) != data_crc:
        raise ValueError("Boot script checksum mismatch")
    length, end = struct.unpack(">II", data[:8])
    if length != size - 8 or end != 0:
        raise ValueError("Invalid script length")
    text = data[8:].decode("utf-8")
    match = re.match(r'setenv pxe_slot_seq ([0-9]+)\nif test "\$\{pxe_probe\}" = yes; then exit; fi\n', text)
    if not match or int(match[1]) >= 0x7fffffff:
        raise ValueError("Invalid script sequence")
    return int(match[1]), text[match.end():], "pi2" if arch == 2 else "pi3"

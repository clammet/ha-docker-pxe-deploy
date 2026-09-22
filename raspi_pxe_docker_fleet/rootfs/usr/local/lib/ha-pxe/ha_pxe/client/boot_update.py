"""Update only the inactive instruction slot on an explicitly marked HA-PXE SD."""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import stat
import struct
from pathlib import Path

from ..boot_slots import IMAGE_SIZE, SLOT_BYTES, SLOT_STARTS, mbr, parse_script, script_image


def read_slot(stream, index: int):
    stream.seek(SLOT_STARTS[index] * 512)
    try:
        return parse_script(stream.read(SLOT_BYTES))
    except (ValueError, UnicodeError):
        return None


def install(stream, script: str, model: str, active: int, *, running_matches: bool = False) -> bool:
    """Keep the currently booted slot intact; commit the new header last."""
    stream.seek(0)
    if stream.read(512) != mbr():
        raise ValueError("SD partition table is not the supported HA-PXE layout")
    if active not in {0, 2, 3}:
        raise ValueError("Unknown active SD slot")
    slots = [read_slot(stream, i) for i in range(2)]
    sequences = [slot[0] if slot else -1 for slot in slots]
    newest = slots[max(range(2), key=lambda i: sequences[i])]
    if (newest and newest[1] == script) or (newest is None and running_matches):
        return False
    target = (1 if active == 2 else 0) if active else min(range(2), key=lambda i: sequences[i])
    image = script_image(script, model, max(sequences) + 1).ljust(SLOT_BYTES, b"\0")
    offset = SLOT_STARTS[target] * 512

    def write_at(position: int, data: bytes) -> None:
        stream.seek(position)
        if stream.write(data) != len(data):
            raise OSError("Short SD write")
        stream.flush()
        os.fsync(stream.fileno())

    write_at(offset, bytes(512))
    write_at(offset + 512, image[512:])
    write_at(offset, image[:512])
    stream.seek(offset)
    if stream.read(SLOT_BYTES) != image:
        raise OSError("SD update readback mismatch")
    return True


def validate_update(data: dict, model: str, serial: str) -> str:
    if not isinstance(data, dict):
        raise ValueError("Invalid SD update manifest")
    if data.get("format") != 1 or data.get("model") != model or data.get("serial") != serial:
        raise ValueError("SD update is for another client, model or layout")
    script = data.get("script")
    if not isinstance(script, str) or len(script.encode()) > SLOT_BYTES - 1024:
        raise ValueError("Invalid SD instruction size")
    if hashlib.sha256(script.encode()).hexdigest() != data.get("script_sha256"):
        raise ValueError("SD instruction checksum mismatch")
    # Loader identity is the hash of the body after its identity declaration.
    declaration, _, body = script.partition("\n")
    digest = hashlib.sha256(body.encode()).hexdigest()
    if data.get("loader") != digest or declaration != f"setenv pxe_script_sha {digest}":
        raise ValueError("SD loader identity mismatch")
    script_image(script, model)
    return script


def find_card(mountinfo: str) -> Path:
    roots = [line for line in mountinfo.splitlines() if line.split()[4] == "/"]
    if len(roots) != 1 or roots[0].split(" - ")[1].split()[0] not in {"nfs", "nfs4"}:
        raise ValueError("SD updates are permitted only while running from NFS")
    candidates = []
    for block in Path("/sys/class/block").glob("mmcblk*"):
        if not re.fullmatch(r"mmcblk[0-9]+", block.name):
            continue
        kind = block / "device/type"
        if kind.is_file() and kind.read_text().strip() == "SD":
            candidates.append(block)
    if len(candidates) != 1:
        raise ValueError("Cannot identify exactly one physical SD card")
    block = candidates[0]
    if (block / "queue/logical_block_size").read_text().strip() != "512":
        raise ValueError("Unsupported SD sector size")
    devices = {(block / "dev").read_text().strip()}
    for partition in block.glob(block.name + "p*"):
        devices.add((partition / "dev").read_text().strip())
    if any(line.split()[2] in devices for line in mountinfo.splitlines()):
        raise ValueError("An SD partition is mounted; refusing to write")
    return Path("/dev") / block.name


def main() -> int:
    args = dict(token.split("=", 1) for token in Path("/proc/cmdline").read_text().split() if "=" in token)
    if args.get("ha_pxe.media") != "1":
        return 0  # Ordinary PXE clients and other SD cards are never touched.
    try:
        model = args.get("ha_pxe.model", "")
        if model not in {"pi2", "pi3", "pi3plus"}:
            raise ValueError("Unknown boot board")
        actual = Path("/sys/firmware/devicetree/base/serial-number").read_bytes().rstrip(b"\0").decode().lower()
        update = Path("/boot/firmware/ha-pxe") / model / "sd-update.json"
        if not update.is_file():
            return 0
        if update.stat().st_size > 2 * SLOT_BYTES:
            raise ValueError("Oversized SD update manifest")
        data = json.loads(update.read_text())
        if not isinstance(data, dict):
            raise ValueError("Invalid SD update manifest")
        serial = str(data.get("serial", ""))
        if not re.fullmatch(r"[0-9a-f]{1,16}", serial) or (serial.zfill(8) != actual[-8:] if len(serial) <= 8 else serial.zfill(16) != actual.zfill(16)):
            raise ValueError("SD update serial does not match this Raspberry Pi")
        script = validate_update(data, model, serial)
        device = find_card(Path("/proc/self/mountinfo").read_text())
        # Exclusive block-device open rejects mounted media. Never open a user-selected disk.
        fd = os.open(device, os.O_RDWR | os.O_EXCL | os.O_SYNC | os.O_NOFOLLOW)
        with os.fdopen(fd, "r+b", buffering=0) as stream:
            if not stat.S_ISBLK(os.fstat(stream.fileno()).st_mode):
                raise ValueError("SD device is not a block device")
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            capacity = struct.unpack("Q", fcntl.ioctl(stream, 0x80081272, bytes(8)))[0]
            if capacity < IMAGE_SIZE:
                raise ValueError("SD device is too small")
            if install(stream, script, model, int(args.get("ha_pxe.slot", "-1")),
                       running_matches=args.get("ha_pxe.loader") == data["loader"]):
                print("HA-PXE: SD boot instructions updated and verified; used on the next reboot", flush=True)
        return 0
    except (OSError, ValueError, KeyError) as exc:
        print(f"HA-PXE: SD update refused: {exc}", flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3.14
"""Inspect, write and read-back verify an SD boot image on macOS or Linux.

Dry-run is the default. --write requires root and interactive confirmation of
the exact whole-disk device. No device is chosen automatically.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import plistlib
import re
import stat
import subprocess
import sys
from pathlib import Path


def capture(*args: str) -> bytes:
    return subprocess.check_output(args)


def digest(stream, size: int | None = None) -> str:
    result = hashlib.sha256()
    remaining = size
    while remaining is None or remaining > 0:
        data = stream.read(min(4 * 1024 * 1024, remaining) if remaining is not None else 4 * 1024 * 1024)
        if not data:
            if remaining:
                raise ValueError("Unexpected end of device during verification")
            break
        result.update(data)
        if remaining is not None:
            remaining -= len(data)
    return result.hexdigest()


def verify_image(path: Path) -> tuple[int, str]:
    if not path.is_file() or path.is_symlink():
        raise ValueError("Image must be a regular file, not a symlink or device")
    checksum = path.with_suffix(path.suffix + ".sha256").read_text().split()
    if len(checksum) != 2 or not re.fullmatch("[a-f0-9]{64}", checksum[0]) or checksum[1] != path.name:
        raise ValueError("Invalid image checksum sidecar")
    size = path.stat().st_size
    if size < 64 * 1024 * 1024 or size % 512:
        raise ValueError("Invalid disk image size")
    with path.open("rb") as stream:
        if digest(stream) != checksum[0]:
            raise ValueError("Image checksum mismatch; refusing to access a disk")
    return size, checksum[0]


def mac_candidate(info: dict) -> dict:
    device = info.get("DeviceIdentifier", "")
    if not re.fullmatch(r"disk[0-9]+", device) or not info.get("Whole"):
        raise ValueError("Select a whole physical disk, not a partition")
    if info.get("VirtualOrPhysical") != "Physical" or info.get("OSInternalMedia"):
        raise ValueError("Virtual and system media are not supported")
    # Built-in SD readers may report Internal=true; require removable media in that case.
    if info.get("Internal", True) and not info.get("RemovableMedia", False):
        raise ValueError("Refusing an internal non-removable disk")
    if not info.get("Writable"):
        raise ValueError("Disk is not writable")
    return {"device": f"/dev/{device}", "size": info["TotalSize"],
            "sector_size": info.get("DeviceBlockSize", 512),
            "identity": [info.get("MediaName"), info.get("DiskUUID"), info.get("IORegistryEntryName")],
            "description": info.get("MediaName", device)}


def mac_system_disks(root_info: dict, apfs: dict) -> set[str]:
    protected = {root_info.get("ParentWholeDisk"), root_info.get("DeviceIdentifier")}
    for container in apfs.get("Containers", []):
        if any(volume.get("MountPoint", "").startswith(("/System/", "/private/"))
               or volume.get("MountPoint") == "/" for volume in container.get("Volumes", [])):
            for store in container.get("PhysicalStores", []):
                match = re.match(r"(disk[0-9]+)", store.get("DeviceIdentifier", ""))
                if match:
                    protected.add(match[1])
    return {value for value in protected if value}


def linux_candidate(info: dict) -> dict:
    if info.get("type") != "disk" or info.get("ro"):
        raise ValueError("Select a writable whole disk")
    if not (info.get("rm") or info.get("tran") == "usb"):
        raise ValueError("Refusing a non-removable disk without a USB transport")

    def check_mounts(node: dict) -> None:
        for mount in node.get("mountpoints") or []:
            if mount and not mount.startswith(("/media/", "/run/media/", "/mnt/")):
                raise ValueError(f"Disk contains a protected mount: {mount}")
        if node.get("type") in {"crypt", "lvm", "raid0", "raid1", "raid5", "raid6", "raid10"}:
            raise ValueError("Disk participates in a mapped/RAID volume")
        for child in node.get("children", []):
            check_mounts(child)
    check_mounts(info)
    return {"device": info["path"], "size": int(info["size"]),
            "sector_size": int(info.get("log-sec", 512)),
            "identity": [info.get("model"), info.get("serial"), info.get("maj:min")],
            "description": f"{info.get('model', '')} {info.get('serial', '')}".strip(), "tree": info}


def inspect(device: str) -> dict:
    system = platform.system()
    if system == "Darwin":
        if not re.fullmatch(r"/dev/disk[0-9]+", device):
            raise ValueError("Use a whole disk such as /dev/disk4, not rdisk or a partition")
        protected = mac_system_disks(
            plistlib.loads(capture("diskutil", "info", "-plist", "/")),
            plistlib.loads(capture("diskutil", "apfs", "list", "-plist")))
        if device.removeprefix("/dev/") in protected:
            raise ValueError("Refusing a disk backing the running macOS system")
        return mac_candidate(plistlib.loads(capture("diskutil", "info", "-plist", device)))
    if system == "Linux":
        if not re.fullmatch(r"/dev/(sd[a-z]+|mmcblk[0-9]+)", device):
            raise ValueError("Supported targets are whole /dev/sdX or /dev/mmcblkN devices")
        if not stat.S_ISBLK(os.stat(device).st_mode):
            raise ValueError("Target is not a block device")
        data = json.loads(capture("lsblk", "--json", "--bytes", "--paths", "--output",
            "PATH,TYPE,SIZE,RM,RO,TRAN,MODEL,SERIAL,MOUNTPOINTS,LOG-SEC,MAJ:MIN", device))
        if len(data["blockdevices"]) != 1:
            raise ValueError("Ambiguous disk")
        return linux_candidate(data["blockdevices"][0])
    raise ValueError("Only macOS and Linux are supported")


def unmount(info: dict) -> None:
    if platform.system() == "Darwin":
        subprocess.run(["diskutil", "unmountDisk", info["device"]], check=True)
    else:
        def visit(node: dict) -> None:
            for child in node.get("children", []):
                visit(child)
            if any(node.get("mountpoints") or []):
                subprocess.run(["umount", node["path"]], check=True)
        visit(info["tree"])


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("image", type=Path, nargs="?")
    parser.add_argument("--device")
    parser.add_argument("--list", action="store_true")
    parser.add_argument("--write", action="store_true")
    args = parser.parse_args()
    if args.list:
        if platform.system() == "Darwin":
            subprocess.run(["diskutil", "list", "physical"], check=True)
        elif platform.system() == "Linux":
            subprocess.run(["lsblk", "-o", "PATH,SIZE,MODEL,SERIAL,TRAN,RM,MOUNTPOINTS"], check=True)
        else:
            parser.error("Only macOS and Linux are supported")
        return
    if not args.image or not args.device:
        parser.error("image and --device are required (or use --list)")
    size, expected = verify_image(args.image)
    info = inspect(args.device)
    if info["sector_size"] != 512 or info["size"] < size + 34 * 512:
        raise ValueError("Disk must use 512-byte logical sectors and have room for the image")
    print(f"Image: {args.image} ({size // 1048576} MiB), SHA-256 verified")
    print(f"Target: {info['device']} — {info['description']} ({info['size'] / 1e9:.1f} GB)")
    if not args.write:
        print("Dry run: nothing unmounted or written. Add --write to erase and flash this disk.")
        return
    if os.geteuid() != 0:
        raise ValueError("Writing requires sudo with python3.14")
    if not sys.stdin.isatty():
        raise ValueError("Writing requires an interactive terminal")
    confirmation = f"ERASE {args.device}"
    if input(f"All data on this disk will be erased. Type '{confirmation}': ") != confirmation:
        raise ValueError("Cancelled")
    fresh = inspect(args.device)
    if any(fresh[key] != info[key] for key in ("device", "size", "sector_size", "identity")):
        raise ValueError("Disk changed since inspection; refusing to write")
    unmount(fresh)
    target = args.device.replace("/dev/disk", "/dev/rdisk") if platform.system() == "Darwin" else args.device
    # r+b never creates a file if a removable disk disappeared.
    with open(target, "r+b", buffering=0) as disk, args.image.open("rb") as image:
        # Remove a previous backup GPT so it cannot conflict with the new MBR.
        disk.seek(info["size"] - 33 * 512)
        disk.write(bytes(33 * 512))
        disk.seek(0)
        written = 0
        while data := image.read(4 * 1024 * 1024):
            view = memoryview(data)
            while view:
                count = disk.write(view)
                if not count:
                    raise OSError("Short disk write")
                view = view[count:]
            written += len(data)
            print(f"\rWriting: {written * 100 // size}%", end="", flush=True)
        os.fsync(disk.fileno())
    if platform.system() == "Linux":
        subprocess.run(["blockdev", "--flushbufs", args.device], check=True)
    print("\nVerifying the written image...")
    with open(target, "rb", buffering=0) as disk:
        if digest(disk, size) != expected:
            raise ValueError("Read-back verification failed; do not use this card")
    if platform.system() == "Darwin":
        subprocess.run(["diskutil", "eject", args.device], check=True)
    print("Image written and read-back SHA-256 verified. The card is ready to remove.")


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError, subprocess.CalledProcessError, KeyboardInterrupt) as exc:
        sys.exit(f"Stopped: {exc}")

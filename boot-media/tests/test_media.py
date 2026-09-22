from __future__ import annotations

import gzip
import hashlib
import importlib.util
import struct
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


prepare = load("prepare", BASE / "scripts/prepare.py")
from ha_pxe import boot_payload as payload
from ha_pxe import boot_slots
flash = load("flash", BASE / "flash.py")


class PreparationTests(unittest.TestCase):
    def test_retry_script_recovers_from_dhcp_tftp_and_hash_failures(self):
        # U-Boot's shell commands are stubbed in Bash to exercise the actual
        # nested retry control flow. Binary compilation/packaging is tested separately.
        script = (payload.TEMPLATES / "boot.cmd").read_text().replace("${serial#}", "${board_serial}")
        for key, value in {"SERIAL16": "000000000000abcd", "SERIAL": "abcd", "MODEL": "pi2",
                           "SERVER": "192.0.2.10", "BOOT_COMMAND": "boot_linux"}.items():
            script = script.replace(f"@{key}@", value)
        stubs = r'''
board_serial=000000000000abcd
attempt=0
pxe_slot=0
setexpr() { pxe_attempt=$((pxe_attempt+1)); }
setenv() { local key=$1; shift; printf -v "$key" '%s' "$*"; }
usb() { :; }
dhcp() { attempt=$((attempt+1)); test "$attempt" -gt 1; }
tftpboot() {
    filesize=10
    last_file=$2
    if test "$attempt" = 2 && [[ $2 = */boot.env ]]; then return 1; fi
    if test "$attempt" = 3 && [[ $2 = */kernel.img ]]; then return 1; fi
    return 0
}
env() {
    pxe_generation=abc
    pxe_kernel_sha=good
    pxe_initrd_sha=good
    pxe_bootargs='console=tty1 rdinit=/init'
}
hash() {
    pxe_actual_sha=good
    if test "$attempt" = 4 && [[ $last_file = */initramfs.gz ]]; then pxe_actual_sha=bad; fi
}
fdt() { :; }
sleep() { if test "$attempt" -gt 5; then exit 99; fi; }
boot_linux() { echo "BOOTED:$attempt:$bootargs"; exit 0; }
'''
        result = subprocess.run(["bash"], input=stubs + script, text=True, capture_output=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("BOOTED:5:console=tty1 rdinit=/init", result.stdout)

    def test_recovery_selects_newest_valid_slot_then_other_then_fallback(self):
        script = (BASE / "templates/recovery.cmd").read_text()
        for a_seq, b_seq, expected in ((2, 3, "3 2 0"), (5, 1, "2 3 0"),
                                       (-1, 1, "3 0"), (-1, -1, "0")):
            stubs = r'''
setenv() { local key=$1; shift; printf -v "$key" '%s' "$*"; }
mmc() { :; }
iminfo() {
    if test "$1" = 0x02000000; then test "$a_seq" -ge 0; else test "$b_seq" -ge 0; fi
}
source() {
    if test "$pxe_probe" = yes; then
        if test "$1" = 0x02000000; then pxe_slot_seq=$a_seq; else pxe_slot_seq=$b_seq; fi
    else
        printf '%s ' "$pxe_slot"
    fi
}
fatload() { :; }
'''
            result = subprocess.run(['bash'], input=f'a_seq={a_seq}\nb_seq={b_seq}\n' + stubs + script,
                                    text=True, capture_output=True, timeout=5)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), expected)

    def test_identifies_kernel_release_inside_zimage_gzip_with_trailing_data(self):
        kernel = b"header" + gzip.compress(b"Linux version 6.12.20-v7+ (builder) ") + b"trailer"
        self.assertEqual(payload.kernel_release(kernel), "6.12.20-v7+")

    def test_kernel_release_rejects_unknown_format(self):
        with self.assertRaises(ValueError):
            payload.kernel_release(b"not a kernel")

    def test_root_settings_preserves_console_and_replaces_boot_method(self):
        root, args = payload.root_settings(
            "console=serial0,115200 root=/dev/nfs rootfstype=nfs "
            "nfsroot=192.0.2.10:/data/exports/abc/root,vers=3,tcp,nolock rw ip=dhcp rootwait panic=0",
            "192.0.2.10")
        self.assertEqual(root, "/data/exports/abc/root")
        self.assertEqual(args, "console=serial0,115200 rdinit=/init rw panic=10 net.ifnames=0")
        with self.assertRaises(ValueError):
            payload.root_settings("nfsroot=192.0.2.10:/data/exports/abc/root", "192.0.2.11")

    def test_module_dependencies_and_soft_dependencies_are_included(self):
        with tempfile.TemporaryDirectory() as tmp:
            tree = Path(tmp)
            (tree / "modules.dep").write_text(
                "kernel/nfs.ko.xz: kernel/sunrpc.ko.xz\n"
                "kernel/sunrpc.ko.xz:\n"
                "kernel/helper.ko.zst:\n"
                "kernel/unrelated.ko:\n")
            (tree / "modules.softdep").write_text("softdep nfs pre: helper\n")
            selected, _ = payload.module_closure(tree)
            self.assertEqual(selected, ["kernel/helper.ko.zst", "kernel/nfs.ko.xz", "kernel/sunrpc.ko.xz"])

    def test_module_dependencies_reject_escape(self):
        with tempfile.TemporaryDirectory() as tmp:
            tree = Path(tmp)
            (tree / "modules.dep").write_text("kernel/nfs.ko: ../../outside.ko\n")
            with self.assertRaises(ValueError):
                payload.module_closure(tree)

    def test_mbr_has_nonoverlapping_recovery_and_update_partitions(self):
        block = boot_slots.mbr()
        self.assertEqual(block[510:], b"\x55\xaa")
        self.assertEqual(block[440:444], b"HPX1")
        end = 2048
        for index, kind in enumerate((12, 218, 218)):
            entry = struct.unpack("<B3sB3sII", block[446 + 16 * index:462 + 16 * index])
            self.assertEqual(entry[2], kind)
            self.assertEqual(entry[4], end)
            end = entry[4] + entry[5]
        self.assertEqual(end * 512, boot_slots.IMAGE_SIZE)


class FlashSafetyTests(unittest.TestCase):
    def test_external_apfs_system_disk_is_protected(self):
        apfs = {"Containers": [{"Volumes": [{"MountPoint": "/System/Volumes/Data"}],
                                 "PhysicalStores": [{"DeviceIdentifier": "disk4s2"}]}]}
        self.assertIn("disk4", flash.mac_system_disks({"ParentWholeDisk": "disk5"}, apfs))

    def test_mac_rejects_internal_disk_and_partition(self):
        info = {"DeviceIdentifier": "disk0", "Whole": True, "Internal": True,
                "VirtualOrPhysical": "Physical", "Writable": True, "TotalSize": 512_000_000_000}
        with self.assertRaises(ValueError):
            flash.mac_candidate(info)
        with self.assertRaises(ValueError):
            flash.mac_candidate(dict(info, Internal=False, DeviceIdentifier="disk4s1"))

    def test_mac_accepts_external_physical_media(self):
        info = {"DeviceIdentifier": "disk4", "Whole": True, "Internal": False,
                "VirtualOrPhysical": "Physical", "Writable": True, "TotalSize": 16_000_000_000}
        self.assertEqual(flash.mac_candidate(info)["device"], "/dev/disk4")

    def test_linux_rejects_system_partition_even_on_usb(self):
        info = {"path": "/dev/sda", "type": "disk", "tran": "usb", "size": 16_000_000_000,
                "children": [{"type": "part", "mountpoints": ["/"]}]}
        with self.assertRaises(ValueError):
            flash.linux_candidate(info)

    def test_linux_accepts_removable_sd_and_rejects_nonremovable_sata(self):
        info = {"path": "/dev/mmcblk0", "type": "disk", "rm": True, "size": 16_000_000_000,
                "children": [{"type": "part", "mountpoints": ["/media/user/CARD"]}]}
        self.assertEqual(flash.linux_candidate(info)["device"], "/dev/mmcblk0")
        with self.assertRaises(ValueError):
            flash.linux_candidate(dict(info, rm=False, tran="sata"))

    def test_checksum_verification_rejects_corrupt_image(self):
        with tempfile.TemporaryDirectory() as tmp:
            image = Path(tmp) / "boot.img"
            with image.open("wb") as f:
                f.truncate(64 * 1024 * 1024)
            with image.open("rb") as f:
                expected = hashlib.file_digest(f, "sha256").hexdigest()
            image.with_suffix(".img.sha256").write_text(f"{expected}  boot.img\n")
            self.assertEqual(flash.verify_image(image), (64 * 1024 * 1024, expected))
            with image.open("r+b") as f:
                f.write(b"corrupt")
            with self.assertRaises(ValueError):
                flash.verify_image(image)


if __name__ == "__main__":
    unittest.main()

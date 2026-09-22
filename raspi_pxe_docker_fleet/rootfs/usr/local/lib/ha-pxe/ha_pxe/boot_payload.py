"""Build matching kernel/initramfs payloads for the retrying SD loader."""
from __future__ import annotations

import gzip
import hashlib
import ipaddress
import json
import lzma
import re
import shutil
import stat
import subprocess
import tempfile
import zlib
from pathlib import Path

from .errors import HaPxeError

TEMPLATES = Path(__file__).resolve().parent.parent / "templates/boot-media"
MODELS = {"pi2": ("kernel7.img", "arm", 0, "bootz"),
          "pi3": ("kernel8.img", "arm64", 1, "booti"),
          "pi3plus": ("kernel8.img", "arm64", 1, "booti")}
SEEDS = ("dwc2", "dwc_otg", "usbnet", "smsc95xx", "lan78xx", "af_packet", "nfs", "nfsv3")


def boot_script(model: str, serial: str, server: str) -> tuple[str, str]:
    server = str(ipaddress.IPv4Address(server))
    if model not in MODELS or not re.fullmatch(r"[0-9a-f]{1,16}", serial):
        raise ValueError("Invalid boot model or serial")
    text = (TEMPLATES / "boot.cmd").read_text()
    if len(serial) <= 8:
        # The add-on also accepts the low 32 bits used by Raspberry Pi TFTP.
        text = text.replace('if test "${serial#}" != "@SERIAL16@"; then',
            'setexpr pxe_board_id ${serial#} \\& ffffffff\n'
            '    setexpr pxe_expected_id @SERIAL@ \\& ffffffff\n'
            '    if test "${pxe_board_id}" != "${pxe_expected_id}"; then')
    for key, value in {"SERVER": server, "SERIAL": serial, "SERIAL16": serial.zfill(16),
                       "MODEL": model, "BOOT_COMMAND": MODELS[model][3]}.items():
        text = text.replace(f"@{key}@", value)
    digest = hashlib.sha256(text.encode()).hexdigest()
    return f"setenv pxe_script_sha {digest}\n" + text, digest


def publish_payload(boot: Path, modules: Path, busybox: Path, model: str, serial: str, server: str) -> None:
    """Publish immutable generations, then atomically switch the small manifest."""
    kernel_name, _, arm64, _ = MODELS[model]
    kernel = (boot / kernel_name).read_bytes()
    if arm64 and kernel.startswith(b"\x1f\x8b"):
        kernel = gzip.decompress(kernel)
    if (arm64 and kernel[56:60] != b"ARM\x64") or (not arm64 and kernel[36:40] != b"\x18\x28\x6f\x01"):
        raise ValueError(f"Kernel architecture does not match {model}")
    release = kernel_release(kernel)
    module_dir = inside(modules, release)
    root, bootargs = root_settings((boot / "cmdline.txt").read_text(), server)
    destination = boot / "ha-pxe" / model
    destination.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=destination, prefix=".build-") as tmp:
        stage = Path(tmp)
        payload = stage / "payload"
        payload.mkdir()
        (payload / "kernel.img").write_bytes(kernel)
        make_initramfs(module_dir, busybox, server, root, payload / "initramfs.gz")
        if len(kernel) > 64 * 1024 * 1024 or (payload / "initramfs.gz").stat().st_size > 128 * 1024 * 1024:
            raise ValueError("Boot payload exceeds reserved memory")
        kernel_sha, initrd_sha = sha(payload / "kernel.img"), sha(payload / "initramfs.gz")
        generation = hashlib.sha256((kernel_sha + initrd_sha + bootargs).encode()).hexdigest()[:20]
        target = destination / generation
        if target.exists():
            if sha(target / "kernel.img") != kernel_sha or sha(target / "initramfs.gz") != initrd_sha:
                raise ValueError("Existing boot generation is corrupt")
        else:
            payload.rename(target)
        script, digest = boot_script(model, serial, server)
        update = {"format": 1, "model": model, "serial": serial, "script": script,
                  "script_sha256": hashlib.sha256(script.encode()).hexdigest(), "loader": digest}
        # The script and its identity travel in one atomic file, avoiding a manifest/blob race.
        (stage / "sd-update.json").write_text(json.dumps(update) + "\n")
        (stage / "sd-update.json").replace(destination / "sd-update.json")
        (stage / "boot.env").write_text(
            f"pxe_generation={generation}\npxe_kernel_sha={kernel_sha}\n"
            f"pxe_initrd_sha={initrd_sha}\npxe_bootargs={bootargs}\n")
        (stage / "boot.env").replace(destination / "boot.env")


def provision_boot_media(context, boot: Path, root: Path, model: str, arch: str, serial: str, server: str) -> None:
    if model not in {"pi2", "pi3"}:
        return
    expected_arch = "armhf" if model == "pi2" else "arm64"
    if arch != expected_arch:
        context.logger.warning(f"Retrying SD media requires {model} image_arch={expected_arch}; skipping its payload")
        return
    modules = root / "usr/lib/modules"
    if not modules.is_dir():
        modules = root / "lib/modules"
    busybox = context.paths.library_dir / "boot-tools" / model / "busybox"
    try:
        for board in (("pi2",) if model == "pi2" else ("pi3", "pi3plus")):
            publish_payload(boot, modules, busybox, board, serial, server)
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        raise HaPxeError(f"Client {serial}: cannot prepare matching SD network payload: {exc}") from exc
    context.logger.info(f"Client {serial}: published matching retry boot payload and automatic SD instructions")


def sha(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def inside(root: Path, name: str) -> Path:
    path = root / name
    if Path(name).is_absolute() or ".." in Path(name).parts or not path.resolve().is_relative_to(root.resolve()):
        raise ValueError(f"Path leaves the input tree: {name}")
    return path


def kernel_release(data: bytes) -> str:
    candidates = [data]
    for match in list(re.finditer(b"\x1f\x8b\x08", data))[:32]:
        try:
            candidates.append(zlib.decompressobj(31).decompress(data[match.start():], 128 * 1024 * 1024))
        except zlib.error:
            continue
    for candidate in candidates:
        match = re.search(rb"Linux version ([A-Za-z0-9_.+\-]+) ", candidate)
        if match:
            return match[1].decode("ascii")
    raise ValueError("Cannot identify the kernel release; supply an unmodified Raspberry Pi OS kernel image")


def root_settings(cmdline: str, server: str) -> tuple[str, str]:
    tokens = cmdline.split()
    roots = [x.split("=", 1)[1].split(",", 1)[0] for x in tokens if x.startswith("nfsroot=")]
    if len(roots) != 1 or ":" not in roots[0]:
        raise ValueError("cmdline.txt must come from a provisioned client and contain exactly one nfsroot=SERVER:PATH")
    old_server, root = roots[0].split(":", 1)
    if str(ipaddress.IPv4Address(old_server)) != server:
        raise ValueError("--server must match cmdline.txt and the add-on server_ip; reprovision before preparing media")
    if not re.fullmatch(r"/[A-Za-z0-9_./-]+", root) or ".." in Path(root).parts:
        raise ValueError("Unsupported NFS export path")
    drop = ("root=", "rootfstype=", "nfsroot=", "ip=", "init=", "rdinit=", "panic=", "net.ifnames=")
    keep = [t for t in tokens if not t.startswith(drop) and t not in {"rootwait", "rw", "ro"}]
    return root, " ".join(keep + ["rdinit=/init", "rw", "panic=10", "net.ifnames=0"])


def module_name(path: str) -> str:
    return Path(path).name.split(".ko", 1)[0].replace("-", "_")


def module_closure(module_dir: Path) -> tuple[list[str], dict[str, list[str]]]:
    deps: dict[str, list[str]] = {}
    for line in (module_dir / "modules.dep").read_text().splitlines():
        name, sep, values = line.partition(":")
        if not sep:
            raise ValueError("Malformed modules.dep")
        inside(module_dir, name)
        deps[name] = values.split()
        for value in deps[name]:
            inside(module_dir, value)
    names = {module_name(name): name for name in deps}
    soft: dict[str, list[str]] = {}
    soft_file = module_dir / "modules.softdep"
    if soft_file.exists():
        for line in soft_file.read_text().splitlines():
            fields = line.split()
            if len(fields) > 2 and fields[0] == "softdep":
                soft[fields[1].replace("-", "_")] = [x.replace("-", "_") for x in fields[2:] if x not in {"pre:", "post:"}]
    selected: set[str] = set()

    def add(name: str) -> None:
        if name in selected:
            return
        if name not in deps:
            raise ValueError(f"Missing module dependency: {name}")
        selected.add(name)
        for dependency in deps[name]:
            add(dependency)
        for dependency in soft.get(module_name(name), []):
            if dependency in names:
                add(names[dependency])

    for seed in SEEDS:
        if seed in names:
            add(names[seed])
    return sorted(selected), deps


def unpack_module(path: Path) -> bytes:
    data = path.read_bytes()
    if path.suffix == ".xz":
        return lzma.decompress(data)
    if path.suffix == ".gz":
        return gzip.decompress(data)
    if path.suffix == ".zst":
        return subprocess.check_output(["zstd", "-d", "-q", "-c"], input=data)
    return data


def plain_module(name: str) -> str:
    return name[:name.index(".ko") + 3]


def cpio_archive(tree: Path) -> bytes:
    """Create newc, including console device nodes, without privileged mknod."""
    output = bytearray()
    inode = 0

    def entry(name: str, mode: int, data: bytes = b"", major: int = 0, minor: int = 0) -> None:
        nonlocal inode
        inode += 1
        name_bytes = name.encode() + b"\0"
        fields = (inode, mode, 0, 0, 2 if stat.S_ISDIR(mode) else 1, 0,
                  len(data), 0, 0, major, minor, len(name_bytes), 0)
        output.extend(b"070701" + "".join(f"{value:08x}" for value in fields).encode())
        output.extend(name_bytes)
        output.extend(bytes(-len(output) % 4))
        output.extend(data)
        output.extend(bytes(-len(output) % 4))

    for path in sorted(tree.rglob("*")):
        mode = path.lstat().st_mode
        data = str(path.readlink()).encode() if path.is_symlink() else path.read_bytes() if path.is_file() else b""
        entry(str(path.relative_to(tree)), mode, data)
    entry("dev/console", stat.S_IFCHR | 0o600, major=5, minor=1)
    entry("dev/null", stat.S_IFCHR | 0o666, major=1, minor=3)
    entry("TRAILER!!!", 0)
    output.extend(bytes(-len(output) % 512))
    return bytes(output)


def make_initramfs(modules: Path, busybox: Path, server: str, root: str, output: Path) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        tree = Path(tmp)
        for name in ("bin", "etc", "proc", "sys", "dev", "run", "newroot", "tmp"):
            (tree / name).mkdir()
        shutil.copy2(busybox, tree / "bin/busybox")
        (tree / "bin/busybox").chmod(0o755)
        (tree / "bin/sh").symlink_to("busybox")
        for source, target in (("init", "init"), ("udhcpc.script", "etc/udhcpc.script")):
            shutil.copy2(TEMPLATES / source, tree / target)
            (tree / target).chmod(0o755)
        (tree / "etc/pxe.conf").write_text(f"PXE_SERVER='{server}'\nPXE_ROOT='{root}'\n")
        (tree / "etc/pxe.modules").write_text("\n".join(SEEDS) + "\n")
        (tree / "etc/resolv.conf").touch()
        selected, deps = module_closure(modules)
        destination = tree / "lib/modules" / modules.name
        destination.mkdir(parents=True)
        for name in selected:
            target = destination / plain_module(name)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(unpack_module(inside(modules, name)))
        (destination / "modules.dep").write_text("".join(
            f"{plain_module(name)}: {' '.join(plain_module(dep) for dep in deps[name])}\n" for name in selected))
        for name in ("modules.builtin", "modules.order", "modules.softdep"):
            if (modules / name).is_file():
                shutil.copy2(modules / name, destination / name)
        output.write_bytes(gzip.compress(cpio_archive(tree), mtime=0))

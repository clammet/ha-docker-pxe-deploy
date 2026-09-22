#!/usr/bin/env python3.14
"""Build a standalone recovery SD card. No HA host or deployed OS is needed."""
from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
LIB = BASE.parent / 'raspi_pxe_docker_fleet/rootfs/usr/local/lib/ha-pxe'
sys.path.insert(0, str(LIB))
from ha_pxe.boot_payload import MODELS, boot_script, sha
from ha_pxe.boot_slots import FAT_SECTORS, IMAGE_SIZE, SLOT_STARTS, mbr, script_image

DTBS = {'pi2': ('bcm2709-rpi-2-b.dtb', 'bcm2710-rpi-2-b.dtb'),
        'pi3': ('bcm2710-rpi-3-b.dtb',), 'pi3plus': ('bcm2710-rpi-3-b-plus.dtb',)}


def fetch(url: str) -> bytes:
    request = urllib.request.Request(url, headers={'User-Agent': 'ha-pxe-boot-media'})
    with urllib.request.urlopen(request, timeout=120) as response:
        return response.read()


def firmware(model: str, revision: str | None) -> tuple[Path, str]:
    # Resolve once; every file comes from exactly the same immutable commit.
    if revision is None:
        revision = json.loads(fetch('https://api.github.com/repos/raspberrypi/firmware/commits/stable'))['sha']
    if not re.fullmatch(r'[0-9a-f]{40}', revision):
        raise ValueError('--firmware-revision must be a full commit SHA')
    directory = BASE / '.cache/firmware' / revision / model
    directory.mkdir(parents=True, exist_ok=True)
    names = ('bootcode.bin', 'start.elf', 'fixup.dat', *DTBS[model], 'LICENCE.broadcom')
    manifest = directory / 'SHA256SUMS.json'
    previous = json.loads(manifest.read_text()) if manifest.exists() else {}
    for name in names:
        path = directory / name
        if not path.is_file() or previous.get(name) != sha(path):
            data = fetch(f'https://raw.githubusercontent.com/raspberrypi/firmware/{revision}/boot/{name}')
            if not data:
                raise ValueError(f'Empty firmware file: {name}')
            temp = path.with_suffix(path.suffix + '.download')
            temp.write_bytes(data)
            temp.replace(path)
    manifest.write_text(json.dumps({name: sha(directory / name) for name in names}, indent=2) + '\n')
    return directory, revision


def make_image(card: Path, image: Path) -> None:
    with image.open('wb') as stream:
        stream.write(mbr())
        stream.truncate(IMAGE_SIZE)
    subprocess.run(['mkfs.fat', '--invariant', '-F', '32', '-n', 'HA_PXE', '--offset=2048',
                    str(image), str(FAT_SECTORS // 2)], check=True)
    for path in sorted(card.iterdir()):
        subprocess.run(['mcopy', '-s', '-i', f'{image}@@1048576', str(path), '::/'], check=True)
    image.with_suffix('.img.sha256').write_text(f'{sha(image)}  {image.name}\n')


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--model', choices=MODELS, required=True)
    parser.add_argument('--serial', required=True, help='Client serial, exactly as configured in HA')
    parser.add_argument('--server', required=True, help='Reserved IPv4 address of the future HA server')
    parser.add_argument('--firmware-revision', help='Reuse an exact firmware commit (otherwise resolve stable)')
    args = parser.parse_args()
    serial = args.serial.lower().removeprefix('0x')
    if not re.fullmatch(r'[0-9a-f]{1,16}', serial):
        parser.error('serial must contain 1–16 hexadecimal digits')
    server = str(ipaddress.IPv4Address(args.server))
    binaries = BASE / 'out/bin' / args.model
    subprocess.run(['sha256sum', '--check', 'SHA256SUMS'], cwd=binaries, check=True)
    destination = BASE / 'out' / f'{serial}-{args.model}'
    if destination.exists():
        raise ValueError(f'{destination.name} already exists; move it aside to keep the previous image')
    source, revision = firmware(args.model, args.firmware_revision)
    with tempfile.TemporaryDirectory(dir=BASE / 'out', prefix='prepare-') as tmp:
        work = Path(tmp)
        card = work / 'sd'
        card.mkdir()
        for name in ('bootcode.bin', 'start.elf', 'fixup.dat', *DTBS[args.model], 'LICENCE.broadcom'):
            shutil.copy2(source / name, card / name)
        shutil.copy2(binaries / 'u-boot.bin', card / 'u-boot.bin')
        (card / 'config.txt').write_text(
            f'[all]\nkernel=u-boot.bin\narm_64bit={MODELS[args.model][2]}\n'
            'auto_initramfs=0\ncmdline=uboot-cmdline.txt\nenable_uart=1\n')
        (card / 'uboot-cmdline.txt').write_text('\n')
        script, digest = boot_script(args.model, serial, server)
        (card / 'fallback.scr').write_bytes(script_image(script, args.model))
        recovery = (BASE / 'templates/recovery.cmd').read_text()
        recovery = recovery.replace('@SLOT_A@', hex(SLOT_STARTS[0])).replace('@SLOT_B@', hex(SLOT_STARTS[1]))
        (card / 'boot.scr').write_bytes(script_image(recovery, args.model))
        metadata = {'format': 1, 'model': args.model, 'serial': serial, 'server': server,
                    'firmware_revision': revision, 'loader': digest, 'uboot_version': '2026.07'}
        (card / 'ha-pxe-card.json').write_text(json.dumps(metadata, indent=2) + '\n')
        (work / 'metadata.json').write_text(json.dumps(metadata, indent=2) + '\n')
        make_image(card, work / 'boot.img')
        work.rename(destination)
    print(f'Prepared out/{destination.name}/boot.img. No server files need to be copied.')


if __name__ == '__main__':
    main()

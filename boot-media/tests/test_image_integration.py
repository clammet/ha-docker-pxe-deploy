"""Real FAT, U-Boot image and initramfs packaging tests; no hardware required."""
from __future__ import annotations

import gzip
import importlib.util
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

BASE = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('prepare_integration', BASE / 'scripts/prepare.py')
prepare = importlib.util.module_from_spec(spec)
spec.loader.exec_module(prepare)
from ha_pxe import boot_payload
from ha_pxe.boot_slots import SLOT_STARTS, SLOT_BYTES, parse_script


@unittest.skipUnless(shutil.which('mkfs.fat') and shutil.which('mcopy') and shutil.which('mkimage'), 'requires builder image')
class ImageIntegrationTests(unittest.TestCase):
    def test_standalone_cards_for_all_boards_without_an_os_or_server(self):
        for model in prepare.MODELS:
            with self.subTest(model=model), tempfile.TemporaryDirectory() as tmp:
                base = Path(tmp)
                (base / 'out').mkdir()
                (base / 'out/bin').symlink_to(BASE / 'out/bin')
                shutil.copytree(BASE / 'templates', base / 'templates')
                firmware = base / 'firmware'
                firmware.mkdir()
                for name in ('bootcode.bin', 'start.elf', 'fixup.dat', 'LICENCE.broadcom', *prepare.DTBS[model]):
                    (firmware / name).write_bytes(b'FIXTURE ONLY')
                argv = ['prepare', '--model', model, '--server', '192.0.2.10', '--serial', 'abcd']
                with patch.object(prepare, 'BASE', base), patch.object(sys, 'argv', argv), \
                        patch.object(prepare, 'firmware', return_value=(firmware, 'a' * 40)):
                    prepare.main()
                output = base / f'out/abcd-{model}'
                image = output / 'boot.img'
                config = subprocess.check_output(['mtype', '-i', f'{image}@@1048576', '::/config.txt']).decode()
                self.assertIn('kernel=u-boot.bin', config)
                for filename in ('boot.scr', 'fallback.scr'):
                    subprocess.run(['mkimage', '-l', str(output / 'sd' / filename)], check=True)
                    parse_script((output / 'sd' / filename).read_bytes())
                with image.open('rb') as stream:
                    for offset in SLOT_STARTS:
                        stream.seek(offset * 512)
                        self.assertEqual(stream.read(SLOT_BYTES), bytes(SLOT_BYTES))
                self.assertFalse((output / 'server').exists())

    def test_server_payload_uses_matching_modules_and_atomic_generations(self):
        for model in prepare.MODELS:
            with self.subTest(model=model), tempfile.TemporaryDirectory() as tmp:
                base = Path(tmp)
                boot = base / 'boot'
                boot.mkdir()
                (boot / 'cmdline.txt').write_text('console=tty1 root=/dev/nfs nfsroot=192.0.2.10:/data/exports/abcd/root,vers=3 ip=dhcp rootwait\n')
                kernel_name, _, arm64, _ = prepare.MODELS[model]
                data = bytearray(4096)
                data[56:60] = b'ARM\x64' if arm64 else bytes(4)
                if not arm64:
                    data[36:40] = b'\x18\x28\x6f\x01'
                data.extend(b'Linux version 6.12.20-fixture (builder) ')
                (boot / kernel_name).write_bytes(gzip.compress(data) if arm64 else data)
                modules = base / 'modules/6.12.20-fixture'
                modules.mkdir(parents=True)
                (modules / 'modules.dep').write_text('kernel/nfs.ko: kernel/sunrpc.ko\nkernel/sunrpc.ko:\n')
                (modules / 'kernel').mkdir()
                (modules / 'kernel/nfs.ko').write_bytes(b'MODULE FIXTURE')
                (modules / 'kernel/sunrpc.ko').write_bytes(b'MODULE FIXTURE')
                args = (boot, modules.parent, BASE / 'out/bin' / model / 'busybox', model, 'abcd', '192.0.2.10')
                boot_payload.publish_payload(*args)
                destination = boot / 'ha-pxe' / model
                manifest = (destination / 'boot.env').read_text()
                fields = dict(line.split('=', 1) for line in manifest.splitlines())
                generation = fields['pxe_generation']
                archive = gzip.decompress((destination / generation / 'initramfs.gz').read_bytes())
                listing = subprocess.check_output(['cpio', '-itv'], input=archive, stderr=subprocess.DEVNULL).decode()
                self.assertIn('kernel/sunrpc.ko', listing)
                self.assertIn('bin/sh -> busybox', listing)
                self.assertTrue(any(line.startswith('crw') and line.endswith('dev/console') for line in listing.splitlines()))
                self.assertTrue(any(line.startswith('-rwx') and line.endswith(' init') for line in listing.splitlines()))
                update = json.loads((destination / 'sd-update.json').read_text())
                self.assertEqual(update['model'], model)
                self.assertEqual(update['serial'], 'abcd')
                boot_payload.publish_payload(*args)
                self.assertEqual((destination / 'boot.env').read_text(), manifest)
                # A missing kernel/module pair must leave the prior manifest usable.
                (modules / 'modules.dep').unlink()
                with self.assertRaises(FileNotFoundError):
                    boot_payload.publish_payload(*args)
                self.assertEqual((destination / 'boot.env').read_text(), manifest)
                self.assertFalse(list(destination.glob('.build-*')))


if __name__ == '__main__':
    unittest.main()

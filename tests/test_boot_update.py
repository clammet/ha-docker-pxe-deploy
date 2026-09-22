from __future__ import annotations

import hashlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

LIB = Path(__file__).resolve().parents[1] / 'raspi_pxe_docker_fleet/rootfs/usr/local/lib/ha-pxe'
sys.path.insert(0, str(LIB))
from ha_pxe.boot_slots import IMAGE_SIZE, SLOT_STARTS, SLOT_BYTES, mbr, parse_script, script_image
from ha_pxe.boot_payload import boot_script
from ha_pxe.client.boot_update import install, read_slot, validate_update


class BootSlotTests(unittest.TestCase):
    def card(self):
        file = tempfile.TemporaryFile()
        file.write(mbr())
        file.truncate(IMAGE_SIZE)
        self.addCleanup(file.close)
        return file

    def test_crc_detects_torn_header_and_body(self):
        image = script_image('echo test\n', 'pi2', 4)
        self.assertEqual(parse_script(image), (4, 'echo test\n', 'pi2'))
        for offset in (0, 8, 63, 75, len(image) - 1):
            corrupt = bytearray(image)
            corrupt[offset] ^= 1
            with self.assertRaises(ValueError):
                parse_script(corrupt)
        with self.assertRaises(ValueError):
            parse_script(image[:-1])

    def test_alternation_preserves_active_slot_and_skips_identical_update(self):
        card = self.card()
        self.assertTrue(install(card, 'echo first\n', 'pi2', 0))
        self.assertEqual(read_slot(card, 0)[:2], (0, 'echo first\n'))
        self.assertTrue(install(card, 'echo second\n', 'pi2', 2))
        self.assertEqual(read_slot(card, 0)[:2], (0, 'echo first\n'))
        self.assertEqual(read_slot(card, 1)[:2], (1, 'echo second\n'))
        with patch('ha_pxe.client.boot_update.os.fsync') as sync:
            self.assertFalse(install(card, 'echo second\n', 'pi2', 2))
            sync.assert_not_called()
        self.assertTrue(install(card, 'echo third\n', 'pi2', 3))
        self.assertEqual(read_slot(card, 0)[:2], (2, 'echo third\n'))
        self.assertEqual(read_slot(card, 1)[:2], (1, 'echo second\n'))

    def test_interruptions_leave_active_and_recovery_regions_untouched(self):
        for stage in (1, 2, 3):
            with self.subTest(stage=stage):
                card = self.card()
                install(card, 'echo known_good\n', 'pi2', 0)
                card.seek(0)
                recovery = card.read(4096)
                calls = 0
                real_write = card.write
                def interrupted(data):
                    nonlocal calls
                    calls += 1
                    if calls == stage:
                        real_write(data[:len(data) // 2])
                        raise OSError('simulated power loss')
                    return real_write(data)
                with patch.object(card, 'write', side_effect=interrupted):
                    with self.assertRaises(OSError):
                        install(card, 'echo new\n' * 1000, 'pi2', 2)
                self.assertEqual(read_slot(card, 0)[:2], (0, 'echo known_good\n'))
                candidate = read_slot(card, 1)
                # A fully committed header can validate, but a torn body cannot.
                self.assertTrue(candidate is None or candidate[1] == 'echo new\n' * 1000)
                card.seek(0)
                self.assertEqual(card.read(4096), recovery)

    def test_unknown_card_layout_is_rejected_before_writing(self):
        card = self.card()
        card.seek(440)
        card.write(b'NOPE')
        with patch.object(card, 'write') as write:
            with self.assertRaises(ValueError):
                install(card, 'echo new', 'pi2', 0)
            write.assert_not_called()

    def test_server_rollback_supersedes_a_newer_local_slot(self):
        card = self.card()
        install(card, 'echo old\n', 'pi2', 0)
        install(card, 'echo new\n', 'pi2', 2)
        self.assertTrue(install(card, 'echo old\n', 'pi2', 3))
        self.assertEqual(read_slot(card, 0)[:2], (2, 'echo old\n'))
        self.assertEqual(read_slot(card, 1)[:2], (1, 'echo new\n'))

    def test_current_factory_instructions_do_not_write_blank_slots(self):
        card = self.card()
        with patch('ha_pxe.client.boot_update.os.fsync') as sync:
            self.assertFalse(install(card, 'echo factory\n', 'pi2', 0, running_matches=True))
            sync.assert_not_called()
        self.assertIsNone(read_slot(card, 0))

    def test_manifest_checks_model_serial_hash_and_loader_identity(self):
        script, digest = boot_script('pi3plus', 'abcd', '192.0.2.10')
        data = dict(format=1, model='pi3plus', serial='abcd', script=script,
                    loader=digest, script_sha256=hashlib.sha256(script.encode()).hexdigest())
        self.assertEqual(validate_update(data, 'pi3plus', 'abcd'), script)
        for key, value in (('serial', 'dcba'), ('model', 'pi2'), ('format', 2),
                           ('script', script + 'corrupt'), ('loader', '0' * 64)):
            with self.subTest(key=key), self.assertRaises(ValueError):
                validate_update(dict(data, **{key: value}), 'pi3plus', 'abcd')


if __name__ == '__main__':
    unittest.main()

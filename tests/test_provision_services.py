from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


LIB_DIR = Path(__file__).resolve().parents[1] / "raspi_pxe_docker_fleet" / "rootfs" / "usr" / "local" / "lib" / "ha-pxe"
if str(LIB_DIR) not in sys.path:
    sys.path.insert(0, str(LIB_DIR))

from ha_pxe.addon_context import AddonContext, AddonPaths
from ha_pxe.provision import _disable_stock_firstboot_services, _prepare_networkmanager_rootfs, _write_bootstrap_files


class DisableStockFirstbootServicesTests(unittest.TestCase):
    def test_masks_stock_firstboot_and_wait_online_services(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir_name:
            root_dir = Path(temp_dir_name)
            multi_user_wants = root_dir / "etc" / "systemd" / "system" / "multi-user.target.wants"
            multi_user_wants.mkdir(parents=True)
            userconfig_wants = multi_user_wants / "userconfig.service"
            userconfig_wants.write_text("", encoding="utf-8")

            rename_config = root_dir / "etc" / "ssh" / "sshd_config.d" / "rename_user.conf"
            rename_config.parent.mkdir(parents=True)
            rename_config.write_text("Banner /usr/share/userconf-pi/sshd_banner\n", encoding="utf-8")

            banner_path = root_dir / "usr" / "share" / "userconf-pi" / "sshd_banner"
            banner_path.parent.mkdir(parents=True)
            banner_path.write_text("Stock banner\n", encoding="utf-8")

            _disable_stock_firstboot_services(root_dir)

            self.assertTrue((root_dir / "etc" / "systemd" / "system" / "userconfig.service").is_symlink())
            self.assertEqual((root_dir / "etc" / "systemd" / "system" / "userconfig.service").readlink(), Path("/dev/null"))
            self.assertTrue((root_dir / "etc" / "systemd" / "system" / "systemd-firstboot.service").is_symlink())
            self.assertEqual(
                (root_dir / "etc" / "systemd" / "system" / "systemd-firstboot.service").readlink(),
                Path("/dev/null"),
            )
            self.assertTrue(
                (root_dir / "etc" / "systemd" / "system" / "systemd-networkd-wait-online.service").is_symlink()
            )
            self.assertEqual(
                (root_dir / "etc" / "systemd" / "system" / "systemd-networkd-wait-online.service").readlink(),
                Path("/dev/null"),
            )
            self.assertFalse(userconfig_wants.exists())
            self.assertFalse(rename_config.exists())
            self.assertEqual(banner_path.read_text(encoding="utf-8"), "")


class PrepareNetworkManagerRootfsTests(unittest.TestCase):
    def test_prepare_networkmanager_rootfs_masks_conflicting_services_and_writes_config(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir_name:
            root_dir = Path(temp_dir_name)
            resolv_path = root_dir / "etc" / "resolv.conf"
            resolv_path.parent.mkdir(parents=True)
            resolv_path.write_text("nameserver 8.8.8.8\n", encoding="utf-8")
            networkmanager_service = root_dir / "usr" / "lib" / "systemd" / "system" / "NetworkManager.service"
            networkmanager_service.parent.mkdir(parents=True)
            networkmanager_service.write_text("[Unit]\nDescription=NetworkManager\n", encoding="utf-8")
            wait_online_service = root_dir / "usr" / "lib" / "systemd" / "system" / "NetworkManager-wait-online.service"
            wait_online_service.write_text("[Unit]\nDescription=NetworkManager wait online\n", encoding="utf-8")

            multi_user_wants = root_dir / "etc" / "systemd" / "system" / "multi-user.target.wants"
            network_online_wants = root_dir / "etc" / "systemd" / "system" / "network-online.target.wants"
            multi_user_wants.mkdir(parents=True)
            network_online_wants.mkdir(parents=True)
            for service in ("dhcpcd.service", "networking.service", "systemd-networkd.service"):
                (multi_user_wants / service).write_text("", encoding="utf-8")
                (network_online_wants / service).write_text("", encoding="utf-8")

            _prepare_networkmanager_rootfs(root_dir)

            self.assertEqual(
                (root_dir / "etc" / "NetworkManager" / "conf.d" / "90-ha-pxe.conf").read_text(encoding="utf-8"),
                "# Managed by HA-PXE\n[main]\ndns=none\n\n[ifupdown]\nmanaged=true\n",
            )
            self.assertFalse(resolv_path.is_symlink())
            self.assertEqual(
                resolv_path.read_text(encoding="utf-8"),
                "# Managed by HA-PXE; populated from /proc/net/pnp on first boot\n",
            )
            self.assertTrue((multi_user_wants / "NetworkManager.service").is_symlink())
            self.assertEqual(
                (multi_user_wants / "NetworkManager.service").readlink(),
                Path("/usr/lib/systemd/system/NetworkManager.service"),
            )
            self.assertTrue((network_online_wants / "NetworkManager-wait-online.service").is_symlink())
            self.assertEqual(
                (network_online_wants / "NetworkManager-wait-online.service").readlink(),
                Path("/usr/lib/systemd/system/NetworkManager-wait-online.service"),
            )
            for service in ("dhcpcd.service", "networking.service", "systemd-networkd.service"):
                service_path = root_dir / "etc" / "systemd" / "system" / service
                self.assertTrue(service_path.is_symlink())
                self.assertEqual(service_path.readlink(), Path("/dev/null"))
                self.assertFalse((multi_user_wants / service).exists())
                self.assertFalse((network_online_wants / service).exists())

    def test_prepare_networkmanager_rootfs_preserves_existing_resolv_conf_after_firstboot(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir_name:
            root_dir = Path(temp_dir_name)
            resolv_path = root_dir / "etc" / "resolv.conf"
            resolv_path.parent.mkdir(parents=True)
            resolv_path.write_text("nameserver 192.168.25.1\n", encoding="utf-8")

            marker = root_dir / "var" / "lib" / "ha-pxe" / "firstboot.done"
            marker.parent.mkdir(parents=True)
            marker.write_text("", encoding="utf-8")

            networkmanager_service = root_dir / "usr" / "lib" / "systemd" / "system" / "NetworkManager.service"
            networkmanager_service.parent.mkdir(parents=True)
            networkmanager_service.write_text("[Unit]\nDescription=NetworkManager\n", encoding="utf-8")
            wait_online_service = root_dir / "usr" / "lib" / "systemd" / "system" / "NetworkManager-wait-online.service"
            wait_online_service.write_text("[Unit]\nDescription=NetworkManager wait online\n", encoding="utf-8")

            _prepare_networkmanager_rootfs(root_dir)

            self.assertEqual(resolv_path.read_text(encoding="utf-8"), "nameserver 192.168.25.1\n")


class WriteBootstrapFilesTests(unittest.TestCase):
    def test_write_bootstrap_files_enables_command_listener_and_writes_command_transport_config(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir_name:
            root_dir = Path(temp_dir_name)
            context = AddonContext(paths=AddonPaths(root=root_dir))
            context._config_cache = {
                "default_username": "pi",
                "default_password": "secret",
                "ssh_authorized_keys": "",
                "default_timezone": "",
                "default_keyboard_layout": "",
                "default_locale": "",
            }

            with patch("ha_pxe.provision.capture", return_value="hashed-password"):
                _write_bootstrap_files(
                    context,
                    root_dir,
                    {"log_level": "warn"},
                    "cdc843d7",
                    "janky",
                    "192.0.2.10",
                    "[]\n",
                )

            bootstrap_env = (root_dir / "etc" / "ha-pxe" / "bootstrap.env").read_text(encoding="utf-8")
            self.assertIn("PXE_LOG_LEVEL=warn\n", bootstrap_env)
            self.assertIn("PXE_COMMAND_HOST=192.0.2.10\n", bootstrap_env)
            self.assertIn("PXE_COMMAND_PORT=8099\n", bootstrap_env)
            self.assertIn("PXE_COMMAND_PATH=/client-command\n", bootstrap_env)
            self.assertTrue((root_dir / "usr" / "local" / "sbin" / "ha-pxe-command-listener").exists())
            command_listener_service = root_dir / "etc" / "systemd" / "system" / "ha-pxe-command-listener.service"
            self.assertTrue(command_listener_service.exists())
            command_listener_text = command_listener_service.read_text(encoding="utf-8")
            self.assertIn("ConditionPathExists=/var/lib/ha-pxe/firstboot.done\n", command_listener_text)
            enabled_service = root_dir / "etc" / "systemd" / "system" / "multi-user.target.wants" / "ha-pxe-command-listener.service"
            self.assertTrue(enabled_service.is_symlink())
            self.assertEqual(enabled_service.readlink(), Path("../ha-pxe-command-listener.service"))
            updater = root_dir / "etc/systemd/system/ha-pxe-boot-update.service"
            self.assertIn("ConditionKernelCommandLine=ha_pxe.media=1", updater.read_text())
            self.assertIn("RequiresMountsFor=/boot/firmware", updater.read_text())
            self.assertEqual(
                (root_dir / "etc/systemd/system/timers.target.wants/ha-pxe-boot-update.timer").readlink(),
                Path("../ha-pxe-boot-update.timer"),
            )


if __name__ == "__main__":
    unittest.main()

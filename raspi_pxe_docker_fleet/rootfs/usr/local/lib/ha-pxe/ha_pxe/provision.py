"""Client export provisioning for the add-on."""

from __future__ import annotations

import json
import os
from pathlib import Path

from .addon_context import AddonContext
from .boot_payload import provision_boot_media
from .container_specs import normalize_container_specs, specs_to_json
from .envfile import format_env_file
from .errors import HaPxeError
from .fs_utils import atomic_write, copy_file, copy_tree, ensure_directory, replace_symlink
from .image_ops import download_image, latest_image_url, populate_from_image
from .log_levels import normalize_log_level
from .runtime import (
    append_exports,
    bind_tftp_tree,
    image_arch_for_model,
    normalize_serial,
    publish_root_tftp_firmware,
    validate_model,
    warn_if_model_needs_manual_attention,
    write_client_state,
)
from .shell import capture


DEFAULT_GROUPS = [
    "sudo",
    "adm",
    "dialout",
    "cdrom",
    "audio",
    "video",
    "plugdev",
    "users",
    "input",
    "netdev",
    "gpio",
    "i2c",
    "spi",
    "render",
    "docker",
]

CMDLINE_DROP_PREFIXES = (
    "root=",
    "rootfstype=",
    "rootflags=",
    "nfsroot=",
    "ip=",
    "init=",
    "rdinit=",
    "systemd.run=",
    "systemd.run_success_action=",
    "systemd.unit=",
)

CMDLINE_DROP_TOKENS = {"rootwait", "rw", "ro", "resize"}
BOOT_CONFIG_MANAGED_START = "# HA-PXE managed config start"
BOOT_CONFIG_MANAGED_END = "# HA-PXE managed config end"
BOOT_CONFIG_RESET_SECTION = "[all]"
I2C_ARM_CONFIG_ENABLED_LINE = "dtparam=i2c_arm=on"
I2C_ARM_CONFIG_DISABLED_LINE = f"#{I2C_ARM_CONFIG_ENABLED_LINE}"
I2C_VC_CONFIG_ENABLED_LINE = "dtparam=i2c_vc=on"
I2C_VC_CONFIG_DISABLED_LINE = f"#{I2C_VC_CONFIG_ENABLED_LINE}"
I2C_MODULE_LINE = "i2c-dev"
NETWORKMANAGER_WAIT_ONLINE_SERVICE = "NetworkManager-wait-online.service"
NETWORKMANAGER_CONFLICTING_SERVICES = (
    "dhcpcd.service",
    "networking.service",
    "systemd-networkd.service",
)
NETWORKMANAGER_CONFIG = (
    "# Managed by HA-PXE\n"
    "[main]\n"
    "dns=none\n\n"
    "[ifupdown]\n"
    "managed=true\n"
)
RESOLV_CONF_PLACEHOLDER = "# Managed by HA-PXE; populated from /proc/net/pnp on first boot\n"


def provision_client(context: AddonContext, client: dict[str, object], server_ip: str) -> None:
    serial = normalize_serial(str(client.get("serial", "")))
    short_serial = serial[-8:]
    model = str(client.get("model", ""))
    hostname = str(client.get("hostname", ""))
    arch_override = str(client.get("image_arch", "auto") or "auto")
    rebuild = bool(client.get("rebuild", False))
    containers_raw = str(client.get("containers", "") or "")
    containers = normalize_container_specs(containers_raw, context.mqtt_env_defaults())
    container_count = len(containers)

    if not validate_model(model):
        raise HaPxeError(f"Unsupported model '{model}' for client {serial}")

    warn_if_model_needs_manual_attention(context, model)
    arch = image_arch_for_model(model, arch_override)
    _log_stage(context, "info", serial, "prepare", "started", f"Provisioning {hostname} for model {model} with image arch {arch} and {container_count} managed container(s)")

    boot_dir = context.paths.exports_dir / serial / "boot"
    root_dir = context.paths.exports_dir / serial / "root"
    state_file = context.paths.state_dir / f"{serial}.json"
    ensure_directory(boot_dir)
    ensure_directory(root_dir)

    if rebuild:
        _log_stage(context, "warn", serial, "prepare", "in_progress", "Rebuild requested; clearing exported boot/root trees and cached state")
        from .fs_utils import clear_directory

        clear_directory(boot_dir)
        clear_directory(root_dir)
        state_file.unlink(missing_ok=True)

    if state_file.exists():
        state = json.loads(state_file.read_text(encoding="utf-8"))
        existing_model = str(state.get("model", "") or "")
        existing_arch = str(state.get("arch", "") or "")
        if existing_model and existing_model != model:
            context.logger.warning(
                f"Client {serial} model changed from {existing_model} to {model}; set rebuild: true to refresh the exported rootfs"
            )
        if existing_arch and existing_arch != arch:
            raise HaPxeError(
                f"Client {serial} architecture changed from {existing_arch} to {arch}; set rebuild: true before restarting the add-on"
            )

    if not (root_dir / "etc" / "os-release").exists() or not (boot_dir / "cmdline.txt").exists():
        _log_stage(context, "info", serial, "image", "started", "Selecting, downloading, and unpacking a Raspberry Pi OS image")
        image_url = latest_image_url(context, arch)
        context.logger.info(f"Selected Raspberry Pi OS image {image_url}")
        image_path = download_image(context, image_url)
        context.logger.info(f"Populating exports for {serial} from {image_path}")
        populate_from_image(context, image_path, boot_dir, root_dir)
        write_client_state(state_file, model, arch, image_url)
        _log_stage(context, "info", serial, "image", "completed", f"Exported boot and root filesystems from {Path(image_url).name}")
    else:
        _log_stage(context, "info", serial, "image", "skipped", "Reusing existing exported boot/root trees")

    enable_i2c = _resolve_bool_option(context.config.get("enable_i2c", False), client.get("enable_i2c"))
    enable_i2c_vc = _resolve_bool_option(context.config.get("enable_i2c_vc", False), client.get("enable_i2c_vc"))
    _log_stage(
        context,
        "info",
        serial,
        "boot-config",
        "started",
        "Writing kernel command line, main config.txt entries, kernel module defaults, fstab mounts, and swap policy for network boot",
    )
    _rewrite_cmdline(context, boot_dir, server_ip, root_dir)
    _rewrite_boot_config(context, boot_dir, client, enable_i2c, enable_i2c_vc)
    _rewrite_modules_conf(context, root_dir, enable_i2c or enable_i2c_vc)
    _rewrite_fstab(root_dir, server_ip, boot_dir)
    _disable_swap_for_network_root(root_dir)
    _prepare_networkmanager_rootfs(root_dir)
    _log_stage(
        context,
        "info",
        serial,
        "boot-config",
        "completed",
        "PXE boot configuration updated, I2C defaults applied, swap disabled, and NetworkManager prepared as the sole network owner",
    )

    _log_stage(context, "info", serial, "bootstrap", "started", "Installing first-boot and container-sync bootstrap assets")
    _disable_stock_firstboot_services(root_dir)
    _write_bootstrap_files(context, root_dir, client, serial, hostname, server_ip, specs_to_json(containers))
    provision_boot_media(context, boot_dir, root_dir, model, arch, serial, server_ip)
    _log_stage(context, "info", serial, "bootstrap", "completed", "Bootstrap scripts, services, and transport settings installed")

    _log_stage(context, "info", serial, "nfs", "started", "Registering per-client NFS exports")
    append_exports(context, boot_dir, root_dir)
    _log_stage(context, "info", serial, "nfs", "completed", "Per-client NFS exports registered")

    _log_stage(context, "info", serial, "tftp", "started", "Publishing shared firmware and binding TFTP trees")
    publish_root_tftp_firmware(context, boot_dir, serial, model)
    bind_tftp_tree(context, boot_dir, serial, short_serial)
    _log_stage(context, "info", serial, "tftp", "completed", f"TFTP trees are bound for {serial} and {short_serial}")

    _log_stage(context, "info", serial, "prepare", "completed", f"Prepared client {hostname} ({serial}) using {arch}")


def _rewrite_cmdline(context: AddonContext, boot_dir: Path, server_ip: str, root_export: Path) -> None:
    existing = " ".join((boot_dir / "cmdline.txt").read_text(encoding="utf-8").split())
    cleaned_tokens: list[str] = []
    for token in existing.split():
        if token.startswith(CMDLINE_DROP_PREFIXES) or token in CMDLINE_DROP_TOKENS:
            continue
        if token not in cleaned_tokens:
            cleaned_tokens.append(token)

    context.logger.debug(f"Original cmdline for {boot_dir}: {existing}")
    prefix = f"{' '.join(cleaned_tokens)} " if cleaned_tokens else ""
    new_cmdline = (
        f"{prefix}root=/dev/nfs rootfstype=nfs "
        f"nfsroot={server_ip}:{root_export},vers=3,tcp,nolock rw ip=dhcp rootwait\n"
    )
    atomic_write(boot_dir / "cmdline.txt", new_cmdline)
    context.logger.debug(f"Rewritten cmdline for {boot_dir}: {new_cmdline.strip()}")


def _resolve_bool_option(global_value: object, client_value: object) -> bool:
    if isinstance(client_value, bool):
        return client_value
    if isinstance(global_value, bool):
        return global_value
    return False


def _rewrite_boot_config(
    context: AddonContext,
    boot_dir: Path,
    client: dict[str, object],
    enable_i2c: bool,
    enable_i2c_vc: bool,
) -> None:
    config_path = boot_dir / "config.txt"
    managed_lines = [
        line
        for line in _merge_boot_config_lines(
            str(context.config.get("boot_config_lines", "") or ""),
            str(client.get("boot_config_lines", "") or ""),
        )
        if not _is_managed_i2c_config_line(line)
    ]

    if not config_path.exists() and not managed_lines and not enable_i2c and not enable_i2c_vc:
        return

    existing = config_path.read_text(encoding="utf-8") if config_path.exists() else ""
    rendered = _render_boot_config(existing, managed_lines, enable_i2c, enable_i2c_vc)
    atomic_write(config_path, rendered)
    context.logger.debug(f"Rewritten config.txt for {boot_dir} with {len(managed_lines)} managed line(s)")


def _merge_boot_config_lines(*raw_values: str) -> list[str]:
    merged: list[str] = []
    for raw_value in raw_values:
        for line in raw_value.splitlines():
            normalized = line.strip()
            if not normalized or normalized in merged:
                continue
            merged.append(normalized)
    return merged


def _is_i2c_arm_config_line(line: str) -> bool:
    normalized = line.strip().removeprefix("#").strip()
    return normalized in {
        "dtparam=i2c",
        "dtparam=i2c=on",
        "dtparam=i2c=off",
        "dtparam=i2c_arm",
        "dtparam=i2c_arm=on",
        "dtparam=i2c_arm=off",
    }


def _is_i2c_vc_config_line(line: str) -> bool:
    normalized = line.strip().removeprefix("#").strip()
    return normalized in {
        "dtparam=i2c_vc",
        "dtparam=i2c_vc=on",
        "dtparam=i2c_vc=off",
    }


def _is_managed_i2c_config_line(line: str) -> bool:
    return _is_i2c_arm_config_line(line) or _is_i2c_vc_config_line(line)


def _render_boot_config(existing: str, managed_lines: list[str], enable_i2c: bool, enable_i2c_vc: bool) -> str:
    output_lines: list[str] = []
    in_managed_block = False
    just_ended_managed_block = False
    i2c_arm_line = I2C_ARM_CONFIG_ENABLED_LINE if enable_i2c else I2C_ARM_CONFIG_DISABLED_LINE
    i2c_vc_line = I2C_VC_CONFIG_ENABLED_LINE if enable_i2c_vc else I2C_VC_CONFIG_DISABLED_LINE
    i2c_arm_line_written = False
    i2c_vc_line_written = False

    for raw_line in existing.splitlines():
        stripped = raw_line.strip()
        if stripped == BOOT_CONFIG_MANAGED_START:
            in_managed_block = True
            continue
        if stripped == BOOT_CONFIG_MANAGED_END:
            in_managed_block = False
            just_ended_managed_block = True
            continue
        if not in_managed_block:
            if _is_i2c_arm_config_line(stripped):
                if not i2c_arm_line_written:
                    output_lines.append(i2c_arm_line)
                    i2c_arm_line_written = True
                just_ended_managed_block = False
                continue
            if _is_i2c_vc_config_line(stripped):
                if not i2c_vc_line_written:
                    output_lines.append(i2c_vc_line)
                    i2c_vc_line_written = True
                just_ended_managed_block = False
                continue
            if just_ended_managed_block and not raw_line.strip() and output_lines and not output_lines[-1].strip():
                just_ended_managed_block = False
                continue
            just_ended_managed_block = False
            output_lines.append(raw_line)

    while output_lines and not output_lines[-1].strip():
        output_lines.pop()

    if not i2c_arm_line_written and enable_i2c:
        if output_lines and output_lines[-1].strip():
            output_lines.append("")
        output_lines.append(i2c_arm_line)

    if not i2c_vc_line_written and enable_i2c_vc:
        if output_lines and output_lines[-1].strip():
            output_lines.append("")
        output_lines.append(i2c_vc_line)

    if managed_lines:
        if output_lines:
            output_lines.append("")
        output_lines.extend(
            (
                BOOT_CONFIG_MANAGED_START,
                BOOT_CONFIG_RESET_SECTION,
                *managed_lines,
                BOOT_CONFIG_RESET_SECTION,
                BOOT_CONFIG_MANAGED_END,
            )
        )

    if not output_lines:
        return ""
    return "\n".join(output_lines) + "\n"


def _rewrite_modules_conf(context: AddonContext, root_dir: Path, load_i2c_dev: bool) -> None:
    modules_path = root_dir / "etc" / "modules-load.d" / "modules.conf"
    if not modules_path.exists() and not load_i2c_dev:
        return

    output_lines: list[str] = []
    i2c_module_written = False
    existing_lines = modules_path.read_text(encoding="utf-8").splitlines() if modules_path.exists() else []

    for raw_line in existing_lines:
        if raw_line.strip() == I2C_MODULE_LINE:
            if load_i2c_dev and not i2c_module_written:
                output_lines.append(I2C_MODULE_LINE)
                i2c_module_written = True
            continue
        output_lines.append(raw_line)

    if load_i2c_dev and not i2c_module_written:
        output_lines.append(I2C_MODULE_LINE)

    output_text = "\n".join(output_lines)
    if output_lines:
        output_text += "\n"

    atomic_write(modules_path, output_text, 0o644 if not modules_path.exists() else None)
    context.logger.debug(f"Rewritten modules.conf for {root_dir} with i2c-dev {'enabled' if load_i2c_dev else 'disabled'}")


def _rewrite_fstab(root_dir: Path, server_ip: str, boot_export: Path) -> None:
    fstab_path = root_dir / "etc" / "fstab"
    output_lines: list[str] = []
    for raw_line in fstab_path.read_text(encoding="utf-8").splitlines():
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("#"):
            output_lines.append(raw_line)
            continue
        fields = raw_line.split()
        if len(fields) < 3:
            output_lines.append(raw_line)
            continue
        mount_point = fields[1]
        fs_type = fields[2]
        if mount_point in {"/", "/boot", "/boot/firmware"} or fs_type == "swap":
            continue
        output_lines.append(raw_line)
    output_lines.append(f"{server_ip}:{boot_export} /boot/firmware nfs defaults,vers=3,tcp,nolock,_netdev,addr={server_ip} 0 0")
    atomic_write(fstab_path, "\n".join(output_lines) + "\n")


def _disable_swap_for_network_root(root_dir: Path) -> None:
    ensure_directory(root_dir / "etc" / "rpi" / "swap.conf.d")
    ensure_directory(root_dir / "etc" / "systemd" / "system")
    atomic_write(root_dir / "etc" / "rpi" / "swap.conf.d" / "90-ha-pxe-no-swap.conf", "[Main]\nMechanism=none\n")
    replace_symlink(root_dir / "etc" / "systemd" / "system" / "dphys-swapfile.service", "/dev/null")
    (root_dir / "var" / "swap").unlink(missing_ok=True)


def _disable_stock_firstboot_services(root_dir: Path) -> None:
    ensure_directory(root_dir / "etc" / "systemd" / "system")
    ensure_directory(root_dir / "etc" / "systemd" / "system" / "multi-user.target.wants")
    replace_symlink(root_dir / "etc" / "systemd" / "system" / "userconfig.service", "/dev/null")
    replace_symlink(root_dir / "etc" / "systemd" / "system" / "systemd-firstboot.service", "/dev/null")
    replace_symlink(root_dir / "etc" / "systemd" / "system" / "systemd-networkd-wait-online.service", "/dev/null")
    (root_dir / "etc" / "systemd" / "system" / "multi-user.target.wants" / "userconfig.service").unlink(missing_ok=True)
    (root_dir / "etc" / "ssh" / "sshd_config.d" / "rename_user.conf").unlink(missing_ok=True)
    banner_path = root_dir / "usr" / "share" / "userconf-pi" / "sshd_banner"
    if banner_path.exists():
        atomic_write(banner_path, "")


def _prepare_networkmanager_rootfs(root_dir: Path) -> None:
    ensure_directory(root_dir / "etc" / "NetworkManager" / "conf.d")
    ensure_directory(root_dir / "etc" / "systemd" / "system")
    ensure_directory(root_dir / "etc" / "systemd" / "system" / "multi-user.target.wants")
    ensure_directory(root_dir / "etc" / "systemd" / "system" / "network-online.target.wants")

    atomic_write(root_dir / "etc" / "NetworkManager" / "conf.d" / "90-ha-pxe.conf", NETWORKMANAGER_CONFIG, 0o644)
    _prepare_resolv_conf_placeholder(root_dir)
    _enable_rootfs_service(root_dir, "NetworkManager.service")
    _enable_rootfs_service(root_dir, NETWORKMANAGER_WAIT_ONLINE_SERVICE, wanted_by="network-online.target.wants")

    for service in NETWORKMANAGER_CONFLICTING_SERVICES:
        replace_symlink(root_dir / "etc" / "systemd" / "system" / service, "/dev/null")
        (root_dir / "etc" / "systemd" / "system" / "multi-user.target.wants" / service).unlink(missing_ok=True)
        (root_dir / "etc" / "systemd" / "system" / "network-online.target.wants" / service).unlink(missing_ok=True)


def _prepare_resolv_conf_placeholder(root_dir: Path) -> None:
    resolv_path = root_dir / "etc" / "resolv.conf"
    firstboot_marker = root_dir / "var" / "lib" / "ha-pxe" / "firstboot.done"
    if firstboot_marker.exists():
        return
    atomic_write(resolv_path, RESOLV_CONF_PLACEHOLDER, 0o644)


def _enable_rootfs_service(root_dir: Path, service: str, *, wanted_by: str = "multi-user.target.wants") -> None:
    service_mask_path = root_dir / "etc" / "systemd" / "system" / service
    if service_mask_path.is_symlink() and service_mask_path.readlink() == Path("/dev/null"):
        service_mask_path.unlink()

    for service_path in (
        root_dir / "etc" / "systemd" / "system" / service,
        root_dir / "usr" / "lib" / "systemd" / "system" / service,
        root_dir / "lib" / "systemd" / "system" / service,
    ):
        if service_path.exists():
            replace_symlink(
                root_dir / "etc" / "systemd" / "system" / wanted_by / service,
                service_path.as_posix().removeprefix(root_dir.as_posix()),
            )
            return


def _write_bootstrap_files(
    context: AddonContext,
    root_dir: Path,
    client: dict[str, object],
    serial: str,
    hostname: str,
    server_ip: str,
    containers_json: str,
) -> None:
    username = str(context.config.get("default_username", "pi") or "pi")
    password = str(context.config.get("default_password", "") or "")
    keys = str(context.config.get("ssh_authorized_keys", "") or "")
    timezone = str(context.config.get("default_timezone", "") or "")
    keyboard_layout = str(context.config.get("default_keyboard_layout", "") or "")
    locale = str(context.config.get("default_locale", "") or "")
    client_log_level = normalize_log_level(str(client.get("log_level", "info") or "info"))
    password_hash = capture(["openssl", "passwd", "-6", password]) if password else ""

    ensure_directory(root_dir / "etc" / "ha-pxe")
    ensure_directory(root_dir / "usr" / "local" / "lib" / "ha-pxe")
    ensure_directory(root_dir / "usr" / "local" / "sbin")
    ensure_directory(root_dir / "etc" / "systemd" / "system" / "multi-user.target.wants")
    ensure_directory(root_dir / "etc" / "systemd" / "system" / "timers.target.wants")
    ensure_directory(root_dir / "var" / "lib" / "ha-pxe")

    templates = context.paths.templates_dir
    for name in ("ha-pxe-boot-update.service", "ha-pxe-boot-update.timer"):
        copy_file(templates / name, root_dir / "etc/systemd/system" / name, 0o644)
    replace_symlink(root_dir / "etc/systemd/system/timers.target.wants/ha-pxe-boot-update.timer",
                    "../ha-pxe-boot-update.timer")
    copy_file(templates / "ha-pxe-firstboot.service", root_dir / "etc" / "systemd" / "system" / "ha-pxe-firstboot.service", 0o644)
    copy_file(templates / "ha-pxe-early-log.service", root_dir / "etc" / "systemd" / "system" / "ha-pxe-early-log.service", 0o644)
    copy_file(
        templates / "ha-pxe-command-listener.service",
        root_dir / "etc" / "systemd" / "system" / "ha-pxe-command-listener.service",
        0o644,
    )
    copy_file(templates / "ha-pxe-container-sync.service", root_dir / "etc" / "systemd" / "system" / "ha-pxe-container-sync.service", 0o644)
    copy_file(templates / "ha-pxe-container-sync.timer", root_dir / "etc" / "systemd" / "system" / "ha-pxe-container-sync.timer", 0o644)
    copy_file(templates / "ha-pxe-early-log.py", root_dir / "usr" / "local" / "sbin" / "ha-pxe-early-log", 0o755)
    copy_file(templates / "ha-pxe-firstboot.py", root_dir / "usr" / "local" / "sbin" / "ha-pxe-firstboot", 0o755)
    copy_file(templates / "ha-pxe-command-listener.py", root_dir / "usr" / "local" / "sbin" / "ha-pxe-command-listener", 0o755)
    copy_file(templates / "ha-pxe-container-sync.py", root_dir / "usr" / "local" / "sbin" / "ha-pxe-container-sync", 0o755)
    copy_tree(context.paths.package_dir, root_dir / "usr" / "local" / "lib" / "ha-pxe" / "ha_pxe")

    replace_symlink(root_dir / "etc" / "systemd" / "system" / "multi-user.target.wants" / "ha-pxe-early-log.service", "../ha-pxe-early-log.service")
    replace_symlink(
        root_dir / "etc" / "systemd" / "system" / "multi-user.target.wants" / "ha-pxe-command-listener.service",
        "../ha-pxe-command-listener.service",
    )
    replace_symlink(root_dir / "etc" / "systemd" / "system" / "timers.target.wants" / "ha-pxe-container-sync.timer", "../ha-pxe-container-sync.timer")

    bootstrap_values = {
        "PXE_USERNAME": username,
        "PXE_PASSWORD_HASH": password_hash,
        "PXE_HOSTNAME": hostname,
        "PXE_SERIAL": serial,
        "PXE_EXTRA_GROUPS": ",".join(DEFAULT_GROUPS),
        "PXE_DEFAULT_TIMEZONE": timezone,
        "PXE_DEFAULT_KEYBOARD_LAYOUT": keyboard_layout,
        "PXE_DEFAULT_LOCALE": locale,
        "PXE_LOG_LEVEL": client_log_level,
        "PXE_LOG_HOST": server_ip,
        "PXE_LOG_PORT": str(context.paths.client_log_port),
        "PXE_LOG_PATH": context.paths.client_log_path,
        "PXE_COMMAND_HOST": server_ip,
        "PXE_COMMAND_PORT": str(context.paths.client_log_port),
        "PXE_COMMAND_PATH": context.paths.client_command_path,
    }
    atomic_write(root_dir / "etc" / "ha-pxe" / "bootstrap.env", format_env_file(bootstrap_values))

    authorized_keys_path = root_dir / "etc" / "ha-pxe" / "authorized_keys"
    atomic_write(authorized_keys_path, f"{keys}\n" if keys else "")
    atomic_write(root_dir / "etc" / "ha-pxe" / "containers.json", containers_json)
    atomic_write(root_dir / "etc" / "hostname", f"{hostname}\n")

    hosts_path = root_dir / "etc" / "hosts"
    if hosts_path.exists():
        lines = hosts_path.read_text(encoding="utf-8").splitlines()
        updated = False
        for index, line in enumerate(lines):
            if line.startswith("127.0.1.1") and line.split():
                lines[index] = f"127.0.1.1\t{hostname}"
                updated = True
                break
        if not updated:
            lines.append(f"127.0.1.1\t{hostname}")
        atomic_write(hosts_path, "\n".join(lines) + "\n")


def _log_stage(context: AddonContext, level: str, serial: str, stage: str, status: str, message: str) -> None:
    prefix = f"[client {serial}] stage={stage} status={status} {message}"
    if level == "error":
        context.logger.error(prefix)
    elif level == "warn":
        context.logger.warning(prefix)
    elif level == "debug":
        context.logger.debug(prefix)
    else:
        context.logger.info(prefix)

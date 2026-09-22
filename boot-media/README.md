# Standalone SD boot media with automatic HA updates

These tools prepare an SD card on macOS or Linux without a running Home Assistant
host, an installed add-on, or a previously deployed client. Docker builds the
ARM binaries and creates the disk image. Host-side Python commands use Python 3.14.

The SD holds Raspberry Pi firmware and U-Boot, which repeatedly tries DHCP/TFTP.
The add-on generates the matching Linux kernel and recovery initramfs from each
client's exported OS and modules automatically. The initramfs retries DHCP and
NFS mounting until the server is ready. Linux runs from NFS; its kernel and
modules are not copied onto the SD.

## Prepare and flash

Run these commands from the repository root. Replace `SERIAL` with the client's
hexadecimal serial (the same spelling used in the add-on) and `SERVER_IP` with
the reserved IPv4 address of the future HAOS host. Neither host has to be online.
The existing `pi3` add-on model covers both 3B and 3B+; select the exact board
variant here because their U-Boot configurations differ.

| Board | Build model | Add-on model | Add-on image_arch |
| --- | --- | --- | --- |
| Pi 2B, including revision 1.2 | `pi2` | `pi2` | `auto` / `armhf` |
| Pi 3B | `pi3` | `pi3` | `auto` / `arm64` |
| Pi 3B+ | `pi3plus` | `pi3` | `auto` / `arm64` |

```sh
./boot-media/build.sh pi2
./boot-media/prepare.sh --model pi2 --serial SERIAL --server SERVER_IP
```

Use `pi3` or `pi3plus` in both commands for those boards. `build.sh all` builds
all three variants. Builds download pinned, checksum-verified U-Boot 2026.07 and
BusyBox 1.37.0 source releases. The first build takes several minutes.

Preparation resolves the official `raspberrypi/firmware` stable branch once,
downloads all required firmware files from that exact commit, and records the
commit and checksums locally. To reuse an exact snapshot, pass
`--firmware-revision FULL_COMMIT_SHA` from a previous output's `metadata.json`.
No Raspberry Pi OS download is required on your laptop.

Outputs are under `boot-media/out/SERIAL-MODEL/`:

- `boot.img` and its SHA-256 sidecar: the complete 256 MiB disk image.
- `sd/`: the immutable recovery partition's files, for inspection.
- `metadata.json`: board, server, serial and firmware provenance.

Preparation refuses to replace an existing output directory. Move it aside when
preparing a replacement. Do not just copy `sd/` to a card: the image also contains
the partition layout needed for automatic updates.

List devices and preview the flash operation first:

```sh
python3.14 boot-media/flash.py --list
python3.14 boot-media/flash.py boot-media/out/SERIAL-pi2/boot.img --device /dev/diskN
```

On Linux the SD reader may be `/dev/sdX` or `/dev/mmcblkN`. Use the actual **whole
removable disk** from the listing. To write it:

```sh
sudo python3.14 boot-media/flash.py boot-media/out/SERIAL-pi2/boot.img --device /dev/diskN --write
```

Writing erases that card. The writer checks the image checksum, rejects system
and unsuitable devices, asks you to type its erase confirmation, unmounts the
card, writes the image and verifies it by reading it back. Without `--write` it
only previews the operation. No OTP programming is required for this SD route.

## What HA and the client do automatically

1. On a new deployment, or an explicitly requested rebuild, HA discovers the
   latest Raspberry Pi OS Lite image for the client's architecture. An existing
   deployed root filesystem is reused; restarting HA does not erase it or perform
   an unattended distribution upgrade. Turn `rebuild` back off after rebuilding.
2. During provisioning HA extracts the exported kernel's release, selects that
   exact release's network/NFS modules, and builds a recovery initramfs using a
   static BusyBox bundled into the add-on. A mismatch fails provisioning rather
   than publishing an unusable mixed kernel/module set.
3. HA publishes immutable kernel/initramfs generations, then atomically switches
   a small TFTP manifest. Old generations remain available to clients already
   downloading them. The SD loader checks their SHA-256 hashes before booting.
4. HA also publishes its current SD boot instructions in the client's boot
   export. The client checks about three minutes after boot and every six hours.
   If they differ, it writes and verifies an inactive SD instruction slot. The
   update takes effect on the next normal reboot; it does not reboot a workload.
   Unchanged instructions cause no writes.

There is no fetch-from-HA or publish-to-HA step on the laptop. The add-on's desired
instructions are authoritative, including intentional add-on rollbacks; timestamps
on the SD and HA are not compared. An older card can boot a newer deployment
because it downloads the deployed kernel/initramfs on every boot. Changes to
Linux alone do not require SD writes.

The HA add-on must be rebuilt/updated to include this implementation. Existing
clients receive the new timer at their next boot after provisioning. The new SD
layout must initially be flashed with these tools; older cards are not migrated
by writing their partition tables remotely.

## Recovery and limits

Partition 1 contains firmware, U-Boot, the selector and original fallback
instructions. Linux never mounts or writes it. Partitions 2 and 3 hold raw,
checksummed boot instructions. The updater only accepts the exact HA-PXE layout,
a matching board/serial, an NFS root, and an unmounted physical SD device.
It leaves the running slot intact, invalidates the inactive header, writes and
flushes the body, then commits the header last and verifies the result.

At boot U-Boot selects the valid slot with the highest local update sequence.
Invalid or incomplete slots are ignored. Updated instructions that cannot load
Linux return after six attempts so the selector can try the other slot and then
the original instructions, which retry indefinitely. The initramfs also retries
when DHCP or NFS is unavailable after Linux starts.

This protects the software layout against interrupted slot writes. It cannot
guarantee an SD controller will survive arbitrary power cuts or physical wear,
and it is not a health-based rollback system for a kernel that boots and then
crashes. A card with few writes should wear slowly, but read-only use is not a
promise of an unlimited lifespan.

Automatic updates deliberately cover **boot instructions**, not `bootcode.bin`,
`start.elf`, U-Boot, or the SD's `config.txt`/device trees. Those form the stable
recovery path. Updating them requires preparing and reflashing a replacement
card. Consequently add-on `boot_config_lines` and firmware-applied overlays do
not automatically change the SD's firmware configuration. This route currently
uses the SD's basic device tree; hardware requiring custom overlays needs a
prepared card with those settings. Native network boot retains its existing
config.txt behavior. Kernel and firmware/device-tree compatibility still needs
checking on the physical board when adopting major OS changes.

Use a reserved HA IP. Changing it requires the clients to remain reachable long
enough to receive new instructions, or preparing replacement media. DHCP must
provide addresses, and the network must allow TFTP and NFS to HA. This uses the
same trusted LAN assumption as the add-on's existing unauthenticated TFTP/NFS;
checksums detect corruption, not a malicious server.

Packaging and interrupted-write tests are automated. Physical cold boots,
power interruption during an update, and delayed HA startup still require a
Pi/SD hardware trial before relying on this unattended.

## Development checks

```sh
python3.14 -m unittest discover -s tests
python3.14 -m unittest discover -s boot-media/tests
# Run real FAT, U-Boot image, and initramfs tests in the Linux builder:
docker run --rm -v "$PWD:/work/repo" -w /work/repo/boot-media \
  ha-pxe-boot-builder:local python3.14 -m unittest discover -s tests
```

Sources: [Raspberry Pi boot documentation](https://www.raspberrypi.com/documentation/computers/raspberry-pi.html),
[official firmware](https://github.com/raspberrypi/firmware),
[U-Boot](https://docs.u-boot.org/en/latest/), and
[BusyBox](https://busybox.net/downloads/). Downloaded sources remain in the build
cache; firmware's Broadcom licence is included in the SD filesystem.

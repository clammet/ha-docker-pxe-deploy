# 1.1.0

- Generate matching kernel/initramfs payloads automatically for Pi 2 and Pi 3
  clients using the standalone SD retry loader.
- Publish boot-instruction updates for redundant SD slots; clients apply changed
  instructions without modifying the recovery firmware or forcing a reboot.
- Carry initramfs DHCP DNS settings into the client's first-boot setup.
- Add standalone SD preparation/flashing tools in the repository's `boot-media`
  directory. No connection to a deployed HA add-on is required.

SD retry boot and interrupted-update recovery still require physical board
validation. Firmware configuration and custom overlays remain part of the
initial SD preparation rather than the automatic instruction updates.

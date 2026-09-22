# Download a coherent kernel and initramfs; keep retrying while HA starts.
setenv autoload no
setenv netretry no
setenv bootp_retry_period 20000
setenv tftptimeout 3000
setenv tftptimeoutcountmax 5
setenv pxe_server @SERVER@
setenv pxe_prefix @SERIAL@/ha-pxe/@MODEL@
setenv pxe_manifest_addr 0x02500000
setenv fdt_addr_r 0x02600000
setenv kernel_addr_r 0x08000000
setenv ramdisk_addr_r 0x18000000
setenv pxe_attempt 0
while true; do
    setexpr pxe_attempt ${pxe_attempt} + 1
    if test "${pxe_slot}" != "0" && test ${pxe_attempt} -gt 6; then
        echo HA-PXE: updated instructions failed; returning to recovery
        exit
    fi
    if test "${serial#}" != "@SERIAL16@"; then
        echo HA-PXE: this card belongs to another client; refusing to share its NFS root
        sleep 10
        continue
    fi
    echo HA-PXE: waiting for Ethernet and DHCP
    usb start
    if dhcp; then
        setenv serverip ${pxe_server}
        setenv pxe_generation
        setenv pxe_kernel_sha
        setenv pxe_initrd_sha
        setenv pxe_bootargs
        if tftpboot ${pxe_manifest_addr} ${pxe_prefix}/boot.env; then
            if env import -t ${pxe_manifest_addr} ${filesize} pxe_generation pxe_kernel_sha pxe_initrd_sha pxe_bootargs; then
                if test -n "${pxe_generation}" && test -n "${pxe_bootargs}"; then
                    echo HA-PXE: loading generation ${pxe_generation}
                    if tftpboot ${kernel_addr_r} ${pxe_prefix}/${pxe_generation}/kernel.img; then
                        if hash sha256 ${kernel_addr_r} ${filesize} pxe_actual_sha && test "${pxe_actual_sha}" = "${pxe_kernel_sha}"; then
                            if tftpboot ${ramdisk_addr_r} ${pxe_prefix}/${pxe_generation}/initramfs.gz; then
                                setenv pxe_initrd_size ${filesize}
                                if hash sha256 ${ramdisk_addr_r} ${pxe_initrd_size} pxe_actual_sha && test "${pxe_actual_sha}" = "${pxe_initrd_sha}"; then
                                    # Preserve the firmware's board identity and applied GPIO/I2C overlays.
                                    if fdt move ${fdt_addr} ${fdt_addr_r} 0x100000; then
                                        setenv bootargs ${pxe_bootargs} ha_pxe.media=1 ha_pxe.model=@MODEL@ ha_pxe.slot=${pxe_slot} ha_pxe.loader=${pxe_script_sha}
                                        @BOOT_COMMAND@ ${kernel_addr_r} ${ramdisk_addr_r}:${pxe_initrd_size} ${fdt_addr_r}
                                    fi
                                fi
                            fi
                        fi
                    fi
                fi
            fi
        fi
    fi
    echo HA-PXE: boot attempt failed; retrying in 10 seconds
    usb stop
    sleep 10
done

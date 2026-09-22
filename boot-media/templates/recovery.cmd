# This file, firmware, U-Boot and fallback.scr are never changed by the client.
setenv pxe_a_seq -1
setenv pxe_b_seq -1
setenv pxe_probe yes
mmc dev 0
if mmc read 0x02000000 @SLOT_A@ 0x100; then
    if iminfo 0x02000000; then
        source 0x02000000
        setenv pxe_a_seq ${pxe_slot_seq}
    fi
fi
if mmc read 0x02100000 @SLOT_B@ 0x100; then
    if iminfo 0x02100000; then
        source 0x02100000
        setenv pxe_b_seq ${pxe_slot_seq}
    fi
fi
setenv pxe_probe no
if test ${pxe_a_seq} -gt ${pxe_b_seq}; then
    if test ${pxe_a_seq} -ge 0; then
        setenv pxe_slot 2
        source 0x02000000
    fi
    if test ${pxe_b_seq} -ge 0; then
        setenv pxe_slot 3
        source 0x02100000
    fi
else
    if test ${pxe_b_seq} -ge 0; then
        setenv pxe_slot 3
        source 0x02100000
    fi
    if test ${pxe_a_seq} -ge 0; then
        setenv pxe_slot 2
        source 0x02000000
    fi
fi
setenv pxe_slot 0
if fatload mmc 0:1 0x02200000 fallback.scr; then
    source 0x02200000
fi

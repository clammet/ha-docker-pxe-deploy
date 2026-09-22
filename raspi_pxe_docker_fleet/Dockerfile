ARG BUILD_FROM
FROM python:3.14-slim-trixie AS boot-tools
RUN apt-get update && apt-get install -y --no-install-recommends \
    bc bison build-essential ca-certificates curl flex \
    gcc-aarch64-linux-gnu gcc-arm-linux-gnueabihf \
    libc6-dev-arm64-cross libc6-dev-armhf-cross \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /work
COPY build/boot-tools.sh /build/boot-tools.sh
RUN BUSYBOX_ONLY=1 JOBS=4 bash /build/boot-tools.sh all

FROM $BUILD_FROM

SHELL ["/bin/bash", "-o", "pipefail", "-c"]

RUN apk add --no-cache \
    coreutils \
    curl \
    dnsmasq \
    dosfstools \
    e2fsprogs \
    findutils \
    iproute2 \
    jq \
    openssl \
    parted \
    python3 \
    rsync \
    shadow \
    util-linux \
    xz \
    zstd \
    nfs-utils

COPY rootfs /
COPY --from=boot-tools /work/out/bin/pi2/busybox /usr/local/lib/ha-pxe/boot-tools/pi2/busybox
COPY --from=boot-tools /work/out/bin/pi3/busybox /usr/local/lib/ha-pxe/boot-tools/pi3/busybox

RUN chmod a+x \
    /run.py \
    /usr/local/lib/ha-pxe/client-log-server.py \
    /usr/local/lib/ha-pxe/provision-client.py \
    /usr/local/lib/ha-pxe/templates/ha-pxe-early-log.py \
    /usr/local/lib/ha-pxe/templates/ha-pxe-firstboot.py \
    /usr/local/lib/ha-pxe/templates/ha-pxe-container-sync.py

CMD ["/run.py"]

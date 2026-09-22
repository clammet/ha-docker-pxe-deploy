#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "$0")"
mkdir -p out .cache
docker run --rm --user "$(id -u):$(id -g)" -e HOME=/tmp \
  -v "$PWD:/work/boot-media" \
  -v "$PWD/../raspi_pxe_docker_fleet:/work/raspi_pxe_docker_fleet:ro" \
  -w /work/boot-media ha-pxe-boot-builder:local python3.14 scripts/prepare.py "$@"

#!/usr/bin/env bash
# Build tools on Linux, including when invoked from macOS through Docker.
set -euo pipefail
cd -- "$(dirname -- "$0")"
case "${1:-all}" in
  all|pi2|pi3|pi3plus) model="${1:-all}" ;;
  *) echo "Usage: $0 [all|pi2|pi3|pi3plus]" >&2; exit 2 ;;
esac
mkdir -p .cache out
docker build -t ha-pxe-boot-builder:local .
docker run --rm --user "$(id -u):$(id -g)" \
  -e HOME=/tmp -e JOBS="${JOBS:-4}" \
  -v "$PWD:/work" -v "$PWD/../raspi_pxe_docker_fleet/build:/build:ro" \
  ha-pxe-boot-builder:local bash /build/boot-tools.sh "$model"

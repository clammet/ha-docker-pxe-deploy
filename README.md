# Home Assistant Raspberry Pi PXE Fleet

This repository contains the Home Assistant add-on `raspi_pxe_docker_fleet`.

The add-on prepares Raspberry Pi network-boot clients by:

- serving boot files over TFTP
- serving a per-client root filesystem over NFS
- disabling client swap before first boot so network-root clients do not write swap to NFS
- creating a user on first boot
- installing Docker on the client during first boot before reconciliation starts
- building or pulling configured Docker workloads on that client and reconciling them locally
- relaying client first-boot and container reconciliation logs back into the add-on log

## Install

1. Add this repository to Home Assistant as an add-on repository.
2. Install `Raspberry Pi PXE Docker Fleet`.
3. Disable Protection mode before starting the add-on.
4. Configure at least one Raspberry Pi client plus a login method.
5. Point your DHCP or ProxyDHCP service at the Home Assistant host for TFTP.

For SD-assisted boot with retries while HAOS starts, see
[`boot-media/README.md`](boot-media/README.md). It includes U-Boot builds for
Pi 2B, Pi 3B and Pi 3B+, standalone SD preparation and flashing on macOS/Linux,
automatic matching network payload generation by HA, and client updates to
redundant SD boot-instruction slots. No running HA host or manual payload
publication is needed to prepare a card.

## Devcontainer Harness


This repository now includes a Home Assistant add-on development harness built
around the official Home Assistant `apps` devcontainer.

Files:

- `.devcontainer/devcontainer.json` starts the Home Assistant devcontainer and
  mounts this repository as a local add-on repository
- `.devcontainer/start-home-assistant.sh` wraps the Home Assistant
  `supervisor_run` flow and preflights nested Docker registry access before
  launching Supervisor
- `.vscode/tasks.json` exposes the common add-on loop inside the container
- `scripts/ha-dev.sh` automates install, dev config, rebuild, restart, and log
  follow for the local add-on
- `.devcontainer/raspi_pxe_docker_fleet.options.json` provides a committed
  default dev-only options payload

Recommended loop:

1. Open the repository in the devcontainer.
2. Run the VS Code task `Start Home Assistant`.
3. Run the VS Code task `Prepare Local Add-on`.
4. Visit `http://localhost:7123` to inspect the Home Assistant instance.
5. After code changes, run `Rebuild Local Add-on` and `Follow Local Add-on Logs`.

The helper script targets the detected add-on as `local_raspi_pxe_docker_fleet`
and posts the dev options payload to the Supervisor API before rebuilding. If
you want a machine-local override without editing the tracked file, create
`.devcontainer/raspi_pxe_docker_fleet.options.local.json`; it is ignored by
git and takes precedence automatically.

If your dev options file uses structured JSON for `clients[].containers`, the
helper automatically converts that field into the string form Home Assistant
expects before posting the options payload.

## Agent Guidance

This repository supports a remote-client Docker deployment model. If an agent or
automation uses the term `Docker.remote`, treat it as repository shorthand for:

- the Docker workload is built, configured, and run on the Raspberry Pi client
- the Home Assistant add-on only provisions the client and writes the desired
  deployment spec into that client root filesystem
- a Git URL is not a `docker pull` target; it is a remote build source that the
  client must clone or otherwise fetch locally before building

`Docker.remote` is not a literal add-on schema key. Agents should map it to one
of the supported `source` modes inside the client `containers` definition:

- `source.type: image`
  Use for normal registry images such as `docker.io/library/nginx:1.27-alpine`.
- `source.type: git`
  Use for repositories like `https://github.com/Clam-/ha-pxe-janky-thermostat.git`
  that should be cloned on the client and built there with `docker build`.
- `source.type: dockerfile_url`
  Use for self-contained raw Dockerfile URLs that can be fetched directly on the
  client and built locally.

When an agent sees a project that is “composable via Git URL” or “buildable from
a Git URL”, the correct interpretation in this repository is:

- do not model it as an image pull
- do not assume Docker Compose will be run directly by this add-on
- model it as a Git-backed remote build on the client
- translate Compose-like settings into the add-on container spec fields such as
  `env`, `files`, `volumes`, `ports`, `devices`, `network_mode`, `depends_on`,
  and `command`

Preferred agent behavior when generating configs:

- Use the JSON array form of `containers` for anything beyond a plain registry
  image.
- Set an explicit unique `name` for each container.
- Prefer `source.type: git` for GitHub repos with a normal `Dockerfile`.
- Use `files` to generate config blobs like `/config/config.json` on the client
  and bind-mount them into the container.
- Use `env` for runtime environment variables.
- Use `image` only as the output tag for locally built sources, unless the
  source is a real registry image pull.
- Treat Compose files as reference material to translate, not as the primary
  runtime mechanism for this add-on.
- Child containers receive MQTT defaults automatically: `MQTT_PORT`,
  `MQTT_USERNAME`, and `MQTT_PASSWORD` from the Home Assistant MQTT service,
  plus `MQTT_BROKER` and `MQTT_HOST` from the Home Assistant host hostname
  when available. Explicit `env` values still win.

For the thermostat example, the correct agent output is a JSON `containers`
definition with:

- a separate `rgpiod` entry for `https://github.com/Clam-/docker-rgpio.git`
- `source.type: git`
- `depends_on: ["rgpiod"]` on the thermostat entry
- `url: https://github.com/Clam-/ha-pxe-janky-thermostat.git` for the thermostat
- `dockerfile: Dockerfile` for each Git-backed build
- generated `files` content for `/config/config.json`
- any required runtime `env`, `devices`, and generated config for MQTT and rgpio endpoints

## Notes

- DHCP or ProxyDHCP is not included in this add-on.
- Raspberry Pi network boot still depends on the board model and bootloader
  state.
- Container management supports both simple shorthand entries and richer JSON
  specs for remote builds, generated config files, and Docker run options.
- Provisioned clients automatically post first-boot and container-sync log
  entries back to the add-on over TCP `8099` at `/client-log`. If you filter
  traffic between the clients and the Home Assistant host, allow that path.
- Each client can optionally set its own `log_level` in `clients[]`. That
  threshold controls what the client emits locally and forwards upstream,
  while the add-on `log_level` separately filters which transported client log
  entries appear in the add-on log.

See [`raspi_pxe_docker_fleet/DOCS.md`](./raspi_pxe_docker_fleet/DOCS.md) for configuration examples and operational details.

# Raspberry Pi PXE Docker Fleet

This add-on prepares Raspberry Pi network-boot clients from Home Assistant.
Each configured client gets:

- a TFTP boot tree
- a dedicated NFS root filesystem
- swap disabled in the exported root before first boot
- a first-boot user setup
- Docker installed on first boot
- local Docker workload reconciliation on the client
- client-side deployment logs mirrored back into the add-on log

## Before you start

- Disable Home Assistant Protection mode before starting the add-on.
- Make sure your Raspberry Pi model and bootloader support network boot.
- Provide DHCP or ProxyDHCP separately. This add-on does not serve DHCP.
- Allow provisioned clients to reach the Home Assistant host on TCP `8099` so
  first-boot and container reconciliation logs can be relayed into the add-on
  log stream.

## Example configuration

```yaml
log_level: info
server_ip: 
default_username: pi
default_password: ""
default_timezone: Australia/Melbourne
default_keyboard_layout: us
default_locale: en_AU.UTF-8
enable_i2c: true
enable_i2c_vc: true
boot_config_lines: |
  dtoverlay=gpio-no-bank0-irq
ssh_authorized_keys: |
  ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIE6A4C2WQY0gVxk7bP5fA8Bf4m3jX9pW5rP8YqL3m7wN user@example
clients:
  - serial: "cdc843d7"
    model: pi3
    hostname: janky
    log_level: info
    image_arch: arm64
    rebuild: false
    containers: |
      [
        {
          "name": "rgpiod",
          "container_name": "rgpiod",
          "image": "local/rgpiod:latest",
          "source": {
            "type": "git",
            "url": "https://github.com/Clam-/docker-rgpio.git",
            "ref": "main",
            "context": ".",
            "dockerfile": "Dockerfile"
          },
          "env": {
            "RGPIOD_PORT": "8889",
            "RGPIOD_LOCAL_ONLY": "0"
          },
          "ports": [
            "8889:8889"
          ],
          "devices": [
            "/dev/gpiochip0:/dev/gpiochip0:rwm",
            "/dev/i2c-0:/dev/i2c-0:rwm",
            "/dev/i2c-1:/dev/i2c-1:rwm",
            "/dev/i2c-2:/dev/i2c-2:rwm"
          ]
        },
        {
          "name": "janky-thermostat",
          "container_name": "janky-thermostat",
          "image": "ghcr.io/clam-docker-images/janky-thermostat:latest",
          "depends_on": [
            "rgpiod"
          ],
          "files": [
            {
              "container_path": "/config/config.json",
              "format": "json",
              "content": {
                "mqtt_broker": "mosquitto",
                "mqtt_port": 1883,
                "mqtt_username": null,
                "mqtt_password": null,
                "schedule": ["06:00 21.0", "22:30 18.0"],
                "min_temp": 20.0,
                "max_temp": 28.0,
                "posmin": 1034,
                "posmax": 24600,
                "posmargin": 50,
                "speed": 500000,
                "lograte": 10,
                "updaterate": 15,
                "updir": 1,
                "i2c_bus": 0,
                "rgpio_addr": "rgpiod",
                "rgpio_port": 8889,
                "loglevel": "WARNING"
              }
            }
          ]
        }
      ]
  - serial: "0x4f2c1a7b"
    model: pi4
    hostname: kitchen-pi.home.example
    image_arch: auto
    rebuild: false
    containers: |
      docker.io/library/nginx:1.27-alpine
      https://github.com/Clam-/ha-pxe-janky-thermostat.git#main
```

## Field reference

- `log_level`: `error`, `warn`, `info`, or `debug`.
- `server_ip`: Optional override for the IP address clients should use for TFTP and NFS.
- `default_username`: User created on each Raspberry Pi at first boot.
- `default_password`: Optional password for that user.
- `default_timezone`: Optional IANA timezone name to apply on first boot.
- `default_keyboard_layout`: Optional XKB keyboard layout code to apply on first boot.
- `default_locale`: Optional locale name to apply on first boot.
- `enable_i2c`: Optional boolean. If `true`, provisioning uncomments `dtparam=i2c_arm=on` in the main exported `config.txt`. If `false`, it comments that firmware entry. The `i2c-dev` module is kept present whenever either `enable_i2c` or `enable_i2c_vc` is enabled.
- `enable_i2c_vc`: Optional boolean. If `true`, provisioning uncomments `dtparam=i2c_vc=on` in the main exported `config.txt`, which allows the VC bus to appear as `/dev/i2c-0`. If `false`, it comments that firmware entry. If both `enable_i2c` and `enable_i2c_vc` are `false`, provisioning removes `i2c-dev` from `etc/modules-load.d/modules.conf`.
- `boot_config_lines`: Optional multiline `config.txt` entries written into each client's exported main boot `config.txt`. These are applied in a managed block in the same boot partition the client later mounts at `/boot/firmware`.
- `ssh_authorized_keys`: Optional newline-separated OpenSSH public keys.
- `clients`: List of Raspberry Pi clients to provision.

## Deployment logging

Provisioned clients automatically send stage-based logs back to the add-on over
HTTP on TCP `8099` using the fixed path `/client-log`. You do not need to add
anything to the client config for this.

The add-on log now shows:

- add-on-side provisioning stages such as image selection, rootfs population,
  bootstrap injection, NFS export registration, and TFTP publication
- client first-boot stages such as hostname/user setup, locale defaults,
  package installation, access configuration, and service startup
- recurring client container-sync stages such as spec validation, image
  pulls/builds, generated file materialization, container creation/recreation,
  and stale resource cleanup

If a first-boot or container-sync stage fails on the client, the add-on log
receives a `stage=... status=failed` entry with the client hostname and serial.

Detailed `docker build` output stays on the client in
`var/lib/ha-pxe/containers/<container-key>/build.log`, so the systemd journal
and add-on log remain focused on stage-level progress.

Client fields:

- `serial`: Raspberry Pi serial number. Hex strings with or without a `0x` prefix are accepted.
- `model`: One of `pi0`, `pi1`, `pi2`, `pi3`, `pi4`, `pi5`, `400`, `500`, `cm3`, `cm4`, `cm5`, or `zero2w`.
- `hostname`: Hostname written into the client root filesystem.
- `log_level`: Optional client-side threshold for first-boot, command-listener, and container-sync logs on that Raspberry Pi. Defaults to `info`. Relayed logs still include their level, so the add-on `log_level` independently decides which transported client entries appear in the add-on log.
- `image_arch`: `auto`, `armhf`, or `arm64`.
- `rebuild`: If `true`, the client boot and root exports are recreated from a fresh Raspberry Pi OS Lite image on the next start.
- `enable_i2c`: Optional per-client override for the global `enable_i2c` setting.
- `enable_i2c_vc`: Optional per-client override for the global `enable_i2c_vc` setting.
- `boot_config_lines`: Optional additional multiline `config.txt` entries for just that client. Global `boot_config_lines` are applied first, then per-client lines are appended, and exact duplicate lines are removed.
- `containers`: Either newline-separated image refs and remote source URLs, or a JSON array of container spec objects.

## `containers` shorthand mode

When `containers` is plain text instead of JSON, each non-empty line becomes one deployment. These shorthands are supported:

- Registry image: `docker.io/library/nginx:1.27-alpine`
- Git build: `https://github.com/owner/repo.git#main`
- Git build with subdirectory context: `https://github.com/owner/repo.git#main:subdir`
- Raw Dockerfile URL: `https://raw.githubusercontent.com/owner/repo/main/Dockerfile.remote`

Shorthand deployments use Docker defaults: `restart: unless-stopped`, no extra mounts, no extra env vars, and no generated config files.

## JSON container spec

Use a JSON array when the container needs runtime configuration.

Top-level container fields:

- `name`: Recommended. Must be unique per client. If `container_name` is omitted, this is also used as the Docker container name and the bridge-network DNS name.
- `container_name`: Optional explicit Docker container name override.
- `source`: String shorthand or an object. If omitted, `image` is treated as a pulled registry image.
- `image`: Optional output tag for build-based sources. For pulled images, this is the registry image ref.
- `restart`: Docker restart policy. Defaults to `unless-stopped`.
- `network_mode`: Optional Docker network mode such as `host`. If omitted, the client attaches the container to a managed user-defined bridge network with normal outbound network access and inter-container DNS.
- `privileged`: Optional boolean.
- `workdir`: Optional working directory inside the container.
- `depends_on`: Optional array of managed container names, or an object using those names as keys. Dependencies only affect reconciliation order.
- `env`: Object of environment variables.
- `labels`: Object of Docker labels.
- `devices`: Array of Docker `--device` strings. Docker's default device permission set is read/write/mknod (`rwm`), and you can append an explicit mode such as `:rwm` when you want that access to be obvious in config examples.
- `extra_hosts`: Array of Docker `--add-host` strings.
- `ports`: Array of Docker port mappings. Each entry can be a string like `8080:80` or an object with `host`, `container`, and optional `protocol`.
- `volumes`: Array of Docker bind mount strings, or objects with `source`, `target`, and optional `read_only`.
- `command`: Optional string or array appended after the image name.
- `files`: Array of generated files to materialize on the client and bind-mount into the container.

Each child container also receives MQTT defaults when the MQTT service is configured for the add-on. The add-on injects `MQTT_PORT`, `MQTT_USERNAME`, and `MQTT_PASSWORD` from the Home Assistant MQTT service. It also injects `MQTT_BROKER` and `MQTT_HOST` using the Home Assistant host hostname from the Supervisor host API. Explicit `env` entries in a container spec override those defaults. If the host hostname is unavailable, `MQTT_BROKER` and `MQTT_HOST` are left unset.

`source` object fields:

- `type`: `image`, `git`, or `dockerfile_url`.
- `ref`: For `image`, the registry image reference. For `git`, the branch, tag, or commit to check out. Defaults to `main`.
- `url`: Required for `git` and `dockerfile_url`.
- `context`: For `git`, the relative build context inside the checked-out repo. Defaults to `.`.
- `dockerfile`: Relative Dockerfile path. Defaults to `Dockerfile`.
- `build_args`: Optional object of `docker build --build-arg` values.

`files` entry fields:

- `container_path`: Absolute path inside the container where the generated file will be mounted.
- `content`: The file body. Strings are written as text. Objects and arrays can be emitted as JSON.
- `format`: `text` or `json`. Defaults to `json` for object/array content and `text` for strings.
- `mode`: File mode string such as `0644`.
- `read_only`: Whether the generated mount should be `:ro`. Defaults to `true`.

## Thermostat plus `rgpiod` example

The `ha-pxe-janky-thermostat` repo can be deployed alongside `docker-rgpio`, with `depends_on` ensuring the GPIO daemon is reconciled first. By default, managed containers join the same user-defined bridge network, so the thermostat can reach `rgpiod` by name without any host alias:

```yaml
containers: |
  [
    {
      "name": "rgpiod",
      "container_name": "rgpiod",
      "image": "local/rgpiod:latest",
      "source": {
        "type": "git",
        "url": "https://github.com/Clam-/docker-rgpio.git",
        "ref": "main",
        "context": ".",
        "dockerfile": "Dockerfile"
      },
      "env": {
        "RGPIOD_PORT": "8889",
        "RGPIOD_LOCAL_ONLY": "0"
      },
      "ports": [
        "8889:8889"
      ],
      "devices": [
        "/dev/gpiochip0:/dev/gpiochip0:rwm",
        "/dev/i2c-0:/dev/i2c-0:rwm",
        "/dev/i2c-1:/dev/i2c-1:rwm",
        "/dev/i2c-2:/dev/i2c-2:rwm"
      ]
    },
    {
      "name": "janky-thermostat",
      "container_name": "janky-thermostat",
      "source": {
        "type": "git",
        "url": "https://github.com/Clam-/ha-pxe-janky-thermostat.git",
        "ref": "main",
        "context": ".",
        "dockerfile": "Dockerfile"
      },
      "depends_on": [
        "rgpiod"
      ],
      "files": [
        {
          "container_path": "/config/config.json",
          "format": "json",
          "content": {
            "mqtt_broker": "mosquitto",
            "mqtt_port": 1883,
            "schedule": ["06:00 21.0", "22:30 18.0"],
            "min_temp": 20.0,
            "max_temp": 28.0,
            "posmin": 1034,
            "posmax": 24600,
            "posmargin": 50,
            "speed": 500000,
            "lograte": 10,
            "updaterate": 15,
            "updir": 1,
            "i2c_bus": 0,
            "rgpio_addr": "rgpiod",
            "rgpio_port": 8889,
            "loglevel": "WARNING"
          }
        }
      ]
    }
  ]
```

That causes the Raspberry Pi client to:

1. Clone or update the `docker-rgpio` Git repo locally and build it on the Pi.
2. Start `rgpiod` with the requested GPIO and I2C device mappings and published port.
3. Clone or update the thermostat repo and build it on the Pi.
4. Write the thermostat JSON config file under its managed state directory.
5. Bind-mount that file to `/config/config.json`.
6. Attach both containers to the managed bridge network so `rgpiod` resolves directly by name.
7. Recreate either container when its repo, build args, or runtime spec changes.

## Prebuilt image workflow

For NFS-root clients or slower Raspberry Pi models, prefer prebuilt registry
images over `git` sources. The client then only pulls and runs images instead
of cloning repos and building locally.

If every client is `arm64`, publish `linux/arm64` images only. If you have a
mix of `arm64` and `armhf` clients, publish separate tags per architecture or a
multi-arch manifest.

```yaml
containers: |
  [
    {
      "name": "rgpiod",
      "container_name": "rgpiod",
      "image": "registry.home.arpa:5000/pxe/rgpiod:stable-arm64",
      "env": {
        "RGPIOD_PORT": "8889",
        "RGPIOD_LOCAL_ONLY": "0"
      },
      "ports": [
        "8889:8889"
      ],
      "devices": [
        "/dev/gpiochip0:/dev/gpiochip0:rwm",
        "/dev/i2c-0:/dev/i2c-0:rwm",
        "/dev/i2c-1:/dev/i2c-1:rwm",
        "/dev/i2c-2:/dev/i2c-2:rwm"
      ]
    },
    {
      "name": "janky-thermostat",
      "container_name": "janky-thermostat",
      "image": "registry.home.arpa:5000/pxe/janky-thermostat:stable-arm64",
      "depends_on": [
        "rgpiod"
      ],
      "files": [
        {
          "container_path": "/config/config.json",
          "format": "json",
          "content": {
            "mqtt_broker": "mosquitto",
            "mqtt_port": 1883,
            "schedule": ["06:00 21.0", "22:30 18.0"],
            "min_temp": 20.0,
            "max_temp": 28.0,
            "posmin": 1034,
            "posmax": 24600,
            "posmargin": 50,
            "speed": 500000,
            "lograte": 10,
            "updaterate": 15,
            "updir": 1,
            "i2c_bus": 0,
            "rgpio_addr": "rgpiod",
            "rgpio_port": 8889,
            "loglevel": "WARNING"
          }
        }
      ]
    }
  ]
```

With that configuration:

1. The add-on writes the desired image references into the exported client root.
2. The client pulls those image tags on each reconciliation run.
3. If a pulled tag resolves to a new image ID, the client recreates the
   managed container automatically.

Recommended update patterns:

- Automatic channel tag: publish a new image to the same tag such as
  `stable-arm64`. Clients pull it on the next reconciliation run and restart the
  container automatically.
- Controlled version tag: publish immutable tags such as `2026-03-15` or a git
  SHA, then change the `image` field in the add-on config and restart the
  add-on to roll that version out.
- Hybrid: publish both immutable tags and a moving channel tag. Use the channel
  tag for normal operation and switch to an immutable tag for rollback or
  debugging.

## Operational notes

- Client reconciliation starts after first-boot provisioning completes, then runs every 15 minutes.
- Image sources are pulled on every reconciliation run.
- Git and raw-Dockerfile sources are built locally on the client, not on Home Assistant.
- Raw Dockerfile URL mode is intended for self-contained Dockerfiles like `Dockerfile.remote` that do not depend on extra local build context.
- Existing newline-separated image lists continue to work.
- If two deployments would infer the same `name`, set explicit unique names in the JSON spec.
- `depends_on` is only available in JSON mode. It controls reconcile/start order only; it does not wait for health checks or auto-create missing containers.
- The client root filesystem is stored under `/data`, so client state survives add-on restarts.
- When `enable_i2c` is set, provisioning manages both the exported boot partition's main `config.txt` and `etc/modules-load.d/modules.conf` in the exported rootfs so the firmware setting and `i2c-dev` module stay in sync.
- When `enable_i2c_vc` is set, provisioning manages `dtparam=i2c_vc=on` in the same main `config.txt`; combined with `i2c-dev`, this allows the VC bus to appear as `/dev/i2c-0`.
- Managed boot config entries are written into the exported boot partition's main `config.txt`, not an included fragment, so custom settings remain available from both TFTP boot and the later `/boot/firmware` NFS mount.
- Raspberry Pi 2 v1.2, Pi 3, and CM3-class network boot first request `/bootcode.bin` from the TFTP root, then typically probe `/bootsig.bin`.
- Raspberry Pi 4, 400, CM4, Pi 5, 500, and CM5 use the EEPROM bootloader instead of `bootcode.bin`.

## SD boot with retries and automatic instruction updates

The repository's [standalone SD tools](../boot-media/README.md) support Pi 2B,
3B and 3B+. They prepare and flash media without access to HA. The add-on now
generates matching kernel/initramfs payloads for Pi 2 (`armhf`) and Pi 3
(`arm64`, including 3B+) during provisioning; no manual upload is required.

A timer on clients booted with this media checks for updated boot instructions
and writes only an inactive SD slot when they change. The initial firmware,
U-Boot and recovery files remain untouched. Updates take effect on the next
normal reboot. The SD's firmware configuration and overlays are not updated by
this mechanism; see the tool's limits before using custom hardware overlays.

New deployments and explicit rebuilds resolve the latest OS image. Existing
root filesystems are retained on restart; this does not automatically upgrade
or replace an installed OS. Ordinary network-boot clients' SD devices are never
modified. View client update results with `journalctl -u ha-pxe-boot-update`.

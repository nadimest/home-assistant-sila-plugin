# SiLA 2 integration for Home Assistant

[![Tests](https://github.com/nadimest/home-assistant-sila-plugin/actions/workflows/test.yml/badge.svg)](https://github.com/nadimest/home-assistant-sila-plugin/actions/workflows/test.yml)
[![Validate](https://github.com/nadimest/home-assistant-sila-plugin/actions/workflows/validate.yml/badge.svg)](https://github.com/nadimest/home-assistant-sila-plugin/actions/workflows/validate.yml)
[![GitHub release](https://img.shields.io/github/v/release/nadimest/home-assistant-sila-plugin)](https://github.com/nadimest/home-assistant-sila-plugin/releases)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

Connect [SiLA 2](https://sila-standard.com) laboratory instruments to
[Home Assistant](https://www.home-assistant.io). SiLA servers on your network
are discovered automatically and appear as Home Assistant devices — their
properties become sensors, their commands become controls and services, and
all of Home Assistant's machinery (history, dashboards, automations,
notifications, voice) works on top of your lab equipment.

Home Assistant can also act as a **SiLA 2 v1.1 cloud gateway**: instruments
configured for *server-initiated connections* dial into Home Assistant from
behind NAT or strict lab firewalls and appear as devices exactly like locally
discovered ones.

## What is this, exactly?

A **custom integration** (also called a custom component) — a Python package
that runs inside Home Assistant core, like the built-in integrations for Hue
or MQTT. It is **not an add-on**: add-ons are Supervisor-managed Docker
containers and only exist on Home Assistant OS / Supervised installs. A
custom integration works on every install type (HA OS, Container, Core,
Supervised) and consists of nothing more than the `custom_components/sila/`
folder in your config directory. Its Python dependencies (`sila2`) are
installed automatically by Home Assistant on first load.

## Features

- **Zero-config discovery** — SiLA servers announce themselves via mDNS
  (`_sila._tcp.local.`); they pop up in *Settings → Devices & Services* with
  one-click setup. Servers without mDNS can be added manually by host + port.
- **One device per SiLA server**, carrying the server's name, type, version,
  and vendor metadata from the SiLAService core feature.
- **Entities generated dynamically from feature definitions** — no
  per-instrument code. Whatever features a server reports, you get matching
  entities (see the mapping table below).
- **Live values**: observable properties stream over gRPC subscriptions and
  push straight into Home Assistant; unobservable properties are polled.
- **Long-running (observable) commands** with live progress, estimated
  remaining time, and completion events for automations.
- **Camera-style commands become images** — a command returning a single
  Binary payload (e.g. `GrabSnapshot → ImagePayload`) gets an image entity
  showing the latest capture on your dashboards.
- **Cloud gateway** for SiLA 2 v1.1 server-initiated connections, with
  automatic device registration, unavailable-on-disconnect, and in-place
  recovery when the instrument redials.
- **TLS that matches lab reality**: pin a server's self-signed certificate on
  first use (default), use the system CA store, or connect unencrypted to
  development servers.

## How SiLA concepts map to Home Assistant

| SiLA 2 concept | Home Assistant representation |
|---|---|
| SiLA Server | Device (one config entry per server) |
| Server name / type / version / vendor | Device registry metadata |
| Observable property | Sensor, push-updated via gRPC subscription stream |
| Unobservable property | Sensor, polled every 30 s |
| SiLAService core properties | Diagnostic sensors |
| Command, no parameters | Button |
| Command, one numeric parameter | Number (set it → command fires) |
| Command, other signatures | `sila.call_command` service |
| Command returning a single Binary (e.g. a snapshot) | Image entity showing the latest payload |
| Observable command execution | Status sensor (`idle`/`waiting`/`running`/`finishedSuccessfully`/`finishedWithError`) + events |
| Server online/offline | Entity availability |

Three details worth knowing:

- **Number entities mirror matching properties.** If a feature has a
  command `SetTargetTemperature(TargetTemperature: Real)` *and* a property
  named `TargetTemperature`, the number entity displays the live property
  value and setting it calls the command — a read/write control out of one
  command/property pair.
- **Complex values are flattened conservatively.** SiLA structures become a
  `structure` state with fields in attributes; lists show their length with
  items in attributes; long strings are truncated with the full value in
  attributes. Scalars pass through unchanged.
- **One subscription regardless of audience.** Home Assistant holds a single
  gRPC subscription per observable property; dashboards, automations, and
  the recorder all fan out from it. Instruments see exactly one client no
  matter how many people are watching.

## Installation

### HACS (recommended, once this repo is on GitHub)

1. HACS → ⋮ → *Custom repositories* → add this repository, category
   *Integration*.
2. Install **SiLA 2**, restart Home Assistant.

### Manual

1. Copy the `custom_components/sila/` folder into the `config` directory of
   your Home Assistant instance, so you end up with
   `config/custom_components/sila/manifest.json`. For example:

   ```bash
   # from this repository, to a remote HA instance
   scp -r custom_components/sila <user>@<ha-host>:/path/to/config/custom_components/
   ```

   On HA OS, use the Samba or SSH add-on to reach the `config` share; the
   target is `/config/custom_components/sila/`.

2. Restart Home Assistant (*Settings → System → Restart*). On first load HA
   installs the Python dependencies declared in the manifest — this can take
   a minute; watch *Settings → System → Logs* if curious.

### Adding servers

- **Discovered**: SiLA servers on the same network appear automatically in
  *Settings → Devices & Services* — click *Configure*, choose a connection
  security mode, done.
- **Manual**: *Add Integration → SiLA 2 → Connect to a SiLA server*, enter
  host and port.
- **Cloud gateway**: *Add Integration → SiLA 2 → Host a cloud endpoint*,
  choose a port (default 50051). Point your instruments' server-initiated
  connection at `<ha-host>:<port>`. Each server that dials in registers as a
  device automatically.

### Connection security

| Mode | Use when |
|---|---|
| **Pin server certificate** (default) | The instrument uses a self-signed certificate (most do). The certificate presented at setup time is fetched and trusted from then on — trust-on-first-use. |
| **System CA store** | The server's certificate chains to a real CA. |
| **No encryption** | Development/simulator servers started insecurely. |

The cloud endpoint currently listens in plaintext — run it on a trusted
network or behind a TLS-terminating proxy (see roadmap).

## Using commands from automations

The `sila.call_command` service invokes any command, with parameters:

```yaml
action: sila.call_command
data:
  device_id: abc123...            # pick the device in the UI
  feature: TemperatureController
  command: SetTargetTemperature
  parameters:
    TargetTemperature: 37.0
```

Responses are available via response variables. For observable
(long-running) commands the optional `wait` field controls the semantics:
`wait: true` (default) blocks until completion and returns the final
responses; `wait: false` returns the execution UUID immediately and lets
events signal completion.

Two events fire for observable commands — use them in automation triggers:

- `sila_command_started`
- `sila_command_finished`

Both carry `device_id`, `server_uuid`, `server_name`, `feature`, `command`,
`execution_uuid`, `status`, and (on finish) `responses` or `error`. The
classic lab automation:

```yaml
triggers:
  - trigger: event
    event_type: sila_command_finished
    event_data:
      feature: Centrifugation
actions:
  - action: notify.mobile_app_phone
    data:
      message: "Centrifuge run finished: {{ trigger.event.data.status }}"
```

## Snapshots and other binary results

Any command whose responses consist of exactly one `Binary` value gets an
**image entity** — the SiLA way of modelling instrument cameras, plate
readers exporting result images, and similar. The raw bytes arrive directly
in the gRPC response (no base64 round-trip), are content-sniffed
(JPEG/PNG/GIF/WebP), and served through Home Assistant's authenticated
image API, so picture cards and notifications work out of the box.

The image refreshes from **every** invocation path: the command's button or
number entity, `sila.call_command`, and observable commands when they
finish. The entity also remembers the last-used parameters, so refreshing it
re-runs the command:

```yaml
# An automation that takes a fresh snapshot every hour
triggers:
  - trigger: time_pattern
    minutes: 0
actions:
  - action: homeassistant.update_entity
    target:
      entity_id: image.my_microscope_camera_image_payload
```

For a command with parameters, run it once (entity or service) so the
integration learns the parameter values to replay; parameterless commands
can be refreshed immediately. Payloads must be embedded SiLA binaries
(≤ 2 MB per the SiLA spec) — see limitations.

## The cloud gateway (server-initiated connections)

SiLA 2 v1.1 defines *cloud connectivity*: instead of listening for clients,
a SiLA server dials out to a `CloudClientEndpoint` and all SiLA traffic is
multiplexed over that one bidirectional gRPC stream. This integration lets
Home Assistant *be* that endpoint — useful when instruments sit behind NAT,
on isolated lab VLANs with outbound-only rules, or are configured for a
cloud platform you want to point at your own infrastructure instead.

Implementation notes:

- The official `SiLACloudConnector.proto` from the SiLA base repository is
  vendored and compiled at runtime with sila2's own protoc machinery.
- A connected server is wrapped in a client facade that mimics the regular
  sila2 client, so coordinators, entities, commands, and events behave
  identically for direct and cloud-connected servers.
- Disconnect marks the device unavailable immediately; a redial recovers it
  in place — same device, same entities, no duplicates.

`demo_server/cloud_bridge.py` is a standalone byte-level proxy that dials
any locally listening SiLA server into a cloud endpoint
(`./scripts/dev.sh bridge`). Because it forwards payloads as opaque bytes it
works for any feature set without code generation — handy both for testing
and for retrofitting server-initiated connectivity onto instruments whose
SiLA stack predates v1.1.

## Architecture notes

- Built on the reference [`sila2`](https://gitlab.com/sila2/sila_python)
  Python client. Its API is blocking, so all network I/O runs in Home
  Assistant's executor; observable-property subscription callbacks arrive on
  gRPC threads and are marshalled onto the event loop.
- A `DataUpdateCoordinator` per server polls unobservable properties every
  30 s and doubles as the availability signal. Dead subscription streams are
  renewed automatically when a server comes back.
- Feature definitions (FDL XML) are fetched from the server at setup and
  drive entity generation via sila2's framework classes, which also handle
  all payload (de)serialization for the cloud gateway.
- `sila2` registers protobuf descriptors at import time, which interacts
  badly with Home Assistant's concurrent module imports — therefore no
  module of this integration imports sila2 at module level; the first import
  happens exactly once, serialized, in the executor
  (`sila_import.ensure_sila2()`). Keep it that way when contributing.
- `grpcio-tools` is pinned `<1.76` because Home Assistant pins the protobuf
  runtime, and newer protoc gencode refuses to load on older runtimes.

### A note on chatty instruments

Every pushed observable-property value becomes a state write in Home
Assistant's recorder database. An instrument streaming at 2 s intervals
writes ~43k rows/day per sensor. If that's more history than you want, use
HA's [recorder filters](https://www.home-assistant.io/integrations/recorder/)
to exclude or throttle specific entities (a built-in per-server throttle is
on the roadmap).

## Known limitations

- SiLA binary transfer (payloads over 2 MB) is not supported; embedded
  binaries — which cover snapshot-style commands — work.
- SiLA client metadata (e.g. lock controller) is not yet sent with calls.
- Observable command *intermediate responses* are not surfaced (execution
  state and progress are).
- The cloud endpoint listens in plaintext.
- Multi-parameter and non-numeric-parameter commands are service-only — by
  design, since HA entities are single-value controls.

## Development

```bash
python3 -m venv .venv
.venv/bin/pip install homeassistant "sila2[codegen]" "grpcio-tools<1.76" \
    pytest-homeassistant-custom-component

# test suite — runs against a real in-process SiLA server, covering
# discovery, config flow, entities, commands, and the cloud gateway
# including disconnect/reconnect
.venv/bin/python -m pytest tests/

# manual testing
./scripts/dev.sh server   # demo SiLA server (simulated thermostat, mDNS)
./scripts/dev.sh          # local HA on http://localhost:8123
./scripts/dev.sh bridge   # dial the demo server into a cloud endpoint
```

The demo server (`demo_server/`) is generated from the feature definitions
in `demo_server/*.sila.xml` with `sila2.code_generator` and simulates a
thermostat — an observable temperature drifting toward a settable target,
plus an observable `Equilibrate` command that reports progress — and a
camera whose `GrabSnapshot` command returns cycling JPEG frames.

## Branding

The integration ships the official SiLA wordmark (light + dark variants) in
`custom_components/sila/brand/`. Home Assistant **2026.3 and newer** serves
these local brand images directly on the integration card; older versions
show a placeholder (the central brands repo no longer accepts custom
integration submissions).

## Roadmap

- [x] Discovery, dynamic entities, buttons/numbers, `call_command` service
- [x] Observable commands with progress + events
- [x] SiLA 2 v1.1 cloud gateway (server-initiated connections)
- [x] Image entities for commands returning binary payloads (snapshots)
- [ ] TLS for the cloud endpoint
- [ ] SiLA client metadata (lock controller) support
- [ ] Binary transfer
- [ ] Unit metadata → `native_unit_of_measurement`, long-term statistics
- [ ] Per-server update throttling for chatty observable properties
- [ ] HACS default-store listing

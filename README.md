# SiLA 2 integration for Home Assistant

Connect [SiLA 2](https://sila-standard.com) lab instruments to Home Assistant.
SiLA servers on your network are discovered automatically via mDNS and show up
as Home Assistant devices — their properties become sensors, their commands
become buttons and services, and all of Home Assistant's machinery (history,
dashboards, automations, alerting) works on top.

## What you get

- **Auto-discovery**: SiLA servers announcing `_sila._tcp.local.` pop up in
  *Settings → Devices & Services* — one click to add.
- **One device per SiLA server**, with vendor, type, and version metadata from
  the SiLAService core feature.
- **Sensors for every property** of every feature:
  - *Observable* properties are push-updated live via gRPC subscription streams.
  - *Unobservable* properties are polled every 30 seconds.
  - SiLAService core properties appear as diagnostic entities.
- **Buttons** for parameterless commands.
- **Observable (long-running) commands**: a status sensor per command shows
  idle/waiting/running/finished with live progress and estimated remaining
  time as attributes; `sila_command_started` / `sila_command_finished` events
  fire so automations can react to runs completing ("notify me when the
  centrifuge finishes").
- **`sila.call_command` service** for commands with parameters, usable from
  automations and scripts, with command responses available via
  *response variables*:

  ```yaml
  action: sila.call_command
  data:
    device_id: abc123...
    feature: TemperatureController
    command: SetTargetTemperature
    parameters:
      TargetTemperature: 37.0
  ```

  For observable commands, `wait: false` returns immediately with the
  execution UUID and lets the finished event signal completion; the default
  `wait: true` blocks and returns the final responses.

- **TLS that matches lab reality**: pin a server's self-signed certificate on
  first use (default), use the system CA store, or connect unencrypted to
  development servers.

## Installation

### HACS (recommended)

1. HACS → ⋮ → *Custom repositories* → add this repository (type: Integration).
2. Install **SiLA 2** and restart Home Assistant.
3. Discovered SiLA servers appear in *Settings → Devices & Services*; servers
   that don't announce via mDNS can be added manually with
   *Add integration → SiLA 2* (host + port).

### Manual

Copy `custom_components/sila/` into your Home Assistant `config/custom_components/`
directory and restart.

## Development

```bash
python3 -m venv .venv
.venv/bin/pip install homeassistant "sila2[codegen]" pytest-homeassistant-custom-component

# run the test suite (spins up a real in-process SiLA server)
.venv/bin/python -m pytest tests/

# manual testing: demo SiLA server (terminal 1) + local HA (terminal 2)
./scripts/dev.sh server
./scripts/dev.sh
```

The `demo_server/` package is a simulated "Demo Thermostat" SiLA server
(generated with `sila2.code_generator` from
`demo_server/TemperatureController.sila.xml`) with an observable temperature
that drifts toward a settable target — handy for watching live push updates
on a dashboard graph.

## Architecture notes

- Built on the reference [`sila2`](https://gitlab.com/SiLA2/sila_python) Python
  client. Its API is blocking, so all network I/O runs in Home Assistant's
  executor; observable-property subscription callbacks arrive on gRPC threads
  and are marshalled back onto the event loop.
- A `DataUpdateCoordinator` per server polls unobservable properties and acts
  as the availability signal; when a server comes back after an outage, dead
  subscription streams are renewed automatically.
- Entities are generated dynamically from the feature definitions the server
  reports — no per-instrument code needed.

## Roadmap

- [x] Discovery, sensors, buttons, `call_command` service
- [x] Observable (long-running) commands with progress reporting and
      completion events
- [ ] SiLA Client Metadata (e.g. lock controller) support
- [ ] **Cloud gateway**: host a SiLA 2 v1.1 *server-initiated connection*
      endpoint inside Home Assistant, so instruments outside the local
      network can connect in
- [ ] Unit metadata → `native_unit_of_measurement`, long-term statistics

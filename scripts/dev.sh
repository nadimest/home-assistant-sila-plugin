#!/usr/bin/env bash
# Run a local Home Assistant with this integration loaded, for manual testing.
#
#   ./scripts/dev.sh            start HA on http://localhost:8123
#   ./scripts/dev.sh server     start the demo SiLA server (announces via mDNS)
#
# Start both (in two terminals), open HA, finish onboarding, and the
# "Demo Thermostat" should appear as a discovered device.
set -euo pipefail
cd "$(dirname "$0")/.."

if [[ "${1:-}" == "server" ]]; then
    exec .venv/bin/python -m demo_server.sila_demo_server --insecure --port 50052 --verbose
fi

mkdir -p dev-config
ln -sfn "$(pwd)/custom_components" dev-config/custom_components
exec .venv/bin/hass --config dev-config

#!/usr/bin/env bash
# Run a local Home Assistant with this integration loaded, for manual testing.
#
#   ./scripts/dev.sh            start HA on http://localhost:8123
#   ./scripts/dev.sh server     start the demo SiLA server (announces via mDNS)
#   ./scripts/dev.sh bridge     dial the demo server into the cloud endpoint
#
# Start HA + server (two terminals), open HA, finish onboarding, and the
# "Demo Thermostat" appears as a discovered device. To try the cloud
# gateway instead: add the "SiLA 2" integration with "Host a cloud
# endpoint" (port 50051), then run the bridge in a third terminal.
set -euo pipefail
cd "$(dirname "$0")/.."

if [[ "${1:-}" == "server" ]]; then
    exec .venv/bin/python -m demo_server.sila_demo_server --insecure --port 50052 --verbose
fi
if [[ "${1:-}" == "bridge" ]]; then
    exec .venv/bin/python -m demo_server.cloud_bridge --server 127.0.0.1:50052 --endpoint 127.0.0.1:50051
fi

mkdir -p dev-config
ln -sfn "$(pwd)/custom_components" dev-config/custom_components
exec .venv/bin/hass --config dev-config

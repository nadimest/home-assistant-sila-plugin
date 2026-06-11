"""Constants for the SiLA 2 integration."""

from __future__ import annotations

DOMAIN = "sila"

CONF_TLS_MODE = "tls_mode"
CONF_PINNED_CERT = "pinned_cert"

TLS_MODE_SYSTEM = "system"
TLS_MODE_PIN = "pin"
TLS_MODE_INSECURE = "insecure"
TLS_MODES = [TLS_MODE_PIN, TLS_MODE_SYSTEM, TLS_MODE_INSECURE]

DEFAULT_PORT = 50052
POLL_INTERVAL_SECONDS = 30

# The SiLAService core feature is mapped to device metadata and
# diagnostic entities rather than regular entities.
SILA_SERVICE_FEATURE = "SiLAService"

SERVICE_CALL_COMMAND = "call_command"
ATTR_FEATURE = "feature"
ATTR_COMMAND = "command"
ATTR_PARAMETERS = "parameters"

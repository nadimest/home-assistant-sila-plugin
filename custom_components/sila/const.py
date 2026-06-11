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

# Config entry modes: outbound connection to a server, or hosting the
# SiLA 2 v1.1 cloud endpoint that servers connect into.
CONF_MODE = "mode"
MODE_CONNECT = "connect"
MODE_CLOUD = "cloud"
DEFAULT_CLOUD_PORT = 50051

# The SiLAService core feature is mapped to device metadata and
# diagnostic entities rather than regular entities.
SILA_SERVICE_FEATURE = "SiLAService"

SERVICE_CALL_COMMAND = "call_command"
ATTR_FEATURE = "feature"
ATTR_COMMAND = "command"
ATTR_PARAMETERS = "parameters"
ATTR_WAIT = "wait"

EVENT_COMMAND_STARTED = "sila_command_started"
EVENT_COMMAND_FINISHED = "sila_command_finished"

# Seconds between status/progress refreshes while an observable command runs.
COMMAND_WATCH_INTERVAL = 1.0


def command_update_signal(entry_id: str) -> str:
    """Dispatcher signal for observable command execution updates."""
    return f"{DOMAIN}_{entry_id}_command_update"


def command_responses_signal(entry_id: str) -> str:
    """Dispatcher signal carrying raw command responses (e.g. image bytes)."""
    return f"{DOMAIN}_{entry_id}_command_responses"


def cloud_new_server_signal(entry_id: str) -> str:
    """Dispatcher signal for a SiLA server newly connected to the cloud endpoint."""
    return f"{DOMAIN}_{entry_id}_cloud_new_server"

"""Test fixtures: a real demo SiLA server running in-process."""

from __future__ import annotations

import socket
from unittest.mock import patch
from uuid import uuid4

import pytest

from demo_server.sila_demo_server.server import Server


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Allow loading custom_components/sila in tests."""
    return


@pytest.fixture
def demo_server():
    """Run the demo SiLA server on a free port, without mDNS."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]

    # sila2 instantiates a real Zeroconf even with discovery disabled; that
    # leaves a lingering event-loop thread and fails outright on CI runners
    # without a multicast-capable interface. The tests never use mDNS.
    with patch("sila2.discovery.broadcaster.Zeroconf"):
        server = Server(server_uuid=uuid4())
        server.start_insecure("127.0.0.1", port, enable_discovery=False)
        yield server, port
        server.stop()
        # sila2 never shuts down the executor it hands to grpc.server(),
        # which leaves worker threads that trip HA's lingering-thread check.
        server.grpc_server._state.thread_pool.shutdown(wait=True)

"""Blocking connection helpers for the sila2 client.

Everything in this module performs blocking network I/O and must be
called through ``hass.async_add_executor_job``. sila2 itself is imported
lazily via sila_import.ensure_sila2() — see that module for why.
"""

from __future__ import annotations

import ssl
from dataclasses import dataclass
from typing import TYPE_CHECKING

from .const import TLS_MODE_INSECURE, TLS_MODE_PIN, TLS_MODE_SYSTEM
from .sila_import import ensure_sila2

if TYPE_CHECKING:
    from sila2.client import SilaClient


@dataclass(slots=True)
class SilaServerInfo:
    """Identity of a SiLA server, read from its SiLAService feature."""

    server_uuid: str
    server_name: str
    server_type: str
    server_version: str
    vendor_url: str
    server_description: str


def fetch_server_certificate(host: str, port: int) -> str:
    """Fetch the PEM certificate a server presents, for pinning (TOFU)."""
    return ssl.get_server_certificate((host, port))


def create_client(
    host: str,
    port: int,
    tls_mode: str,
    pinned_cert: str | None = None,
) -> SilaClient:
    """Connect to a SiLA server. Blocking."""
    ensure_sila2()
    from sila2.client import SilaClient  # noqa: PLC0415

    if tls_mode == TLS_MODE_INSECURE:
        return SilaClient(host, port, insecure=True)
    if tls_mode == TLS_MODE_PIN:
        cert = pinned_cert or fetch_server_certificate(host, port)
        return SilaClient(host, port, root_certs=cert.encode("ascii"))
    if tls_mode == TLS_MODE_SYSTEM:
        return SilaClient(host, port)
    raise ValueError(f"Unknown TLS mode: {tls_mode}")


def close_client(client: SilaClient) -> None:
    """Close a client and release all its resources. Blocking.

    ``SilaClient.close()`` only closes the gRPC channel; the client's
    internal subscription executor keeps its worker threads alive, so we
    shut it down explicitly to avoid leaking threads on unload.
    """
    client.close()
    client._task_executor.shutdown(wait=True, cancel_futures=True)


def read_server_info(client: SilaClient) -> SilaServerInfo:
    """Read server identity from the SiLAService core feature. Blocking."""
    service = client.SiLAService
    return SilaServerInfo(
        server_uuid=service.ServerUUID.get(),
        server_name=service.ServerName.get(),
        server_type=service.ServerType.get(),
        server_version=service.ServerVersion.get(),
        vendor_url=service.ServerVendorURL.get(),
        server_description=service.ServerDescription.get(),
    )

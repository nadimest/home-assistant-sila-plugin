"""Data update coordinator for a SiLA server."""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import TYPE_CHECKING, Any

import grpc
from sila2.client import SilaClient

if TYPE_CHECKING:
    from .command_runner import SilaCommandRunner

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .connection import SilaServerInfo
from .const import DOMAIN, POLL_INTERVAL_SECONDS

_LOGGER = logging.getLogger(__name__)

type SilaConfigEntry = ConfigEntry[SilaCoordinator]


def property_key(feature_id: str, property_id: str) -> str:
    """Key used to store a property value in coordinator data."""
    return f"{feature_id}.{property_id}"


class SilaCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Polls all unobservable properties of a SiLA server.

    Observable properties are push-based and handled by per-entity gRPC
    subscriptions; this coordinator additionally serves as the shared
    availability signal for the whole device.
    """

    config_entry: SilaConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        entry: SilaConfigEntry,
        client: SilaClient,
        server_info: SilaServerInfo,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=f"{DOMAIN}_{server_info.server_uuid}",
            update_interval=timedelta(seconds=POLL_INTERVAL_SECONDS),
        )
        self.client = client
        self.server_info = server_info
        # Set right after construction in async_setup_entry.
        self.command_runner: SilaCommandRunner = None  # type: ignore[assignment]

    async def _async_update_data(self) -> dict[str, Any]:
        return await self.hass.async_add_executor_job(self._fetch_all)

    def _fetch_all(self) -> dict[str, Any]:
        """Read every unobservable property of every feature. Blocking."""
        data: dict[str, Any] = {}
        for feature_id, feature in self.client._features.items():
            client_feature = getattr(self.client, feature_id, None)
            if client_feature is None:
                continue
            for prop_id in feature._unobservable_properties:
                key = property_key(feature_id, prop_id)
                try:
                    data[key] = getattr(client_feature, prop_id).get()
                except grpc.RpcError as err:
                    if err.code() in (
                        grpc.StatusCode.UNAVAILABLE,
                        grpc.StatusCode.DEADLINE_EXCEEDED,
                    ):
                        raise UpdateFailed(
                            f"SiLA server unreachable: {err.code().name}"
                        ) from err
                    # Property-level SiLA execution errors should not take
                    # down the whole device.
                    _LOGGER.debug("Error reading %s: %s", key, err)
                    data[key] = None
                except Exception as err:  # noqa: BLE001 - defensive: arbitrary server types
                    _LOGGER.debug("Error reading %s: %s", key, err)
                    data[key] = None
        return data

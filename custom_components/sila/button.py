"""Buttons for parameterless SiLA commands.

Commands that take parameters are exposed through the ``sila.call_command``
service instead, since buttons cannot carry arguments.
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.button import ButtonEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .cloud import SilaCloudGateway
from .const import SILA_SERVICE_FEATURE, cloud_new_server_signal
from .coordinator import SilaConfigEntry, SilaCoordinator
from .entity import SilaEntity

_LOGGER = logging.getLogger(__name__)


def _build_buttons(coordinator: SilaCoordinator) -> list[ButtonEntity]:
    """One button per parameterless command."""
    entities: list[ButtonEntity] = []

    for feature_id, feature in coordinator.client._features.items():
        if feature_id == SILA_SERVICE_FEATURE:
            # SetServerName etc. are not useful as buttons.
            continue
        for command_id, command in feature._unobservable_commands.items():
            if command.parameters.fields:
                continue
            entities.append(
                SilaCommandButton(coordinator, feature_id, feature, command_id, command)
            )
        for command_id, command in feature._observable_commands.items():
            if command.parameters.fields:
                continue
            entities.append(
                SilaObservableCommandButton(
                    coordinator, feature_id, feature, command_id, command
                )
            )

    return entities


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SilaConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    if isinstance(entry.runtime_data, SilaCloudGateway):
        gateway = entry.runtime_data
        added: set[str] = set()

        @callback
        def _async_add_server(coordinator: SilaCoordinator) -> None:
            if (uuid := coordinator.server_info.server_uuid) in added:
                return
            added.add(uuid)
            async_add_entities(_build_buttons(coordinator))

        entry.async_on_unload(
            async_dispatcher_connect(
                hass, cloud_new_server_signal(entry.entry_id), _async_add_server
            )
        )
        for coordinator in gateway.coordinators.values():
            _async_add_server(coordinator)
        return

    async_add_entities(_build_buttons(entry.runtime_data))


class SilaCommandButton(SilaEntity, ButtonEntity):
    """Fires a parameterless SiLA command."""

    def __init__(
        self,
        coordinator: SilaCoordinator,
        feature_id: str,
        feature: Any,
        command_id: str,
        command: Any,
    ) -> None:
        super().__init__(coordinator)
        self._feature_id = feature_id
        self._command_id = command_id
        self._attr_unique_id = (
            f"{coordinator.server_info.server_uuid}_{feature_id}_{command_id}"
        )
        self._attr_name = f"{feature._display_name} {command._display_name}"

    async def async_press(self) -> None:
        client_feature = getattr(self.coordinator.client, self._feature_id)
        command = getattr(client_feature, self._command_id)
        try:
            await self.hass.async_add_executor_job(command)
        except Exception as err:
            raise HomeAssistantError(
                f"SiLA command {self._feature_id}.{self._command_id} failed: {err}"
            ) from err


class SilaObservableCommandButton(SilaCommandButton):
    """Starts a parameterless observable command; progress is reported by
    the matching command status sensor."""

    async def async_press(self) -> None:
        await self.coordinator.command_runner.async_start(
            self._feature_id, self._command_id, {}
        )

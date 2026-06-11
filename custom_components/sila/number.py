"""Number entities for SiLA commands with a single numeric parameter.

Buttons cannot carry arguments, so commands like SetTargetTemperature(Real)
would otherwise be invisible on the device page. Setting the number calls
the command with that value. If the feature also has an unobservable
property named like the parameter (SetTargetTemperature's TargetTemperature
parameter ↔ TargetTemperature property), the entity shows its live value.

Commands with multiple or non-numeric parameters remain service-only
(``sila.call_command``).
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .cloud import SilaCloudGateway
from .const import SILA_SERVICE_FEATURE, cloud_new_server_signal
from .coordinator import SilaConfigEntry, SilaCoordinator, property_key
from .entity import SilaEntity

_LOGGER = logging.getLogger(__name__)


def _single_numeric_parameter(command: Any) -> tuple[Any, type] | None:
    """Return (parameter, python_type) if the command takes exactly one number."""
    fields = command.parameters.fields
    if len(fields) != 1:
        return None
    data_type = fields[0].data_type
    while hasattr(data_type, "base_type"):  # unwrap Constrained
        data_type = data_type.base_type
    type_name = type(data_type).__name__
    if type_name == "Real":
        return fields[0], float
    if type_name == "Integer":
        return fields[0], int
    return None


def _build_numbers(coordinator: SilaCoordinator) -> list[NumberEntity]:
    entities: list[NumberEntity] = []
    for feature_id, feature in coordinator.client._features.items():
        if feature_id == SILA_SERVICE_FEATURE:
            continue
        for command_id, command in {
            **feature._unobservable_commands,
            **feature._observable_commands,
        }.items():
            if (match := _single_numeric_parameter(command)) is None:
                continue
            parameter, value_type = match
            entities.append(
                SilaCommandNumber(
                    coordinator,
                    feature_id,
                    feature,
                    command_id,
                    command,
                    parameter,
                    value_type,
                    observable=command_id in feature._observable_commands,
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
            async_add_entities(_build_numbers(coordinator))

        entry.async_on_unload(
            async_dispatcher_connect(
                hass, cloud_new_server_signal(entry.entry_id), _async_add_server
            )
        )
        for coordinator in gateway.coordinators.values():
            _async_add_server(coordinator)
        return

    async_add_entities(_build_numbers(entry.runtime_data))


class SilaCommandNumber(SilaEntity, NumberEntity):
    """Calls a single-numeric-parameter SiLA command when set."""

    _attr_icon = "mdi:tune-variant"
    _attr_mode = NumberMode.BOX
    _attr_native_min_value = -1e12
    _attr_native_max_value = 1e12

    def __init__(
        self,
        coordinator: SilaCoordinator,
        feature_id: str,
        feature: Any,
        command_id: str,
        command: Any,
        parameter: Any,
        value_type: type,
        observable: bool,
    ) -> None:
        super().__init__(coordinator)
        self._feature_id = feature_id
        self._command_id = command_id
        self._parameter_id = parameter._identifier
        self._value_type = value_type
        self._observable = observable
        self._attr_native_step = 1 if value_type is int else 0.1
        self._attr_unique_id = (
            f"{coordinator.server_info.server_uuid}_{feature_id}_{command_id}_number"
        )
        self._attr_name = f"{feature._display_name} {command._display_name}"
        # Mirror a property named like the parameter, if the feature has one.
        self._mirror_key = (
            property_key(feature_id, self._parameter_id)
            if self._parameter_id in feature._unobservable_properties
            else None
        )

    @property
    def native_value(self) -> float | None:
        if self._mirror_key is not None:
            value = (self.coordinator.data or {}).get(self._mirror_key)
            if isinstance(value, (int, float)):
                return value
            return None
        return self._attr_native_value

    async def async_set_native_value(self, value: float) -> None:
        parameters = {self._parameter_id: self._value_type(value)}
        if self._observable:
            await self.coordinator.command_runner.async_start(
                self._feature_id, self._command_id, parameters
            )
        else:
            command = getattr(
                getattr(self.coordinator.client, self._feature_id), self._command_id
            )
            try:
                response = await self.hass.async_add_executor_job(
                    lambda: command(**parameters)
                )
            except Exception as err:
                raise HomeAssistantError(
                    f"SiLA command {self._feature_id}.{self._command_id} "
                    f"failed: {err}"
                ) from err
            self.coordinator.last_command_parameters[
                (self._feature_id, self._command_id)
            ] = parameters
            self.coordinator.publish_command_responses(
                self._feature_id, self._command_id, response
            )
        if self._mirror_key is not None:
            await self.coordinator.async_request_refresh()
        else:
            self._attr_native_value = value
            self.async_write_ha_state()

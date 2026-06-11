"""Dynamically generated sensors for SiLA server properties."""

from __future__ import annotations

import logging
from typing import Any

from sila2.client.subscription import Subscription

from homeassistant.components.sensor import SensorEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .cloud import SilaCloudGateway
from .command_runner import STATUS_IDLE, CommandExecution
from .const import (
    SILA_SERVICE_FEATURE,
    cloud_new_server_signal,
    command_update_signal,
)
from .coordinator import SilaConfigEntry, SilaCoordinator, property_key
from .entity import SilaEntity, render_state

_LOGGER = logging.getLogger(__name__)


def _build_sensors(
    coordinator: SilaCoordinator, entry: SilaConfigEntry
) -> list[SensorEntity]:
    """One sensor per SiLA property plus one status sensor per observable command."""
    entities: list[SensorEntity] = []
    for feature_id, feature in coordinator.client._features.items():
        for prop_id, prop in feature._unobservable_properties.items():
            entities.append(
                SilaPolledPropertySensor(coordinator, feature_id, feature, prop_id, prop)
            )
        for prop_id, prop in feature._observable_properties.items():
            entities.append(
                SilaObservablePropertySensor(
                    coordinator, feature_id, feature, prop_id, prop
                )
            )
        for command_id, command in feature._observable_commands.items():
            entities.append(
                SilaCommandStatusSensor(
                    coordinator, entry, feature_id, feature, command_id, command
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
            async_add_entities(_build_sensors(coordinator, entry))

        entry.async_on_unload(
            async_dispatcher_connect(
                hass, cloud_new_server_signal(entry.entry_id), _async_add_server
            )
        )
        for coordinator in gateway.coordinators.values():
            _async_add_server(coordinator)
        return

    async_add_entities(_build_sensors(entry.runtime_data, entry))


class SilaPropertySensorBase(SilaEntity, SensorEntity):
    """Common naming/identity for property sensors."""

    def __init__(
        self,
        coordinator: SilaCoordinator,
        feature_id: str,
        feature: Any,
        prop_id: str,
        prop: Any,
    ) -> None:
        super().__init__(coordinator)
        self._feature_id = feature_id
        self._prop_id = prop_id
        self._attr_unique_id = (
            f"{coordinator.server_info.server_uuid}_{feature_id}_{prop_id}"
        )
        if feature_id == SILA_SERVICE_FEATURE:
            # Core feature: expose as diagnostics, not regular sensors.
            self._attr_entity_category = EntityCategory.DIAGNOSTIC
            self._attr_name = prop._display_name
        else:
            self._attr_name = f"{feature._display_name} {prop._display_name}"

    @callback
    def _update_from_value(self, value: Any) -> None:
        state, attrs = render_state(value)
        self._attr_native_value = state
        self._attr_extra_state_attributes = {
            "feature": self._feature_id,
            "property": self._prop_id,
            **attrs,
        }


class SilaPolledPropertySensor(SilaPropertySensorBase):
    """Unobservable SiLA property, polled via the coordinator."""

    def _handle_coordinator_update(self) -> None:
        key = property_key(self._feature_id, self._prop_id)
        self._update_from_value((self.coordinator.data or {}).get(key))
        super()._handle_coordinator_update()

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        key = property_key(self._feature_id, self._prop_id)
        self._update_from_value((self.coordinator.data or {}).get(key))


class SilaObservablePropertySensor(SilaPropertySensorBase):
    """Observable SiLA property, push-updated via a gRPC subscription."""

    def __init__(self, *args: Any) -> None:
        super().__init__(*args)
        self._subscription: Subscription | None = None

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        await self._async_subscribe()

    async def async_will_remove_from_hass(self) -> None:
        await self._async_unsubscribe()
        await super().async_will_remove_from_hass()

    async def _async_subscribe(self) -> None:
        try:
            self._subscription = await self.hass.async_add_executor_job(
                self._start_subscription
            )
        except Exception as err:  # noqa: BLE001 - keep entity alive; coordinator tracks availability
            _LOGGER.warning(
                "Could not subscribe to %s.%s: %s",
                self._feature_id,
                self._prop_id,
                err,
            )

    def _start_subscription(self) -> Subscription:
        """Open the gRPC subscription stream. Blocking."""
        client_feature = getattr(self.coordinator.client, self._feature_id)
        subscription = getattr(client_feature, self._prop_id).subscribe()
        subscription.add_callback(self._handle_stream_value)
        return subscription

    def _handle_stream_value(self, value: Any) -> None:
        """Called from the gRPC subscription thread."""
        self.hass.loop.call_soon_threadsafe(self._async_handle_stream_value, value)

    @callback
    def _async_handle_stream_value(self, value: Any) -> None:
        if self.hass is None or not self.coordinator.last_update_success:
            return
        self._update_from_value(value)
        self.async_write_ha_state()

    async def _async_unsubscribe(self) -> None:
        if self._subscription is not None and not self._subscription.is_cancelled:
            subscription = self._subscription
            self._subscription = None
            await self.hass.async_add_executor_job(subscription.cancel)

    def _handle_coordinator_update(self) -> None:
        # The coordinator only provides availability for this entity; if the
        # server came back after an outage, the old stream is dead, so renew it.
        if self.coordinator.last_update_success and (
            self._subscription is None or self._subscription.is_cancelled
        ):
            self.hass.async_create_task(self._async_resubscribe())
        super()._handle_coordinator_update()

    async def _async_resubscribe(self) -> None:
        await self._async_unsubscribe()
        await self._async_subscribe()


class SilaCommandStatusSensor(SilaEntity, SensorEntity):
    """Execution status of an observable (long-running) SiLA command.

    State is idle/waiting/running/finishedSuccessfully/finishedWithError;
    progress, remaining time, and final responses appear as attributes.
    Updates arrive via dispatcher from the command runner.
    """

    def __init__(
        self,
        coordinator: SilaCoordinator,
        entry: SilaConfigEntry,
        feature_id: str,
        feature: Any,
        command_id: str,
        command: Any,
    ) -> None:
        super().__init__(coordinator)
        self._entry = entry
        self._feature_id = feature_id
        self._command_id = command_id
        self._attr_unique_id = (
            f"{coordinator.server_info.server_uuid}_{feature_id}_{command_id}_status"
        )
        self._attr_name = f"{feature._display_name} {command._display_name} status"
        self._attr_native_value = STATUS_IDLE

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                command_update_signal(self._entry.entry_id),
                self._handle_execution_update,
            )
        )
        # Pick up an execution already running at entity setup time.
        execution = self.coordinator.command_runner.executions.get(
            (self._feature_id, self._command_id)
        )
        if execution is not None:
            self._apply_execution(execution)

    @callback
    def _handle_execution_update(self, execution: CommandExecution) -> None:
        if (execution.server_uuid, execution.feature_id, execution.command_id) != (
            self.coordinator.server_info.server_uuid,
            self._feature_id,
            self._command_id,
        ):
            return
        self._apply_execution(execution)
        self.async_write_ha_state()

    @callback
    def _apply_execution(self, execution: CommandExecution) -> None:
        self._attr_native_value = execution.status
        self._attr_extra_state_attributes = {
            "feature": self._feature_id,
            "command": self._command_id,
            "execution_uuid": execution.execution_uuid,
            "progress": execution.progress,
            "estimated_remaining_seconds": execution.remaining_seconds,
            "responses": execution.responses,
            "error": execution.error,
        }

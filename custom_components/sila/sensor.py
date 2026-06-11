"""Dynamically generated sensors for SiLA server properties."""

from __future__ import annotations

import logging
from typing import Any

from sila2.client.subscription import Subscription

from homeassistant.components.sensor import SensorEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import SILA_SERVICE_FEATURE
from .coordinator import SilaConfigEntry, SilaCoordinator, property_key
from .entity import SilaEntity, render_state

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SilaConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Create one sensor per SiLA property, across all features."""
    coordinator = entry.runtime_data
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

    async_add_entities(entities)


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

"""Image entities for SiLA commands that return a single binary payload.

A command like ``GrabSnapshot(Zoom: Real) -> ImagePayload: Binary`` gets an
image entity showing the latest payload. The entity updates whenever the
command runs — via its button or number entity, the ``sila.call_command``
service, or an observable command finishing — and can re-run the command
itself with the last-used parameters when refreshed through
``homeassistant.update_entity``.
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.image import ImageEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util import dt as dt_util

from .cloud import SilaCloudGateway
from .const import (
    SILA_SERVICE_FEATURE,
    cloud_new_server_signal,
    command_responses_signal,
)
from .coordinator import SilaConfigEntry, SilaCoordinator
from .entity import SilaEntity

_LOGGER = logging.getLogger(__name__)


def _single_binary_response(command: Any) -> Any | None:
    """Return the response field if the command returns exactly one Binary."""
    fields = command.responses.fields
    if len(fields) != 1:
        return None
    data_type = fields[0].data_type
    while hasattr(data_type, "base_type"):  # unwrap Constrained
        data_type = data_type.base_type
    if type(data_type).__name__ == "Binary":
        return fields[0]
    return None


def _sniff_content_type(data: bytes) -> str:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return "image/jpeg"


def _build_images(
    hass: HomeAssistant, coordinator: SilaCoordinator
) -> list[ImageEntity]:
    entities: list[ImageEntity] = []
    for feature_id, feature in coordinator.client._features.items():
        if feature_id == SILA_SERVICE_FEATURE:
            continue
        for command_id, command in {
            **feature._unobservable_commands,
            **feature._observable_commands,
        }.items():
            if (response := _single_binary_response(command)) is None:
                continue
            entities.append(
                SilaCommandImage(
                    hass,
                    coordinator,
                    feature_id,
                    feature,
                    command_id,
                    command,
                    response,
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
            async_add_entities(_build_images(hass, coordinator))

        entry.async_on_unload(
            async_dispatcher_connect(
                hass, cloud_new_server_signal(entry.entry_id), _async_add_server
            )
        )
        for coordinator in gateway.coordinators.values():
            _async_add_server(coordinator)
        return

    async_add_entities(_build_images(hass, entry.runtime_data))


class SilaCommandImage(SilaEntity, ImageEntity):
    """Shows the latest binary payload returned by a SiLA command."""

    def __init__(
        self,
        hass: HomeAssistant,
        coordinator: SilaCoordinator,
        feature_id: str,
        feature: Any,
        command_id: str,
        command: Any,
        response: Any,
        observable: bool,
    ) -> None:
        SilaEntity.__init__(self, coordinator)
        ImageEntity.__init__(self, hass)
        self._feature_id = feature_id
        self._command_id = command_id
        self._response_id = response._identifier
        self._has_parameters = bool(command.parameters.fields)
        self._observable = observable
        self._payload: bytes | None = None
        self._attr_unique_id = (
            f"{coordinator.server_info.server_uuid}_{feature_id}_{command_id}_image"
        )
        self._attr_name = f"{feature._display_name} {response._display_name}"

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                command_responses_signal(self.coordinator.config_entry.entry_id),
                self._handle_responses,
            )
        )

    @callback
    def _handle_responses(
        self, server_uuid: str, feature_id: str, command_id: str, responses: Any
    ) -> None:
        if (
            server_uuid != self.coordinator.server_info.server_uuid
            or feature_id != self._feature_id
            or command_id != self._command_id
        ):
            return
        payload = getattr(responses, self._response_id, None)
        if not isinstance(payload, bytes) or not payload:
            return
        self._payload = payload
        self._attr_content_type = _sniff_content_type(payload)
        self._attr_image_last_updated = dt_util.utcnow()
        self.async_write_ha_state()

    async def async_image(self) -> bytes | None:
        return self._payload

    async def async_update(self) -> None:
        """Re-run the command on ``homeassistant.update_entity``."""
        parameters = self.coordinator.last_command_parameters.get(
            (self._feature_id, self._command_id)
        )
        if parameters is None:
            if self._has_parameters:
                raise HomeAssistantError(
                    f"No known parameters for "
                    f"{self._feature_id}.{self._command_id} yet; run it once "
                    f"via its entity or sila.call_command first"
                )
            parameters = {}

        if self._observable:
            execution = await self.coordinator.command_runner.async_start(
                self._feature_id, self._command_id, parameters
            )
            await execution.task
            return

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
        self.coordinator.publish_command_responses(
            self._feature_id, self._command_id, response
        )

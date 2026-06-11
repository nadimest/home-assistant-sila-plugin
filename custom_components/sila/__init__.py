"""The SiLA 2 integration."""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import ATTR_DEVICE_ID, CONF_HOST, CONF_PORT, Platform
from homeassistant.core import (
    HomeAssistant,
    ServiceCall,
    ServiceResponse,
    SupportsResponse,
)
from homeassistant.exceptions import (
    ConfigEntryNotReady,
    HomeAssistantError,
    ServiceValidationError,
)
from homeassistant.helpers import config_validation as cv, device_registry as dr

from .cloud import SilaCloudGateway
from .command_runner import SilaCommandRunner
from .connection import close_client, create_client, read_server_info
from .const import (
    ATTR_COMMAND,
    ATTR_FEATURE,
    ATTR_PARAMETERS,
    ATTR_WAIT,
    CONF_MODE,
    CONF_PINNED_CERT,
    CONF_TLS_MODE,
    DOMAIN,
    MODE_CLOUD,
    SERVICE_CALL_COMMAND,
)
from .coordinator import SilaConfigEntry, SilaCoordinator

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [Platform.BUTTON, Platform.IMAGE, Platform.NUMBER, Platform.SENSOR]

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

SERVICE_CALL_COMMAND_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_DEVICE_ID): cv.string,
        vol.Required(ATTR_FEATURE): cv.string,
        vol.Required(ATTR_COMMAND): cv.string,
        vol.Optional(ATTR_PARAMETERS, default=dict): dict,
        vol.Optional(ATTR_WAIT, default=True): cv.boolean,
    }
)


async def async_setup(hass: HomeAssistant, config: dict[str, Any]) -> bool:
    """Register integration-level services."""

    async def handle_call_command(call: ServiceCall) -> ServiceResponse:
        coordinator = _coordinator_for_device(hass, call.data[ATTR_DEVICE_ID])
        feature_id = call.data[ATTR_FEATURE]
        command_id = call.data[ATTR_COMMAND]
        parameters: dict[str, Any] = call.data[ATTR_PARAMETERS]

        client_feature = getattr(coordinator.client, feature_id, None)
        if client_feature is None:
            raise ServiceValidationError(
                f"Server does not implement feature '{feature_id}'"
            )
        feature = coordinator.client._features[feature_id]

        if command_id in feature._observable_commands:
            execution = await coordinator.command_runner.async_start(
                feature_id, command_id, parameters
            )
            if not call.data[ATTR_WAIT]:
                if call.return_response:
                    return {"execution_uuid": execution.execution_uuid}
                return None
            await execution.task
            if execution.error is not None:
                raise HomeAssistantError(
                    f"SiLA command {feature_id}.{command_id} failed: "
                    f"{execution.error}"
                )
            if call.return_response:
                return {
                    "execution_uuid": execution.execution_uuid,
                    "status": execution.status,
                    "responses": execution.responses or {},
                }
            return None

        if command_id not in feature._unobservable_commands:
            raise ServiceValidationError(
                f"Feature '{feature_id}' has no command '{command_id}'"
            )

        command = getattr(client_feature, command_id)
        try:
            response = await hass.async_add_executor_job(
                lambda: command(**parameters)
            )
        except Exception as err:
            raise HomeAssistantError(
                f"SiLA command {feature_id}.{command_id} failed: {err}"
            ) from err
        coordinator.last_command_parameters[(feature_id, command_id)] = parameters
        coordinator.publish_command_responses(feature_id, command_id, response)
        if call.return_response:
            return dict(response._asdict()) if hasattr(response, "_asdict") else {}
        return None

    hass.services.async_register(
        DOMAIN,
        SERVICE_CALL_COMMAND,
        handle_call_command,
        schema=SERVICE_CALL_COMMAND_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )
    return True


async def async_setup_entry(hass: HomeAssistant, entry: SilaConfigEntry) -> bool:
    """Set up a SiLA server connection or the cloud gateway endpoint."""
    if entry.data.get(CONF_MODE) == MODE_CLOUD:
        gateway = SilaCloudGateway(hass, entry, entry.data[CONF_PORT])
        entry.runtime_data = gateway
        await gateway.async_start()
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
        return True

    try:
        client = await hass.async_add_executor_job(
            create_client,
            entry.data[CONF_HOST],
            entry.data[CONF_PORT],
            entry.data[CONF_TLS_MODE],
            entry.data.get(CONF_PINNED_CERT),
        )
        server_info = await hass.async_add_executor_job(read_server_info, client)
    except Exception as err:
        raise ConfigEntryNotReady(
            f"Cannot connect to SiLA server at "
            f"{entry.data[CONF_HOST]}:{entry.data[CONF_PORT]}"
        ) from err

    coordinator = SilaCoordinator(hass, entry, client, server_info)
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator
    coordinator.command_runner = SilaCommandRunner(hass, entry, coordinator)

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: SilaConfigEntry) -> bool:
    """Unload a SiLA server or the cloud gateway."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        if isinstance(entry.runtime_data, SilaCloudGateway):
            await entry.runtime_data.async_stop()
        else:
            await hass.async_add_executor_job(
                close_client, entry.runtime_data.client
            )
    return unload_ok


def _coordinator_for_device(hass: HomeAssistant, device_id: str) -> SilaCoordinator:
    """Resolve a device_id from a service call to its coordinator."""
    device = dr.async_get(hass).async_get(device_id)
    if device is None:
        raise ServiceValidationError(f"Unknown device: {device_id}")
    server_uuids = {
        identifier[1] for identifier in device.identifiers if identifier[0] == DOMAIN
    }
    for entry_id in device.config_entries:
        entry = hass.config_entries.async_get_entry(entry_id)
        if (
            entry is None
            or entry.domain != DOMAIN
            or entry.state is not ConfigEntryState.LOADED
        ):
            continue
        if isinstance(entry.runtime_data, SilaCloudGateway):
            for uuid in server_uuids:
                if (coordinator := entry.runtime_data.coordinators.get(uuid)) is not None:
                    return coordinator
            continue
        return entry.runtime_data
    raise ServiceValidationError(f"Device {device_id} is not a loaded SiLA server")

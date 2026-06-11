"""End-to-end tests against a live in-process SiLA server."""

from __future__ import annotations

from ipaddress import ip_address

import pytest
from homeassistant.config_entries import SOURCE_USER, SOURCE_ZEROCONF
from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.service_info.zeroconf import ZeroconfServiceInfo
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.sila.const import (
    CONF_PINNED_CERT,
    CONF_TLS_MODE,
    DOMAIN,
    TLS_MODE_INSECURE,
)

# socket_enabled: tests talk to a real in-process SiLA server over localhost.
# mock_async_zeroconf: keep HA's zeroconf component off the real network.
pytestmark = pytest.mark.usefixtures("socket_enabled", "mock_async_zeroconf")


async def test_config_flow_and_setup(hass: HomeAssistant, demo_server) -> None:
    """Manual config flow connects, creates an entry, device, and entities."""
    server, port = demo_server

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    assert result["type"] == "form"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_HOST: "127.0.0.1", CONF_PORT: port, CONF_TLS_MODE: TLS_MODE_INSECURE},
    )
    assert result["type"] == "create_entry"
    assert result["title"] == "Demo Thermostat"
    entry = result["result"]
    assert entry.unique_id == str(server.server_uuid)
    await hass.async_block_till_done()

    # Device registered with SiLA server identity
    device = dr.async_get(hass).async_get_device(
        identifiers={(DOMAIN, str(server.server_uuid))}
    )
    assert device is not None
    assert device.model == "DemoThermostat"

    # Polled (unobservable) property sensor
    target = hass.states.get("sensor.demo_thermostat_temperature_controller_target_temperature")
    assert target is not None
    assert float(target.state) == 21.0

    # Diagnostic sensor from the SiLAService core feature
    server_name = hass.states.get("sensor.demo_thermostat_server_name")
    assert server_name is not None
    assert server_name.state == "Demo Thermostat"

    # Button for the parameterless Reset command
    assert hass.states.get("button.demo_thermostat_temperature_controller_reset") is not None

    # Observable property sensor gets push updates
    state = hass.states.get("sensor.demo_thermostat_temperature_controller_current_temperature")
    assert state is not None

    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_zeroconf_discovery_flow(hass: HomeAssistant, demo_server) -> None:
    """A server announced via mDNS leads to a confirm step and an entry."""
    server, port = demo_server

    discovery_info = ZeroconfServiceInfo(
        ip_address=ip_address("127.0.0.1"),
        ip_addresses=[ip_address("127.0.0.1")],
        hostname="demo-thermostat.local.",
        name=f"{server.server_uuid}._sila._tcp.local.",
        port=port,
        properties={},
        type="_sila._tcp.local.",
    )
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_ZEROCONF}, data=discovery_info
    )
    assert result["type"] == "form"
    assert result["step_id"] == "zeroconf_confirm"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_TLS_MODE: TLS_MODE_INSECURE}
    )
    assert result["type"] == "create_entry"
    assert result["title"] == "Demo Thermostat"
    assert result["result"].unique_id == str(server.server_uuid)
    await hass.async_block_till_done()

    # Re-discovery of a configured server aborts instead of prompting again.
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_ZEROCONF}, data=discovery_info
    )
    assert result["type"] == "abort"
    assert result["reason"] == "already_configured"

    entry = hass.config_entries.async_entries(DOMAIN)[0]
    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_call_command_service(hass: HomeAssistant, demo_server) -> None:
    """The sila.call_command service calls commands with parameters."""
    server, port = demo_server

    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=str(server.server_uuid),
        data={
            CONF_HOST: "127.0.0.1",
            CONF_PORT: port,
            CONF_TLS_MODE: TLS_MODE_INSECURE,
            CONF_PINNED_CERT: None,
        },
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    device = dr.async_get(hass).async_get_device(
        identifiers={(DOMAIN, str(server.server_uuid))}
    )
    await hass.services.async_call(
        DOMAIN,
        "call_command",
        {
            "device_id": device.id,
            "feature": "TemperatureController",
            "command": "SetTargetTemperature",
            "parameters": {"TargetTemperature": 42.0},
        },
        blocking=True,
    )
    assert server.temperaturecontroller._target == 42.0

    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_observable_command(hass: HomeAssistant, demo_server) -> None:
    """Observable commands run via the service, fire events, update sensors."""
    server, port = demo_server

    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=str(server.server_uuid),
        data={
            CONF_HOST: "127.0.0.1",
            CONF_PORT: port,
            CONF_TLS_MODE: TLS_MODE_INSECURE,
            CONF_PINNED_CERT: None,
        },
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    status_entity = "sensor.demo_thermostat_temperature_controller_equilibrate_status"
    assert hass.states.get(status_entity).state == "idle"

    events = []
    hass.bus.async_listen("sila_command_started", events.append)
    hass.bus.async_listen("sila_command_finished", events.append)

    device = dr.async_get(hass).async_get_device(
        identifiers={(DOMAIN, str(server.server_uuid))}
    )
    response = await hass.services.async_call(
        DOMAIN,
        "call_command",
        {
            "device_id": device.id,
            "feature": "TemperatureController",
            "command": "Equilibrate",
            "parameters": {"Duration": 0.5},
            "wait": True,
        },
        blocking=True,
        return_response=True,
    )
    await hass.async_block_till_done()

    assert response["status"] == "finishedSuccessfully"
    assert "FinalTemperature" in response["responses"]

    event_types = [e.event_type for e in events]
    assert "sila_command_started" in event_types
    assert "sila_command_finished" in event_types
    finished = next(e for e in events if e.event_type == "sila_command_finished")
    assert finished.data["device_id"] == device.id
    assert finished.data["status"] == "finishedSuccessfully"
    assert finished.data["responses"] is not None

    state = hass.states.get(status_entity)
    assert state.state == "finishedSuccessfully"
    assert state.attributes["responses"] is not None

    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()

"""Config flow for the SiLA 2 integration."""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

import voluptuous as vol

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.core import HomeAssistant
from homeassistant.helpers.selector import (
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
)
from homeassistant.helpers.service_info.zeroconf import ZeroconfServiceInfo

from .connection import (
    close_client,
    create_client,
    fetch_server_certificate,
    read_server_info,
)
from .const import (
    CONF_MODE,
    CONF_PINNED_CERT,
    CONF_TLS_MODE,
    DEFAULT_CLOUD_PORT,
    DEFAULT_PORT,
    DOMAIN,
    MODE_CLOUD,
    MODE_CONNECT,
    TLS_MODE_PIN,
    TLS_MODES,
)

_LOGGER = logging.getLogger(__name__)

STEP_USER_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_HOST): str,
        vol.Required(CONF_PORT, default=DEFAULT_PORT): int,
        vol.Required(CONF_TLS_MODE, default=TLS_MODE_PIN): SelectSelector(
            SelectSelectorConfig(
                options=TLS_MODES,
                mode=SelectSelectorMode.DROPDOWN,
                translation_key="tls_mode",
            )
        ),
    }
)

STEP_CONFIRM_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_TLS_MODE, default=TLS_MODE_PIN): SelectSelector(
            SelectSelectorConfig(
                options=TLS_MODES,
                mode=SelectSelectorMode.DROPDOWN,
                translation_key="tls_mode",
            )
        ),
    }
)


def _validate_connection(
    host: str, port: int, tls_mode: str
) -> tuple[dict[str, Any], dict[str, str]]:
    """Connect, read server identity, disconnect. Blocking.

    Returns (entry_data, info) or raises.
    """
    pinned_cert: str | None = None
    if tls_mode == TLS_MODE_PIN:
        pinned_cert = fetch_server_certificate(host, port)

    client = create_client(host, port, tls_mode, pinned_cert)
    try:
        server_info = read_server_info(client)
    finally:
        close_client(client)

    entry_data = {
        CONF_MODE: MODE_CONNECT,
        CONF_HOST: host,
        CONF_PORT: port,
        CONF_TLS_MODE: tls_mode,
        CONF_PINNED_CERT: pinned_cert,
    }
    info = {
        "server_uuid": server_info.server_uuid,
        "server_name": server_info.server_name,
    }
    return entry_data, info


async def async_validate_connection(
    hass: HomeAssistant, host: str, port: int, tls_mode: str
) -> tuple[dict[str, Any], dict[str, str]]:
    return await hass.async_add_executor_job(
        _validate_connection, host, port, tls_mode
    )


class SilaConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle config flows for SiLA servers."""

    VERSION = 1

    def __init__(self) -> None:
        self._discovered_host: str | None = None
        self._discovered_port: int | None = None
        self._discovered_name: str | None = None

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Choose between connecting to a server and hosting a cloud endpoint."""
        return self.async_show_menu(step_id="user", menu_options=["connect", "cloud"])

    async def async_step_cloud(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Host a SiLA 2 v1.1 cloud endpoint for server-initiated connections."""
        if user_input is not None:
            port = user_input[CONF_PORT]
            await self.async_set_unique_id(f"cloud_{port}")
            self._abort_if_unique_id_configured()
            return self.async_create_entry(
                title=f"SiLA Cloud Gateway (port {port})",
                data={CONF_MODE: MODE_CLOUD, CONF_PORT: port},
            )
        return self.async_show_form(
            step_id="cloud",
            data_schema=vol.Schema(
                {vol.Required(CONF_PORT, default=DEFAULT_CLOUD_PORT): int}
            ),
        )

    async def async_step_connect(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle manual setup of an outbound server connection."""
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                entry_data, info = await async_validate_connection(
                    self.hass,
                    user_input[CONF_HOST],
                    user_input[CONF_PORT],
                    user_input[CONF_TLS_MODE],
                )
            except Exception:  # noqa: BLE001 - surface any connection problem in the form
                _LOGGER.exception("Cannot connect to SiLA server")
                errors["base"] = "cannot_connect"
            else:
                await self.async_set_unique_id(info["server_uuid"])
                self._abort_if_unique_id_configured(
                    updates={
                        CONF_HOST: user_input[CONF_HOST],
                        CONF_PORT: user_input[CONF_PORT],
                    }
                )
                return self.async_create_entry(
                    title=info["server_name"], data=entry_data
                )

        return self.async_show_form(
            step_id="connect", data_schema=STEP_USER_SCHEMA, errors=errors
        )

    async def async_step_zeroconf(
        self, discovery_info: ZeroconfServiceInfo
    ) -> ConfigFlowResult:
        """Handle a SiLA server discovered via mDNS."""
        host = discovery_info.host
        port = discovery_info.port or DEFAULT_PORT

        # SiLA servers announce themselves as "<server-uuid>._sila._tcp.local."
        try:
            server_uuid = str(UUID(discovery_info.name.split(".")[0]))
        except ValueError:
            server_uuid = None

        if server_uuid is not None:
            await self.async_set_unique_id(server_uuid)
            self._abort_if_unique_id_configured(
                updates={CONF_HOST: host, CONF_PORT: port}
            )

        self._discovered_host = host
        self._discovered_port = port
        self._discovered_name = (
            discovery_info.properties.get("server_name") or f"{host}:{port}"
        )
        self.context["title_placeholders"] = {"name": self._discovered_name}
        return await self.async_step_zeroconf_confirm()

    async def async_step_zeroconf_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Confirm a discovered server and choose how to trust it."""
        assert self._discovered_host is not None
        assert self._discovered_port is not None
        errors: dict[str, str] = {}

        if user_input is not None:
            try:
                entry_data, info = await async_validate_connection(
                    self.hass,
                    self._discovered_host,
                    self._discovered_port,
                    user_input[CONF_TLS_MODE],
                )
            except Exception:  # noqa: BLE001 - surface any connection problem in the form
                _LOGGER.exception("Cannot connect to SiLA server")
                errors["base"] = "cannot_connect"
            else:
                await self.async_set_unique_id(info["server_uuid"])
                self._abort_if_unique_id_configured(
                    updates={
                        CONF_HOST: self._discovered_host,
                        CONF_PORT: self._discovered_port,
                    }
                )
                return self.async_create_entry(
                    title=info["server_name"], data=entry_data
                )

        return self.async_show_form(
            step_id="zeroconf_confirm",
            data_schema=STEP_CONFIRM_SCHEMA,
            errors=errors,
            description_placeholders={
                "name": self._discovered_name or "",
                "host": self._discovered_host,
                "port": str(self._discovered_port),
            },
        )

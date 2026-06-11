"""Base entity for the SiLA 2 integration."""

from __future__ import annotations

from typing import Any

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import SilaCoordinator


def render_state(value: Any) -> tuple[Any, dict[str, Any]]:
    """Map an arbitrary SiLA property value to (state, extra_attributes).

    SiLA values can be scalars, datetimes, byte blobs, lists, or nested
    structures (namedtuples). HA sensor states must be short scalars, so
    complex values get a summary state with the details in attributes.
    """
    attrs: dict[str, Any] = {}
    if value is None:
        return None, attrs
    if isinstance(value, bool):
        return str(value).lower(), attrs
    if isinstance(value, (int, float)):
        return value, attrs
    if isinstance(value, str):
        if len(value) > 255:
            attrs["full_value"] = value
            return value[:252] + "...", attrs
        return value, attrs
    if isinstance(value, bytes):
        return f"{len(value)} bytes", attrs
    if hasattr(value, "_asdict"):  # SiLA structure
        attrs["structure"] = {k: str(v) for k, v in value._asdict().items()}
        return "structure", attrs
    if isinstance(value, (list, tuple)):
        attrs["items"] = [str(item) for item in value][:50]
        return len(value), attrs
    return str(value)[:255], attrs


class SilaEntity(CoordinatorEntity[SilaCoordinator]):
    """Entity belonging to a SiLA server device."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: SilaCoordinator) -> None:
        super().__init__(coordinator)
        info = coordinator.server_info
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, info.server_uuid)},
            name=info.server_name,
            manufacturer=info.vendor_url,
            model=info.server_type,
            sw_version=info.server_version,
        )

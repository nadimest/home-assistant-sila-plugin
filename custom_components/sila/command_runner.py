"""Execution of observable (long-running) SiLA commands.

Starting an observable command returns a ClientObservableCommandInstance
whose status/progress attributes are kept current by a sila2 background
thread. A watcher task mirrors them into HA: dispatcher updates for the
per-command status sensors while running, and HA events on start/finish
so automations can react.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.dispatcher import async_dispatcher_send

from .const import (
    COMMAND_WATCH_INTERVAL,
    DOMAIN,
    EVENT_COMMAND_FINISHED,
    EVENT_COMMAND_STARTED,
    command_update_signal,
)

if TYPE_CHECKING:
    from .coordinator import SilaConfigEntry

_LOGGER = logging.getLogger(__name__)

STATUS_IDLE = "idle"


@dataclass
class CommandExecution:
    """Tracks one observable command execution."""

    feature_id: str
    command_id: str
    execution_uuid: str
    status: str = "waiting"
    progress: float | None = None
    remaining_seconds: float | None = None
    responses: dict[str, Any] | None = None
    error: str | None = None
    task: asyncio.Task | None = field(default=None, repr=False)


class SilaCommandRunner:
    """Starts and watches observable command executions for one server."""

    def __init__(self, hass: HomeAssistant, entry: SilaConfigEntry) -> None:
        self._hass = hass
        self._entry = entry
        # Latest execution per (feature, command), for the status sensors.
        self.executions: dict[tuple[str, str], CommandExecution] = {}

    async def async_start(
        self, feature_id: str, command_id: str, parameters: dict[str, Any]
    ) -> CommandExecution:
        """Start an observable command and watch it until it finishes."""
        coordinator = self._entry.runtime_data
        command = getattr(getattr(coordinator.client, feature_id), command_id)
        try:
            instance = await self._hass.async_add_executor_job(
                lambda: command(**parameters)
            )
        except Exception as err:
            raise HomeAssistantError(
                f"Could not start SiLA command {feature_id}.{command_id}: {err}"
            ) from err

        execution = CommandExecution(
            feature_id=feature_id,
            command_id=command_id,
            execution_uuid=str(instance.execution_uuid),
        )
        self.executions[(feature_id, command_id)] = execution
        self._fire_event(EVENT_COMMAND_STARTED, execution)
        self._notify(execution)

        execution.task = self._entry.async_create_background_task(
            self._hass,
            self._watch(execution, instance),
            name=f"sila_command_{execution.execution_uuid}",
        )
        return execution

    async def _watch(self, execution: CommandExecution, instance: Any) -> None:
        while not instance.done:
            self._refresh(execution, instance)
            await asyncio.sleep(COMMAND_WATCH_INTERVAL)
        self._refresh(execution, instance)

        try:
            responses = await self._hass.async_add_executor_job(
                instance.get_responses
            )
            if hasattr(responses, "_asdict"):
                execution.responses = {
                    k: str(v) for k, v in responses._asdict().items()
                }
        except Exception as err:  # noqa: BLE001 - report any execution error to the user
            execution.error = str(err)
            execution.status = "finishedWithError"

        self._fire_event(EVENT_COMMAND_FINISHED, execution)
        self._notify(execution)

    def _refresh(self, execution: CommandExecution, instance: Any) -> None:
        if instance.status is not None:
            execution.status = instance.status.name
        execution.progress = instance.progress
        remaining = instance.estimated_remaining_time
        execution.remaining_seconds = (
            remaining.total_seconds() if remaining is not None else None
        )
        self._notify(execution)

    def _notify(self, execution: CommandExecution) -> None:
        async_dispatcher_send(
            self._hass, command_update_signal(self._entry.entry_id), execution
        )

    def _fire_event(self, event_type: str, execution: CommandExecution) -> None:
        info = self._entry.runtime_data.server_info
        device = dr.async_get(self._hass).async_get_device(
            identifiers={(DOMAIN, info.server_uuid)}
        )
        self._hass.bus.async_fire(
            event_type,
            {
                "device_id": device.id if device else None,
                "server_uuid": info.server_uuid,
                "server_name": info.server_name,
                "feature": execution.feature_id,
                "command": execution.command_id,
                "execution_uuid": execution.execution_uuid,
                "status": execution.status,
                "responses": execution.responses,
                "error": execution.error,
            },
        )

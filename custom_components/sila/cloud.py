"""SiLA 2 v1.1 cloud gateway: server-initiated connections.

Home Assistant hosts the CloudClientEndpoint gRPC service. SiLA servers
configured for server-initiated connections dial in and keep one
bidirectional stream open; all SiLA calls are multiplexed over it,
correlated by request UUID.

CloudSilaClient duck-types the parts of sila2.client.SilaClient that the
rest of this integration uses (``_features``, per-feature attribute access,
property ``get``/``subscribe``, callable commands), so the coordinator,
entities, and command runner work unchanged for cloud-connected servers.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from typing import TYPE_CHECKING, Any, Callable
from uuid import uuid4

import grpc
from sila2.features.silaservice import SiLAServiceFeature
from sila2.framework.command.execution_info import CommandExecutionStatus
from sila2.framework.feature import Feature

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_send

from .cloud_proto import SiLAClientMessage, cloud_pb2_grpc
from .connection import SilaServerInfo
from .const import cloud_new_server_signal

if TYPE_CHECKING:
    from .coordinator import SilaConfigEntry, SilaCoordinator

_LOGGER = logging.getLogger(__name__)

REQUEST_TIMEOUT = 30
COMMAND_TIMEOUT = 300


class CloudDisconnectedError(ConnectionError):
    """The SiLA server's cloud connection is gone."""


class CloudCallError(Exception):
    """The SiLA server reported an error for a call."""


def _describe_sila_error(error_msg: Any) -> str:
    which = error_msg.WhichOneof("error")
    if which is None:
        return "unknown SiLA error"
    sub = getattr(error_msg, which)
    message = getattr(sub, "message", "")
    identifier = getattr(sub, "errorIdentifier", "")
    return f"{which}{f' {identifier}' if identifier else ''}: {message}"


class CloudConnection:
    """One open ConnectSiLAServer stream, with request/response correlation."""

    def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
        self.loop = loop
        self.client: CloudSilaClient | None = None
        self._outgoing: asyncio.Queue[Any] = asyncio.Queue()
        self._pending: dict[str, asyncio.Future] = {}
        self._streams: dict[str, Callable[[Any], None]] = {}
        self._closed = False

    @property
    def closed(self) -> bool:
        return self._closed

    def _new_message(self) -> Any:
        return SiLAClientMessage(requestUUID=str(uuid4()))

    async def async_send(self, msg: Any) -> None:
        if self._closed:
            raise CloudDisconnectedError("Cloud connection is closed")
        await self._outgoing.put(msg)

    async def async_request(self, msg: Any) -> Any:
        """Send a message and await the correlated response."""
        future: asyncio.Future = self.loop.create_future()
        self._pending[msg.requestUUID] = future
        try:
            await self.async_send(msg)
            return await asyncio.wait_for(future, REQUEST_TIMEOUT)
        finally:
            self._pending.pop(msg.requestUUID, None)

    def register_stream(self, request_uuid: str, handler: Callable[[Any], None]) -> None:
        self._streams[request_uuid] = handler

    def unregister_stream(self, request_uuid: str) -> None:
        self._streams.pop(request_uuid, None)

    async def next_outgoing(self) -> Any | None:
        """Next message to yield to the server; None means the stream ends."""
        return await self._outgoing.get()

    @callback
    def dispatch(self, server_msg: Any) -> None:
        """Route an incoming SiLAServerMessage (called on the event loop)."""
        request_uuid = server_msg.requestUUID
        which = server_msg.WhichOneof("message")
        is_error = which in ("commandError", "propertyError", "binaryTransferError")

        if (future := self._pending.get(request_uuid)) is not None:
            if future.done():
                return
            if is_error:
                future.set_exception(
                    CloudCallError(_describe_sila_error(getattr(server_msg, which)))
                )
            else:
                future.set_result(server_msg)
            return

        if (handler := self._streams.get(request_uuid)) is not None:
            if is_error:
                _LOGGER.warning(
                    "SiLA error on cloud subscription %s: %s",
                    request_uuid,
                    _describe_sila_error(getattr(server_msg, which)),
                )
                self.unregister_stream(request_uuid)
                return
            handler(server_msg)
            return

        _LOGGER.debug("Unmatched cloud message for request %s (%s)", request_uuid, which)

    @callback
    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        for future in self._pending.values():
            if not future.done():
                future.set_exception(CloudDisconnectedError("Connection closed"))
        self._pending.clear()
        self._streams.clear()
        # Wake up the response generator so the gRPC handler can return.
        self._outgoing.put_nowait(None)


class CloudSubscription:
    """Duck-types sila2's Subscription for cloud property streams."""

    def __init__(
        self, conn: CloudConnection, request_uuid: str, decode: Callable[[bytes], Any]
    ) -> None:
        self._conn = conn
        self._request_uuid = request_uuid
        self._decode = decode
        self._callbacks: list[Callable[[Any], Any]] = []
        self._cancelled = False

    @property
    def is_cancelled(self) -> bool:
        return self._cancelled or self._conn.closed

    def add_callback(self, callback_fn: Callable[[Any], Any]) -> None:
        self._callbacks.append(callback_fn)

    def _handle_message(self, server_msg: Any) -> None:
        try:
            value = self._decode(server_msg.observablePropertyValue.value)
        except Exception:  # noqa: BLE001 - never kill the dispatch loop on a bad payload
            _LOGGER.exception("Could not decode cloud property value")
            return
        for callback_fn in self._callbacks:
            callback_fn(value)

    def cancel(self) -> None:
        """Cancel the subscription. Blocking, called from the executor."""
        if self.is_cancelled:
            self._cancelled = True
            return
        self._cancelled = True
        asyncio.run_coroutine_threadsafe(
            self._async_cancel(), self._conn.loop
        ).result(REQUEST_TIMEOUT)

    async def _async_cancel(self) -> None:
        self._conn.unregister_stream(self._request_uuid)
        if self._conn.closed:
            return
        msg = SiLAClientMessage(requestUUID=self._request_uuid)
        msg.cancelObservablePropertySubscription.SetInParent()
        await self._conn.async_send(msg)


class CloudObservableCommandInstance:
    """Duck-types sila2's ClientObservableCommandInstance over the stream."""

    def __init__(
        self, conn: CloudConnection, command: "CloudCommand", execution_uuid: str
    ) -> None:
        self._conn = conn
        self._command = command
        self.execution_uuid = execution_uuid
        self.status: CommandExecutionStatus | None = None
        self.progress: float | None = None
        self.estimated_remaining_time: timedelta | None = None
        self._info_uuid: str | None = None

    @property
    def done(self) -> bool:
        return self._conn.closed or self.status in (
            CommandExecutionStatus.finishedSuccessfully,
            CommandExecutionStatus.finishedWithError,
        )

    async def async_subscribe_execution_info(self) -> None:
        msg = self._conn._new_message()
        msg.observableCommandExecutionInfoSubscription.commandExecutionUUID.value = (
            self.execution_uuid
        )
        self._info_uuid = msg.requestUUID
        self._conn.register_stream(msg.requestUUID, self._handle_info)
        await self._conn.async_send(msg)

    def _handle_info(self, server_msg: Any) -> None:
        info = server_msg.observableCommandExecutionInfo.executionInfo
        self.status = CommandExecutionStatus[
            type(info).CommandStatus.Name(info.commandStatus)
        ]
        if info.HasField("progressInfo"):
            self.progress = info.progressInfo.value
        if info.HasField("estimatedRemainingTime"):
            self.estimated_remaining_time = timedelta(
                seconds=info.estimatedRemainingTime.seconds
                + info.estimatedRemainingTime.nanos / 1e9
            )

    def get_responses(self) -> Any:
        """Fetch final responses. Blocking, called from the executor."""
        return asyncio.run_coroutine_threadsafe(
            self.async_get_responses(), self._conn.loop
        ).result(REQUEST_TIMEOUT)

    async def async_get_responses(self) -> Any:
        if self._info_uuid is not None:
            self._conn.unregister_stream(self._info_uuid)
        msg = self._conn._new_message()
        msg.observableCommandGetResponse.commandExecutionUUID.value = self.execution_uuid
        server_msg = await self._conn.async_request(msg)
        return self._command.decode_responses(
            server_msg.observableCommandResponse.response
        )


class CloudProperty:
    """A SiLA property accessed over the cloud stream."""

    def __init__(self, conn: CloudConnection, wrapped: Any, observable: bool) -> None:
        self._conn = conn
        self._wrapped = wrapped
        self._observable = observable

    def _decode(self, payload: bytes) -> Any:
        response_msg = self._wrapped.response_message_type.FromString(payload)
        return self._wrapped.to_native_type(response_msg)

    def get(self) -> Any:
        """Read the current value. Blocking, called from the executor."""
        return asyncio.run_coroutine_threadsafe(
            self.async_get(), self._conn.loop
        ).result(REQUEST_TIMEOUT)

    async def async_get(self) -> Any:
        msg = self._conn._new_message()
        msg.unobservablePropertyRead.fullyQualifiedPropertyId = str(
            self._wrapped.fully_qualified_identifier
        )
        server_msg = await self._conn.async_request(msg)
        return self._decode(server_msg.unobservablePropertyValue.value)

    def subscribe(self) -> CloudSubscription:
        """Open a value subscription. Blocking, called from the executor."""
        return asyncio.run_coroutine_threadsafe(
            self.async_subscribe(), self._conn.loop
        ).result(REQUEST_TIMEOUT)

    async def async_subscribe(self) -> CloudSubscription:
        msg = self._conn._new_message()
        msg.observablePropertySubscription.fullyQualifiedPropertyId = str(
            self._wrapped.fully_qualified_identifier
        )
        subscription = CloudSubscription(self._conn, msg.requestUUID, self._decode)
        self._conn.register_stream(msg.requestUUID, subscription._handle_message)
        await self._conn.async_send(msg)
        return subscription


class CloudCommand:
    """A SiLA command invoked over the cloud stream."""

    def __init__(self, conn: CloudConnection, wrapped: Any, observable: bool) -> None:
        self._conn = conn
        self._wrapped = wrapped
        self._observable = observable

    def _encode_parameters(self, *args: Any, **kwargs: Any) -> bytes:
        param_msg = self._wrapped.parameters.to_message(
            *args,
            **kwargs,
            toplevel_named_data_node=self._wrapped.parameters,
            metadata=None,
        )
        return param_msg.SerializeToString()

    def decode_responses(self, payload: bytes) -> Any:
        responses_type = getattr(
            self._wrapped.parent_feature._pb2_module,
            f"{self._wrapped._identifier}_Responses",
        )
        return self._wrapped.responses.to_native_type(responses_type.FromString(payload))

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        """Execute the command. Blocking, called from the executor."""
        return asyncio.run_coroutine_threadsafe(
            self.async_call(*args, **kwargs), self._conn.loop
        ).result(COMMAND_TIMEOUT)

    async def async_call(self, *args: Any, **kwargs: Any) -> Any:
        payload = await self._conn.loop.run_in_executor(
            None, lambda: self._encode_parameters(*args, **kwargs)
        )
        msg = self._conn._new_message()
        if self._observable:
            msg.observableCommandInitiation.fullyQualifiedCommandId = str(
                self._wrapped.fully_qualified_identifier
            )
            msg.observableCommandInitiation.commandParameter.parameters = payload
            server_msg = await self._conn.async_request(msg)
            confirmation = server_msg.observableCommandConfirmation.commandConfirmation
            instance = CloudObservableCommandInstance(
                self._conn, self, confirmation.commandExecutionUUID.value
            )
            await instance.async_subscribe_execution_info()
            return instance

        msg.unobservableCommandExecution.fullyQualifiedCommandId = str(
            self._wrapped.fully_qualified_identifier
        )
        msg.unobservableCommandExecution.commandParameter.parameters = payload
        server_msg = await self._conn.async_request(msg)
        return self.decode_responses(server_msg.unobservableCommandResponse.response)


class CloudClientFeature:
    """Per-feature accessor: properties and commands as attributes."""

    def __init__(self, conn: CloudConnection, feature: Feature) -> None:
        for prop in feature._unobservable_properties.values():
            setattr(self, prop._identifier, CloudProperty(conn, prop, observable=False))
        for prop in feature._observable_properties.values():
            setattr(self, prop._identifier, CloudProperty(conn, prop, observable=True))
        for command in feature._unobservable_commands.values():
            setattr(
                self, command._identifier, CloudCommand(conn, command, observable=False)
            )
        for command in feature._observable_commands.values():
            setattr(
                self, command._identifier, CloudCommand(conn, command, observable=True)
            )


class CloudSilaClient:
    """SilaClient-compatible facade over one cloud connection."""

    def __init__(self, conn: CloudConnection, hass: HomeAssistant) -> None:
        self._conn = conn
        self._hass = hass
        self._features: dict[str, Feature] = {}
        self._feature_clients: dict[str, CloudClientFeature] = {}
        conn.client = self

    @property
    def connected(self) -> bool:
        return not self._conn.closed

    def __getattr__(self, name: str) -> CloudClientFeature:
        try:
            return self.__dict__["_feature_clients"][name]
        except KeyError:
            raise AttributeError(name) from None

    def _add_feature(self, feature: Feature) -> None:
        self._features[feature._identifier] = feature
        self._feature_clients[feature._identifier] = CloudClientFeature(
            self._conn, feature
        )

    async def async_handshake(self) -> SilaServerInfo:
        """Fetch server identity and all feature definitions over the stream."""
        sila_service = await self._hass.async_add_executor_job(
            Feature, SiLAServiceFeature._feature_definition
        )
        self._add_feature(sila_service)
        service = self._feature_clients["SiLAService"]

        implemented = await service.ImplementedFeatures.async_get()
        for feature_fqid in implemented:
            if str(feature_fqid) == str(sila_service.fully_qualified_identifier):
                continue
            fdl_response = await service.GetFeatureDefinition.async_call(
                FeatureIdentifier=str(feature_fqid)
            )
            feature = await self._hass.async_add_executor_job(
                Feature, fdl_response.FeatureDefinition
            )
            self._add_feature(feature)

        return SilaServerInfo(
            server_uuid=await service.ServerUUID.async_get(),
            server_name=await service.ServerName.async_get(),
            server_type=await service.ServerType.async_get(),
            server_version=await service.ServerVersion.async_get(),
            vendor_url=await service.ServerVendorURL.async_get(),
            server_description=await service.ServerDescription.async_get(),
        )


class SilaCloudGateway:
    """Runs the CloudClientEndpoint and manages connected servers."""

    def __init__(self, hass: HomeAssistant, entry: SilaConfigEntry, port: int) -> None:
        self._hass = hass
        self._entry = entry
        self._port = port
        self._server: grpc.aio.Server | None = None
        self.coordinators: dict[str, SilaCoordinator] = {}

    async def async_start(self) -> None:
        self._server = grpc.aio.server()
        cloud_pb2_grpc.add_CloudClientEndpointServicer_to_server(
            _CloudEndpointServicer(self), self._server
        )
        self._server.add_insecure_port(f"[::]:{self._port}")
        await self._server.start()
        _LOGGER.info("SiLA cloud endpoint listening on port %s", self._port)

    async def async_stop(self) -> None:
        if self._server is not None:
            await self._server.stop(grace=2)
            self._server = None
        for coordinator in self.coordinators.values():
            await coordinator.async_shutdown()

    async def async_handle_connection(self, conn: CloudConnection) -> None:
        """Handshake a newly connected server and (re)wire its coordinator."""
        # Imports here to avoid a cycle (coordinator -> command_runner -> ...).
        from .command_runner import SilaCommandRunner  # noqa: PLC0415
        from .connection import read_server_info  # noqa: F401, PLC0415
        from .coordinator import SilaCoordinator  # noqa: PLC0415

        client = CloudSilaClient(conn, self._hass)
        server_info = await client.async_handshake()
        uuid = server_info.server_uuid

        if (coordinator := self.coordinators.get(uuid)) is not None:
            _LOGGER.info("SiLA server %s reconnected to cloud endpoint", uuid)
            coordinator.client = client
            await coordinator.async_refresh()
            return

        _LOGGER.info(
            "SiLA server %s (%s) connected to cloud endpoint",
            server_info.server_name,
            uuid,
        )
        coordinator = SilaCoordinator(self._hass, self._entry, client, server_info)
        coordinator.command_runner = SilaCommandRunner(
            self._hass, self._entry, coordinator
        )
        await coordinator.async_refresh()
        self.coordinators[uuid] = coordinator
        async_dispatcher_send(
            self._hass, cloud_new_server_signal(self._entry.entry_id), coordinator
        )

    @callback
    def handle_disconnect(self, conn: CloudConnection) -> None:
        client = conn.client
        if client is None:
            return
        for coordinator in self.coordinators.values():
            if coordinator.client is client:
                coordinator.async_set_update_error(
                    CloudDisconnectedError("SiLA server disconnected")
                )


class _CloudEndpointServicer(cloud_pb2_grpc.CloudClientEndpointServicer):
    def __init__(self, gateway: SilaCloudGateway) -> None:
        self._gateway = gateway

    async def ConnectSiLAServer(self, request_iterator: Any, context: Any) -> Any:
        loop = asyncio.get_running_loop()
        conn = CloudConnection(loop)

        async def _read() -> None:
            try:
                async for server_msg in request_iterator:
                    conn.dispatch(server_msg)
            except Exception as err:  # noqa: BLE001 - stream teardown is not exceptional
                _LOGGER.debug("Cloud stream read ended: %s", err)
            finally:
                conn.close()

        read_task = loop.create_task(_read())
        handshake_task = loop.create_task(self._gateway.async_handle_connection(conn))

        try:
            while True:
                msg = await conn.next_outgoing()
                if msg is None:
                    break
                yield msg
        finally:
            conn.close()
            read_task.cancel()
            if not handshake_task.done():
                handshake_task.cancel()
            elif handshake_task.exception() is not None:
                _LOGGER.warning(
                    "Cloud connection handshake failed: %s", handshake_task.exception()
                )
            self._gateway.handle_disconnect(conn)

"""Bridge a locally listening SiLA server to a SiLA cloud endpoint.

Implements the server side of the SiLA 2 v1.1 cloud connectivity protocol:
dials out to a CloudClientEndpoint (e.g. the Home Assistant SiLA gateway),
and proxies every multiplexed request to a normal SiLA server over plain
gRPC. Payloads are passed through as raw bytes, so this works for any
feature set without code generation.

Usage:
    python -m demo_server.cloud_bridge --server 127.0.0.1:50052 --endpoint 127.0.0.1:50051
"""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

import grpc
import sila2.framework.pb2  # noqa: F401 - registers SiLAFramework_pb2 alias
import sila2.framework.pb2.SiLABinaryTransfer_pb2 as _bt_pb2
from sila2.framework.utils import run_protoc

sys.modules.setdefault("SiLABinaryTransfer_pb2", _bt_pb2)

_PROTO = str(
    Path(__file__).parent.parent / "custom_components" / "sila" / "SiLACloudConnector.proto"
)
cloud_pb2, cloud_pb2_grpc = run_protoc(_PROTO)

_LOGGER = logging.getLogger(__name__)

_IDENTITY = lambda b: b  # noqa: E731 - raw-bytes (de)serializer


def _resolve(fully_qualified_id: str) -> tuple[str, str | None]:
    """Map a SiLA fully qualified identifier to (grpc service, member).

    "io.unitelabs/demo/TemperatureController/v1/Property/TargetTemperature"
    -> ("sila2.io.unitelabs.demo.temperaturecontroller.v1.TemperatureController",
        "TargetTemperature")
    """
    parts = fully_qualified_id.split("/")
    originator, category, feature_id, version = parts[:4]
    package = f"sila2.{originator}.{category}.{feature_id.lower()}.{version}"
    member = parts[5] if len(parts) > 5 else None
    return f"{package}.{feature_id}", member


class CloudBridge:
    """One outbound cloud connection proxying one local SiLA server."""

    def __init__(self, server_address: str, endpoint_address: str) -> None:
        self._server_address = server_address
        self._endpoint_address = endpoint_address
        self._outgoing: asyncio.Queue = asyncio.Queue()
        self._local: grpc.aio.Channel | None = None
        # subscription request UUID -> task; execution UUID -> grpc service
        self._streams: dict[str, asyncio.Task] = {}
        self._executions: dict[str, str] = {}

    async def run(self) -> None:
        async with (
            grpc.aio.insecure_channel(self._server_address) as local,
            grpc.aio.insecure_channel(self._endpoint_address) as cloud,
        ):
            self._local = local
            stub = cloud_pb2_grpc.CloudClientEndpointStub(cloud)
            call = stub.ConnectSiLAServer(self._request_stream())
            _LOGGER.info(
                "Bridging SiLA server %s to cloud endpoint %s",
                self._server_address,
                self._endpoint_address,
            )
            try:
                async for client_msg in call:
                    asyncio.ensure_future(self._handle(client_msg))
            finally:
                for task in self._streams.values():
                    task.cancel()

    async def _request_stream(self):
        while True:
            msg = await self._outgoing.get()
            yield msg

    def _unary(self, service: str, method: str):
        return self._local.unary_unary(
            f"/{service}/{method}",
            request_serializer=_IDENTITY,
            response_deserializer=_IDENTITY,
        )

    def _stream(self, service: str, method: str):
        return self._local.unary_stream(
            f"/{service}/{method}",
            request_serializer=_IDENTITY,
            response_deserializer=_IDENTITY,
        )

    async def _reply(self, request_uuid: str) -> cloud_pb2.SiLAServerMessage:
        return cloud_pb2.SiLAServerMessage(requestUUID=request_uuid)

    async def _send(self, msg) -> None:
        await self._outgoing.put(msg)

    async def _send_error(self, request_uuid: str, field: str, err: Exception) -> None:
        out = cloud_pb2.SiLAServerMessage(requestUUID=request_uuid)
        error = getattr(out, field)
        error.undefinedExecutionError.message = str(err)
        await self._send(out)

    async def _handle(self, msg) -> None:
        which = msg.WhichOneof("message")
        try:
            handler = getattr(self, f"_on_{which}", None)
            if handler is None:
                _LOGGER.warning("Unsupported cloud message: %s", which)
                return
            await handler(msg)
        except grpc.aio.AioRpcError as err:
            field = "propertyError" if "Property" in (which or "") else "commandError"
            await self._send_error(msg.requestUUID, field, err)
        except Exception:  # noqa: BLE001
            _LOGGER.exception("Error handling cloud message %s", which)

    async def _on_unobservablePropertyRead(self, msg) -> None:
        service, member = _resolve(msg.unobservablePropertyRead.fullyQualifiedPropertyId)
        response = await self._unary(service, f"Get_{member}")(b"")
        out = cloud_pb2.SiLAServerMessage(requestUUID=msg.requestUUID)
        out.unobservablePropertyValue.value = response
        await self._send(out)

    async def _on_observablePropertySubscription(self, msg) -> None:
        service, member = _resolve(
            msg.observablePropertySubscription.fullyQualifiedPropertyId
        )

        async def _pump() -> None:
            try:
                async for value in self._stream(service, f"Subscribe_{member}")(b""):
                    out = cloud_pb2.SiLAServerMessage(requestUUID=msg.requestUUID)
                    out.observablePropertyValue.value = value
                    await self._send(out)
            except (asyncio.CancelledError, grpc.aio.AioRpcError):
                pass

        self._streams[msg.requestUUID] = asyncio.ensure_future(_pump())

    async def _on_cancelObservablePropertySubscription(self, msg) -> None:
        if (task := self._streams.pop(msg.requestUUID, None)) is not None:
            task.cancel()

    async def _on_unobservableCommandExecution(self, msg) -> None:
        service, member = _resolve(
            msg.unobservableCommandExecution.fullyQualifiedCommandId
        )
        response = await self._unary(service, member)(
            msg.unobservableCommandExecution.commandParameter.parameters
        )
        out = cloud_pb2.SiLAServerMessage(requestUUID=msg.requestUUID)
        out.unobservableCommandResponse.response = response
        await self._send(out)

    async def _on_observableCommandInitiation(self, msg) -> None:
        service, member = _resolve(
            msg.observableCommandInitiation.fullyQualifiedCommandId
        )
        confirmation_bytes = await self._unary(service, member)(
            msg.observableCommandInitiation.commandParameter.parameters
        )
        out = cloud_pb2.SiLAServerMessage(requestUUID=msg.requestUUID)
        out.observableCommandConfirmation.commandConfirmation.ParseFromString(
            confirmation_bytes
        )
        execution_uuid = (
            out.observableCommandConfirmation.commandConfirmation.commandExecutionUUID.value
        )
        self._executions[execution_uuid] = f"{service}/{member}"
        await self._send(out)

    async def _on_observableCommandExecutionInfoSubscription(self, msg) -> None:
        execution_uuid = (
            msg.observableCommandExecutionInfoSubscription.commandExecutionUUID.value
        )
        service, member = self._executions[execution_uuid].split("/")
        uuid_bytes = (
            msg.observableCommandExecutionInfoSubscription.commandExecutionUUID.SerializeToString()
        )

        async def _pump() -> None:
            try:
                async for info in self._stream(service, f"{member}_Info")(uuid_bytes):
                    out = cloud_pb2.SiLAServerMessage(requestUUID=msg.requestUUID)
                    out.observableCommandExecutionInfo.commandExecutionUUID.value = (
                        execution_uuid
                    )
                    out.observableCommandExecutionInfo.executionInfo.ParseFromString(info)
                    await self._send(out)
            except (asyncio.CancelledError, grpc.aio.AioRpcError):
                pass

        self._streams[msg.requestUUID] = asyncio.ensure_future(_pump())

    async def _on_observableCommandGetResponse(self, msg) -> None:
        execution_uuid = msg.observableCommandGetResponse.commandExecutionUUID.value
        service, member = self._executions[execution_uuid].split("/")
        response = await self._unary(service, f"{member}_Result")(
            msg.observableCommandGetResponse.commandExecutionUUID.SerializeToString()
        )
        out = cloud_pb2.SiLAServerMessage(requestUUID=msg.requestUUID)
        out.observableCommandResponse.commandExecutionUUID.value = execution_uuid
        out.observableCommandResponse.response = response
        await self._send(out)


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--server", default="127.0.0.1:50052", help="local SiLA server")
    parser.add_argument(
        "--endpoint", default="127.0.0.1:50051", help="cloud endpoint to dial"
    )
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    asyncio.run(CloudBridge(args.server, args.endpoint).run())


if __name__ == "__main__":
    main()

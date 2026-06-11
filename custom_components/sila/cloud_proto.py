"""Runtime-compiled protobuf modules for the SiLA Cloud Connectivity protocol.

The official SiLACloudConnector.proto (SiLA 2 v1.1, Part B) is vendored next
to this module and compiled with sila2's own protoc machinery, so all
messages share sila2's SiLAFramework descriptor pool.

Importing this module runs protoc (~100 ms, blocking) — Home Assistant
imports integration modules in the executor, so this is safe.
"""

from __future__ import annotations

import sys
from pathlib import Path

import sila2.framework.pb2  # noqa: F401 - registers the SiLAFramework_pb2 alias
import sila2.framework.pb2.SiLABinaryTransfer_pb2 as _binary_transfer_pb2
from sila2.framework.pb2 import SiLAFramework_pb2
from sila2.framework.utils import run_protoc

# The generated module imports these by their bare names.
sys.modules.setdefault("SiLABinaryTransfer_pb2", _binary_transfer_pb2)

_PROTO_FILE = str(Path(__file__).parent / "SiLACloudConnector.proto")

cloud_pb2, cloud_pb2_grpc = run_protoc(_PROTO_FILE)

SiLAClientMessage = cloud_pb2.SiLAClientMessage
SiLAServerMessage = cloud_pb2.SiLAServerMessage
ExecutionInfo = SiLAFramework_pb2.ExecutionInfo
CommandExecutionUUID = SiLAFramework_pb2.CommandExecutionUUID

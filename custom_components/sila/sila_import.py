"""Serialized, idempotent loading of sila2 and the cloud connector proto.

sila2 compiles SiLAFramework.proto at import time and registers the result
in protobuf's process-global descriptor pool. That registration is not
rolled back if the import fails, and Home Assistant imports integration
modules concurrently (import executor + event loop, with importlib
deadlock-breaking) — a race that can execute sila2's module body twice and
poison the pool ("duplicate file name SiLAFramework.proto").

Therefore no module in this integration imports sila2 at module level.
Everything goes through ensure_sila2(), which performs the first (and only
heavy) import exactly once, under a lock, and is safe to call from
executor threads.
"""

from __future__ import annotations

import logging
import threading

_LOGGER = logging.getLogger(__name__)

_LOCK = threading.Lock()
_loaded = False
_first_error: str | None = None


def ensure_sila2() -> None:
    """Import sila2 (triggering its protoc compilation) exactly once. Blocking."""
    global _loaded, _first_error
    if _loaded:
        return
    with _LOCK:
        if _loaded:
            return
        if _first_error is not None:
            # A failed first import leaves SiLAFramework.proto registered in
            # the pool; retrying can only produce the misleading "duplicate
            # file name" error. Surface the original cause instead.
            raise RuntimeError(
                f"sila2 failed to import earlier ({_first_error}); protobuf's "
                "descriptor pool cannot recover — restart Home Assistant"
            )
        try:
            _heal_occupied_pool()
            import sila2.client  # noqa: F401, PLC0415
            import sila2.features.silaservice  # noqa: F401, PLC0415
            import sila2.framework.feature  # noqa: F401, PLC0415
        except BaseException as err:
            _first_error = f"{type(err).__name__}: {err}"
            # Config-entry retries are logged at debug level by HA, which
            # would hide this root cause entirely — log it loudly once.
            _LOGGER.exception("First sila2 import failed; all SiLA "
                              "connections will fail until HA restarts")
            _log_pool_forensics()
            raise

        _pin_framework_pb2()
        _loaded = True


class _PinnedPb2Finder:
    """Meta-path finder that permanently resolves the bare SiLA pb2 names.

    sila2's run_protoc deletes the bare aliases from sys.modules after every
    compile, so each later compile re-executes freshly generated gencode and
    re-registers the protos in the global pool. That re-registration is only
    tolerated while every copy matches what the pool already holds; this
    finder makes every import of the bare names return one pinned module so
    each proto is registered exactly once per process.
    """

    def find_spec(self, fullname: str, path=None, target=None):
        if fullname not in _PINNED:
            return None
        import importlib.util  # noqa: PLC0415

        return importlib.util.spec_from_loader(
            fullname, _PinnedPb2Loader(_PINNED[fullname])
        )


class _PinnedPb2Loader:
    def __init__(self, module) -> None:
        self._module = module

    def create_module(self, spec):
        return self._module

    def exec_module(self, module) -> None:
        return None


_PINNED: dict = {}
_finder_installed = False


def _pin(module_name: str, module) -> None:
    global _finder_installed
    _PINNED.setdefault(module_name, module)
    if not _finder_installed:
        import sys  # noqa: PLC0415

        sys.meta_path.insert(0, _PinnedPb2Finder())
        _finder_installed = True


def _pin_framework_pb2() -> None:
    """Pin the loaded pb2 modules for the process lifetime (see finder)."""
    import sila2.framework.abc.sila_error as sila_error  # noqa: PLC0415
    import sila2.framework.binary_transfer.binary_transfer_error as bt_error  # noqa: PLC0415

    _pin("SiLAFramework_pb2", sila_error._pb2_module)
    _pin("SiLABinaryTransfer_pb2", bt_error.binary_transfer_pb2_module)


def _heal_occupied_pool() -> None:
    """Recover when SiLA protos are already in the descriptor pool.

    Registrations survive rolled-back imports, so a torn earlier import (or
    any other registrar) leaves the pool occupied while sila2 itself is
    absent from sys.modules. Importing sila2 would then execute fresh
    gencode and die with "duplicate file name". Instead, rebuild a module
    from the pool's existing registration and alias it — run_protoc's
    import_module() then returns it without executing the generated file.
    """
    import sys  # noqa: PLC0415
    import types  # noqa: PLC0415

    from google.protobuf import descriptor_pool, message_factory  # noqa: PLC0415
    from google.protobuf.internal.enum_type_wrapper import (  # noqa: PLC0415
        EnumTypeWrapper,
    )

    for proto_name, module_name in (
        ("SiLAFramework.proto", "SiLAFramework_pb2"),
        ("SiLABinaryTransfer.proto", "SiLABinaryTransfer_pb2"),
    ):
        if module_name in sys.modules:
            continue
        try:
            file_desc = descriptor_pool.Default().FindFileByName(proto_name)
        except KeyError:
            continue

        _LOGGER.warning(
            "%s is already registered in the descriptor pool (torn earlier "
            "import?); reusing the existing registration",
            proto_name,
        )
        module = types.ModuleType(module_name)
        module.DESCRIPTOR = file_desc
        for name, message_desc in file_desc.message_types_by_name.items():
            setattr(module, name, message_factory.GetMessageClass(message_desc))
        for name, enum_desc in file_desc.enum_types_by_name.items():
            setattr(module, name, EnumTypeWrapper(enum_desc))
        # Seed both registrars: run_protoc's bare-name import_module (which
        # deletes its import afterwards — the pin makes it permanent), and
        # the pregenerated copy that sila2.framework.utils pulls in through
        # sila2.framework.pb2.custom_protocols at import time.
        _pin(module_name, module)
        sys.modules[module_name] = module
        sys.modules[f"sila2.framework.pb2.{module_name}"] = module


def _log_pool_forensics() -> None:
    """Identify what already occupies SiLAFramework.proto in the pool.

    The descriptor pool is process-global and registrations survive
    rolled-back imports, so when the sila2 import dies with "duplicate file
    name" the interesting question is who registered the proto first and
    with which content.
    """
    try:
        import sys  # noqa: PLC0415

        from google.protobuf import descriptor_pool, descriptor_pb2  # noqa: PLC0415

        sila_modules = sorted(
            name for name in sys.modules
            if "sila" in name.lower() or "SiLA" in name
        )
        _LOGGER.error("sila-related modules in sys.modules: %s", sila_modules)

        pool = descriptor_pool.Default()
        file_desc = pool.FindFileByName("SiLAFramework.proto")
        proto = descriptor_pb2.FileDescriptorProto()
        file_desc.CopyToProto(proto)
        serialized = proto.SerializeToString()
        _LOGGER.error(
            "Pool already holds SiLAFramework.proto: %d bytes, "
            "messages=%s",
            len(serialized),
            sorted(m.name for m in file_desc.message_types_by_name.values())[:10],
        )
    except Exception:  # noqa: BLE001 - forensics must never mask the original error
        _LOGGER.exception("Pool forensics failed")

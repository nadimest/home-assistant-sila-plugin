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
            import sila2.client  # noqa: F401, PLC0415
            import sila2.features.silaservice  # noqa: F401, PLC0415
            import sila2.framework.feature  # noqa: F401, PLC0415
        except BaseException as err:
            _first_error = f"{type(err).__name__}: {err}"
            # Config-entry retries are logged at debug level by HA, which
            # would hide this root cause entirely — log it loudly once.
            _LOGGER.exception("First sila2 import failed; all SiLA "
                              "connections will fail until HA restarts")
            raise

        _loaded = True

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

import threading

_LOCK = threading.Lock()
_loaded = False


def ensure_sila2() -> None:
    """Import sila2 (triggering its protoc compilation) exactly once. Blocking."""
    global _loaded
    if _loaded:
        return
    with _LOCK:
        if _loaded:
            return
        import sila2.client  # noqa: F401, PLC0415
        import sila2.features.silaservice  # noqa: F401, PLC0415
        import sila2.framework.feature  # noqa: F401, PLC0415

        _loaded = True

"""Where a proved app is actually put, and which of the two ways is in use.

`local` is bare processes on this machine; `apprt` is two containers per
generation. The pipeline above them imports `serving` and never either one by
name, which is the only way the claim that it cannot tell them apart stays true
— a module that reached for `local.listening` would be a module that could not
run anywhere else.

The choice is an environment variable rather than a config field because the
worker makes it at import time, before it has read anything. `apprt` is the
default, because the whole pipeline suite is green on it and a default that is
not the path which gets deployed is a default that proves nothing. `local`
survives only until the same ordered run passes on the Linux host; the switch
goes with it, so that the production path is not the untested one.
"""

from __future__ import annotations

import os
from importlib import import_module
from types import ModuleType

from worker.runtimes.base import NO_ENTRYPOINT, Running, StartupFailed, Watcher

RUNTIMES = ("local", "apprt")
DEFAULT_RUNTIME = "apprt"

__all__ = [
    "NO_ENTRYPOINT",
    "RUNTIMES",
    "Running",
    "StartupFailed",
    "Watcher",
    "serving",
]


def _chosen() -> ModuleType:
    name = os.environ.get("VD_RUNTIME") or DEFAULT_RUNTIME
    if name not in RUNTIMES:
        raise ValueError(
            f"VD_RUNTIME is {name!r}, which is not one of {', '.join(RUNTIMES)}."
        )
    return import_module(f"worker.runtimes.{name}")


# Bound once, eagerly, so that a typo is an error on startup rather than a
# surprise on the first deploy — and so that a test which replaces a function on
# it replaces the one the pipeline actually calls.
serving = _chosen()

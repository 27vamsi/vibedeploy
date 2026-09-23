"""ASGI entrypoint. `uvicorn sidecar.server:app --host 0.0.0.0 --port 8080`.

Config is read once, at import, so a missing key stops the container from
starting rather than surfacing as a confusing 500 on the first login.
"""

from __future__ import annotations

from sidecar.app import create_app
from sidecar.config import SidecarConfig

app = create_app(SidecarConfig.from_env())

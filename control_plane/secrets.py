"""Where credentials live, which is never the control plane's database.

Readme.md section 18.1 gives every app four secrets under `vd/apps/<app_id>/`:
`runtime`, `sidecar`, `migrator` and `agent`. Splitting them that way is what
lets section 21 give the gateway read access to `agent-*` and nothing else.

Locally that is a directory of files instead of Secrets Manager. The names and
the shape are identical, so M8 swaps the backend and nothing else.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

RUNTIME = "runtime"
SIDECAR = "sidecar"
MIGRATOR = "migrator"
AGENT = "agent"


class SecretNotFound(KeyError):
    pass


def secret_name(app_id: str, kind: str) -> str:
    return f"vd/apps/{app_id}/{kind}"


@dataclass(frozen=True)
class SecretStore:
    root: Path

    def _path(self, name: str) -> Path:
        # The name is built by `secret_name` from an app id that
        # kernel.render.validate_app_id has already restricted to
        # `app_[a-z0-9_]`, so no component can climb out of the root.
        return self.root.joinpath(*name.split("/")).with_suffix(".json")

    def put(self, name: str, value: Mapping[str, Any]) -> None:
        path = self._path(name)
        path.parent.mkdir(parents=True, exist_ok=True)
        # Written 0600 before anything is in it, so the window where a secret
        # exists at default permissions never opens.
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(dict(value), handle)

    def get(self, name: str) -> dict[str, Any]:
        path = self._path(name)
        if not path.exists():
            raise SecretNotFound(name)
        return json.loads(path.read_text(encoding="utf-8"))

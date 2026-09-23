"""Who is allowed in. Readme.md section 16.

The sidecar does not store users, hash passwords or own a database. It asks the
control plane, which does the argon2 check, and believes the answer. That is on
purpose: the sidecar sits in front of untrusted app code on a public subnet, so
the less it knows the less a hole in it is worth.

Two calls:
  - `login`  POST /internal/apps/{app_id}/login  -> sub, role, session_version
  - `session_version`  GET .../users/{sub}/session-version, cached for 60s

The cache is what makes a removed user stop working within a minute without a
control plane round trip on every single request.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

import httpx

API_KEY_HEADER = "X-VD-Sidecar-Key"

# A login must not be able to hang the gate, and a control plane that is down
# must produce a refusal rather than a hung browser tab.
TIMEOUT = httpx.Timeout(5.0)


@dataclass(frozen=True)
class Principal:
    """A person, as the control plane describes them."""

    sub: str
    role: str
    session_version: int


class Directory:
    def __init__(self, base_url: str, api_key: str, app_id: str, *, ttl: int = 60):
        self._base = base_url.rstrip("/")
        self._api_key = api_key
        self._app_id = app_id
        self._ttl = ttl
        self._client = httpx.AsyncClient(timeout=TIMEOUT)
        self._versions: dict[str, tuple[int, float]] = {}

    async def aclose(self) -> None:
        await self._client.aclose()

    @property
    def _headers(self) -> dict[str, str]:
        return {API_KEY_HEADER: self._api_key}

    async def login(self, email: str, password: str) -> Principal | None:
        """None for wrong credentials, unknown user, or a control plane that
        did not answer. The browser is told the same thing in every case."""
        try:
            response = await self._client.post(
                f"{self._base}/internal/apps/{self._app_id}/login",
                json={"email": email, "password": password},
                headers=self._headers,
            )
        except httpx.HTTPError:
            return None
        if response.status_code != 200:
            return None
        try:
            body = response.json()
        except ValueError:
            return None

        sub, role = body.get("sub"), body.get("role")
        version = body.get("session_version")
        if not isinstance(sub, str) or not sub:
            return None
        if not isinstance(role, str):
            return None
        if not isinstance(version, int) or isinstance(version, bool):
            return None

        self._versions[sub] = (version, time.monotonic())
        return Principal(sub=sub, role=role, session_version=version)

    async def session_version(self, sub: str) -> int | None:
        """The current version for a person, cached for `ttl` seconds.

        None means we could not find out, and a session we cannot confirm is
        not a session. Section 3: unsure means deny.
        """
        cached = self._versions.get(sub)
        if cached is not None and time.monotonic() - cached[1] < self._ttl:
            return cached[0]

        try:
            response = await self._client.get(
                f"{self._base}/internal/apps/{self._app_id}"
                f"/users/{sub}/session-version",
                headers=self._headers,
            )
        except httpx.HTTPError:
            return None
        if response.status_code != 200:
            return None
        try:
            version = response.json().get("session_version")
        except ValueError:
            return None
        if not isinstance(version, int) or isinstance(version, bool):
            return None

        self._versions[sub] = (version, time.monotonic())
        return version

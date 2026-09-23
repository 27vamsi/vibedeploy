"""The gate in-process, with a directory that never leaves the test.

The end-to-end suite runs the real thing against real processes. This one exists
for the parts that must be checked at their real settings — the login rate
limits in particular, which a suite sharing one loopback address and two seeded
people cannot exercise honestly.

No upstream is configured. Every test here stops at the gate, which is the
point: if a request reached the upstream the client would fail to connect, so
"the app was never reached" is not something these tests have to assert.
"""

from __future__ import annotations

import httpx
import pytest

from sidecar.app import create_app
from sidecar.config import SidecarConfig
from sidecar.directory import Principal

KEY = "a1" * 32
SESSION_KEY = "b2" * 32
APP_ID = "app_test"

PASSWORD = "correct horse"
PEOPLE = {"alice@example.com": Principal("user-a", "member", 1)}


class FakeDirectory:
    """The control plane's answers, without the control plane."""

    def __init__(self):
        self.versions = {p.sub: p.session_version for p in PEOPLE.values()}
        self.logins = 0
        self.reachable = True

    async def login(self, email: str, password: str) -> Principal | None:
        self.logins += 1
        if not self.reachable:
            return None
        person = PEOPLE.get(email)
        if person is None or password != PASSWORD:
            return None
        return Principal(person.sub, person.role, self.versions[person.sub])

    async def session_version(self, sub: str) -> int | None:
        if not self.reachable:
            return None
        return self.versions.get(sub)

    async def aclose(self) -> None:
        return None


def config(**overrides) -> SidecarConfig:
    settings = {
        "app_id": APP_ID,
        "identity_key": KEY.encode(),
        "session_key": SESSION_KEY.encode(),
        "control_plane_url": "http://control-plane.invalid",
        "sidecar_api_key": "sidecar-key",
        "upstream": "http://upstream.invalid",
        "cookie_secure": False,
    }
    settings.update(overrides)
    return SidecarConfig(**settings)


@pytest.fixture
def directory() -> FakeDirectory:
    return FakeDirectory()


@pytest.fixture
def gate(directory):
    """A factory, so a test can ask for its own limits."""

    def build(**overrides) -> httpx.AsyncClient:
        app = create_app(config(**overrides), directory=directory)
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://gate.test",
            timeout=10,
        )

    return build


@pytest.fixture
async def browser(gate):
    async with gate() as client:
        yield client

"""The MCP front door. Readme.md section 21, section 8 M13.

These tests go over the wire. The app is driven as an ASGI application with
hand-written JSON-RPC rather than through the SDK's client, because the thing
worth pinning down is the contract section 21 states — bearer key, a session on
initialize, a filtered `tools/list`, and `{"status":"pending_approval",
"action_id":...}` from a write — and an SDK client would hide all four behind
its own helpers.

The claim being tested is "MCP is just another front door". So every test here
has a counterpart somewhere in `test_gateway_core.py` or `test_approvals.py`
that asserts the same thing through `Gateway` directly, and the one that matters
most is the last: a call that arrived over MCP lands in the same audit log and
is counted by the same evidence report, because it went down the same pipeline.
"""

from __future__ import annotations

import asyncio
import json
import uuid

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import select

from control_plane import service
from control_plane.models import Action, ActionStatus
from gateway import mcp_server
from tests.gateway.conftest import wire

CONTROL_URL = (
    "postgresql+asyncpg://vd_admin:vd_local_password@127.0.0.1:55432"
    "/vibedeploy_mcp_test"
)

POLICY = """
agent: alices-helper
acts_for: alice@example.com
session_ttl_minutes: 30
rate_limit_per_minute: 240
postgres:
  default: {read: auto}
  tables:
    customers: {read: auto, update: approve}
    orders: {read: auto}
    audit_log: {}
"""

PROTOCOL = "2025-06-18"
JSON_RPC = {
    "accept": "application/json, text/event-stream",
    "content-type": "application/json",
}


# ---------------------------------------------------------------------------
# Driving the app
# ---------------------------------------------------------------------------


class Lifespan:
    """The Streamable HTTP session manager is started by the ASGI lifespan, so
    something has to run it. `httpx.ASGITransport` does not."""

    def __init__(self, app):
        self._app = app

    async def __aenter__(self):
        self._inbox: asyncio.Queue = asyncio.Queue()
        self._outbox: asyncio.Queue = asyncio.Queue()
        self._task = asyncio.create_task(
            self._app(
                {"type": "lifespan", "asgi": {"version": "3.0", "spec_version": "2.0"}},
                self._inbox.get,
                self._outbox.put,
            )
        )
        await self._inbox.put({"type": "lifespan.startup"})
        started = await asyncio.wait_for(self._outbox.get(), 10)
        assert started["type"] == "lifespan.startup.complete", started
        return self

    async def __aexit__(self, *exc):
        await self._inbox.put({"type": "lifespan.shutdown"})
        try:
            await asyncio.wait_for(self._outbox.get(), 10)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            pass
        self._task.cancel()


class Agent:
    """One MCP client, holding one bearer key and one transport session."""

    def __init__(self, http: httpx.AsyncClient, key: str):
        self._http = http
        self._key = key
        self.transport_id: str | None = None
        self._next = 0

    def _headers(self, **extra) -> dict[str, str]:
        headers = {**JSON_RPC, "authorization": f"Bearer {self._key}"}
        if self.transport_id:
            headers["mcp-session-id"] = self.transport_id
            headers["mcp-protocol-version"] = PROTOCOL
        return {**headers, **extra}

    async def post(self, body, **extra) -> httpx.Response:
        return await self._http.post(
            mcp_server.PATH, json=body, headers=self._headers(**extra)
        )

    async def initialize(self) -> httpx.Response:
        self._next += 1
        response = await self.post(
            {
                "jsonrpc": "2.0",
                "id": self._next,
                "method": "initialize",
                "params": {
                    "protocolVersion": PROTOCOL,
                    "capabilities": {},
                    "clientInfo": {"name": "test", "version": "1"},
                },
            }
        )
        if response.status_code == 200:
            self.transport_id = response.headers["mcp-session-id"]
            await self.post({"jsonrpc": "2.0", "method": "notifications/initialized"})
        return response

    async def request(self, method: str, params=None) -> dict:
        self._next += 1
        response = await self.post(
            {
                "jsonrpc": "2.0",
                "id": self._next,
                "method": method,
                "params": params or {},
            }
        )
        assert response.status_code == 200, response.text
        return response.json()

    async def tools(self) -> dict[str, dict]:
        listed = (await self.request("tools/list"))["result"]["tools"]
        return {tool["name"]: tool for tool in listed}

    async def call(self, tool: str, **args) -> dict:
        """The tool's own answer, unwrapped from the JSON-RPC envelope."""
        result = (await self.request("tools/call", {"name": tool, "arguments": args}))[
            "result"
        ]
        return {
            "is_error": result.get("isError", False),
            "body": json.loads(result["content"][0]["text"]),
        }


@pytest_asyncio.fixture(scope="module")
async def wired(gateway_admin, tmp_path_factory):
    async with wire(
        gateway_admin,
        tmp_path_factory.mktemp("mcp"),
        control_url=CONTROL_URL,
        policy=POLICY,
    ) as wired:
        yield wired


@pytest_asyncio.fixture(scope="module")
async def served(wired):
    app = mcp_server.build(wired.gateway)
    async with Lifespan(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8000"
        ) as http:
            yield http


@pytest_asyncio.fixture
async def agent(served, wired):
    client = Agent(served, wired.key)
    assert (await client.initialize()).status_code == 200
    return client


# ---------------------------------------------------------------------------
# The front door
# ---------------------------------------------------------------------------


async def test_a_request_without_a_key_never_reaches_the_protocol(served):
    response = await served.post(
        mcp_server.PATH,
        json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        headers=JSON_RPC,
    )
    assert response.status_code == 401
    assert "www-authenticate" in response.headers
    # No session was begun: the protocol layer never saw the request, so there
    # is nothing for an unauthenticated client to go on with.
    assert "mcp-session-id" not in response.headers


async def test_a_key_that_does_not_work_is_refused_at_the_door(served):
    assert (await Agent(served, "vd_agent_nonsense").initialize()).status_code == 401


async def test_initialize_hands_back_a_session(agent):
    assert agent.transport_id


async def test_somebody_elses_session_id_is_not_adopted(served, agent):
    """The transport session id is not a credential, and is not treated as
    one: a request carrying it with a different key is refused rather than
    answered as the agent that opened it."""
    impostor = Agent(served, "vd_agent_nonsense")
    impostor.transport_id = agent.transport_id
    response = await impostor.post({"jsonrpc": "2.0", "id": 9, "method": "tools/list"})
    assert response.status_code == 404


# ---------------------------------------------------------------------------
# tools/list
# ---------------------------------------------------------------------------


async def test_only_the_tools_the_policy_leaves_open_are_listed(agent):
    """Section 21: "`tools/list` returns only non-forbidden tools". There is no
    filtering in the MCP layer at all — the list comes from the policy — which
    is why a forbidden tool cannot be left in by forgetting something here."""
    listed = await agent.tools()
    assert {"db_query", "db_update", mcp_server.STATUS_TOOL} == set(listed)
    assert "db_delete" not in listed
    assert "db_create" not in listed


async def test_a_tool_that_is_not_listed_is_not_callable(agent, wired):
    """Not listed is not the same as not callable, so both are tested. An agent
    that has read section 19 and guesses the name gets nowhere."""
    answer = await agent.call("db_delete", table="customers", filters=[])
    assert answer["is_error"]
    assert answer["body"]["status"] == ActionStatus.denied.value


async def test_every_listed_tool_says_what_it_takes(agent):
    for name, tool in (await agent.tools()).items():
        assert tool["inputSchema"]["type"] == "object", name
        assert tool["description"], name


# ---------------------------------------------------------------------------
# tools/call
# ---------------------------------------------------------------------------


async def test_a_read_returns_exactly_the_acts_for_persons_rows(agent, wired):
    answer = await agent.call("db_query", table="customers")
    assert not answer["is_error"]
    expected = {str(key[0]) for key in wired.app.plan.owned_by("customers", "A")}
    assert {row["id"] for row in answer["body"]["rows"]} == expected
    assert expected


async def test_a_write_that_needs_a_person_comes_back_parked(agent, wired):
    """Section 21: a write tool returns either the result or
    `{"status":"pending_approval","action_id":"..."}`. And nothing has happened
    yet: the row still says what it said."""
    mine = wired.app.plan.one_owned_by("customers", "A")
    was = await wired.name_of(mine.key[0])

    answer = await agent.call(
        "db_update",
        table="customers",
        filters=[{"column": "id", "op": "=", "value": str(mine.key[0])}],
        values={"name": "asked for over MCP"},
    )

    body = answer["body"]
    assert body["status"] == "pending_approval"
    assert uuid.UUID(body["action_id"])
    assert await wired.name_of(mine.key[0]) == was


async def test_an_agent_can_wait_for_the_decision(agent, wired):
    """Section 21's extra tool. It is the only reason this module adds a tool
    at all: over REST an agent would poll the action's own URL, and there is no
    GET here to poll."""
    mine = wired.app.plan.one_owned_by("customers", "A")
    parked = (
        await agent.call(
            "db_update",
            table="customers",
            filters=[{"column": "id", "op": "=", "value": str(mine.key[0])}],
            values={"name": "waiting for admin"},
        )
    )["body"]
    action_id = parked["action_id"]

    waiting = await agent.call(mcp_server.STATUS_TOOL, action_id=action_id)
    assert waiting["body"]["status"] == ActionStatus.pending_approval.value
    assert waiting["body"]["waiting_until"]

    outcome = await wired.gateway.approve(action_id, approver=wired.admin)
    assert outcome.ok, outcome.reason

    decided = await agent.call(mcp_server.STATUS_TOOL, action_id=action_id)
    assert decided["body"]["status"] == ActionStatus.verified.value
    assert await wired.name_of(mine.key[0]) == "waiting for admin"


async def test_an_action_nobody_asked_for_is_not_there_to_read(agent):
    answer = await agent.call(mcp_server.STATUS_TOOL, action_id=str(uuid.uuid4()))
    assert answer["is_error"]
    assert answer["body"]["reason"] == "There is no such action."


async def test_a_nonsense_action_id_is_the_same_answer(agent):
    """Not a different error. "There is no such action" is what an id that
    could never exist gets too, so guessing tells an agent nothing."""
    answer = await agent.call(mcp_server.STATUS_TOOL, action_id="not-a-uuid")
    assert answer["is_error"]
    assert answer["body"]["reason"] == "There is no such action."


# ---------------------------------------------------------------------------
# Fail closed. The switches apply to this door too.
# ---------------------------------------------------------------------------


async def test_the_kill_switch_shuts_the_door(served, wired):
    async with wired.sessions() as session, session.begin():
        await service.set_kill_switch(session, on=True)
    try:
        assert (await Agent(served, wired.key).initialize()).status_code == 401
    finally:
        async with wired.sessions() as session, session.begin():
            await service.set_kill_switch(session, on=False)


async def test_the_kill_switch_stops_an_open_session_too(agent, wired):
    """Thrown after initialize, so there is a live transport session and a live
    gateway session. Neither is cached in a way that could still answer."""
    async with wired.sessions() as session, session.begin():
        await service.set_kill_switch(session, on=True)
    try:
        answer = await agent.call("db_query", table="customers")
        assert answer["is_error"]
        assert answer["body"]["rows"] == []
    finally:
        async with wired.sessions() as session, session.begin():
            await service.set_kill_switch(session, on=False)


async def test_a_table_the_policy_does_not_name_is_denied(agent):
    answer = await agent.call("db_query", table="no_such_table")
    assert answer["is_error"]
    assert answer["body"]["status"] == ActionStatus.denied.value


# ---------------------------------------------------------------------------
# The same pipeline, not a second one
# ---------------------------------------------------------------------------


async def test_a_call_over_mcp_is_recorded_like_any_other(agent, wired):
    before = await wired.gateway.evidence(wired.app_uuid)
    await agent.call("db_query", table="orders")
    after = await wired.gateway.evidence(wired.app_uuid)

    assert after.total == before.total + 1
    assert after.verified == before.verified + 1
    assert after.chain.intact


async def test_the_action_row_names_the_agent_and_the_person(agent, wired):
    await agent.call("db_query", table="customers")
    async with wired.sessions() as session:
        latest = (
            await session.execute(
                select(Action).order_by(Action.created_at.desc()).limit(1)
            )
        ).scalar_one()
    assert latest.agent_id == wired.agent_id
    assert latest.acts_for == wired.alice
    assert latest.app_id == wired.app_uuid


@pytest.mark.parametrize("tool", ["db_query", "db_update"])
async def test_the_listed_tools_are_the_connectors_own(agent, wired, tool):
    """The schemas an MCP client is shown are the connector's, so a client
    that validates against them is validating against what the pipeline will
    actually accept."""
    listed = (await agent.tools())[tool]
    connector_said = {
        spec["name"]: spec for spec in await wired.gateway.tools(await wired.session_id())
    }[tool]
    assert listed["inputSchema"] == connector_said["input_schema"]
    assert listed["description"] == connector_said["description"]

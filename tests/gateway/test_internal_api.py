"""The console's door into the gateway. Readme.md 19.4, 19.5, 19.7, 23.

This exists because of one rule in CLAUDE.md: "The gateway may read only
`vd/apps/*/agent-*` secrets." Approving an action redoes the dry run, which
needs that secret, so the console cannot do it — it asks the gateway. The tests
here are therefore about two things and nothing else:

  - the door is shut without the right key, and shut when no key was set at
    all, which is the dangerous case;
  - what comes through it is the pipeline's own answer, not a second
    implementation of the approval rules. Every assertion about *whether*
    something may be approved has its real home in `test_approvals.py`; here
    the question is only whether the HTTP layer faithfully carries it.
"""

from __future__ import annotations

import uuid

import httpx
import pytest_asyncio

from control_plane.config import DEFAULT_APP_DSN, ControlPlaneConfig
from control_plane.models import ActionStatus
from gateway import app as gateway_app
from tests.gateway.conftest import wire

CONTROL_URL = (
    "postgresql+asyncpg://vd_admin:vd_local_password@127.0.0.1:55432"
    "/vibedeploy_internal_test"
)

CONSOLE_KEY = "a-console-key-nobody-else-has"

POLICY = """
agent: alices-helper
acts_for: alice@example.com
session_ttl_minutes: 30
rate_limit_per_minute: 240
postgres:
  default: {read: auto}
  tables:
    customers: {read: auto, update: approve}
"""


@pytest_asyncio.fixture(scope="module")
async def wired(gateway_admin, tmp_path_factory):
    async with wire(
        gateway_admin,
        tmp_path_factory.mktemp("internal"),
        control_url=CONTROL_URL,
        policy=POLICY,
    ) as wired:
        yield wired


def _console(wired, *, key: str | None) -> httpx.AsyncClient:
    """The console, as the gateway sees it: one key and no database of its own."""
    config = ControlPlaneConfig(
        database_url=CONTROL_URL,
        app_admin_dsn=DEFAULT_APP_DSN,
        sidecar_api_key="unused here",
        state_root=wired.gateway._store.root.parent,
        public_url="http://127.0.0.1:0",
        gateway_api_key=key or "",
    )
    internal = gateway_app.internal(wired.gateway, config)
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=internal),
        base_url="http://gateway",
        headers={"x-vd-console-key": CONSOLE_KEY} if key else {},
    )


@pytest_asyncio.fixture
async def console(wired):
    async with _console(wired, key=CONSOLE_KEY) as client:
        yield client


async def _park(wired, name: str) -> tuple[str, str]:
    """One action waiting for a person, and the row it is aimed at."""
    mine = wired.app.plan.one_owned_by("customers", "A")
    outcome = await wired.gateway.call(
        await wired.session_id(),
        "db_update",
        {
            "table": "customers",
            "filters": [{"column": "id", "op": "=", "value": str(mine.key[0])}],
            "values": {"name": name},
        },
    )
    assert outcome.status == ActionStatus.pending_approval.value, outcome.reason
    return str(outcome.action_id), mine.key[0]


# ---------------------------------------------------------------------------
# The door
# ---------------------------------------------------------------------------


async def test_no_key_is_no_entry(wired):
    async with _console(wired, key=None) as anonymous:
        for path in (f"/apps/{wired.app_uuid}/pending", "/audit"):
            assert (await anonymous.get(path)).status_code == 403


async def test_the_wrong_key_is_no_entry(wired):
    async with _console(wired, key=CONSOLE_KEY) as impostor:
        impostor.headers["x-vd-console-key"] = "nearly-the-console-key"
        assert (await impostor.get("/audit")).status_code == 403


async def test_a_gateway_that_was_never_given_a_key_admits_nobody(wired):
    """Fail closed, and the case most likely to be got wrong: with no key
    configured, an empty header must not match an empty setting."""
    config = ControlPlaneConfig(
        database_url=CONTROL_URL,
        app_admin_dsn=DEFAULT_APP_DSN,
        sidecar_api_key="unused here",
        state_root=wired.gateway._store.root.parent,
        public_url="http://127.0.0.1:0",
        gateway_api_key="",
    )
    unkeyed = gateway_app.internal(wired.gateway, config)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=unkeyed), base_url="http://gateway"
    ) as client:
        assert (await client.get("/audit", headers={"x-vd-console-key": ""})).status_code == 403
        assert (await client.get("/audit")).status_code == 403


# ---------------------------------------------------------------------------
# 19.4 through the door
# ---------------------------------------------------------------------------


async def test_what_is_waiting_comes_with_what_19_4_says_to_show(console, wired):
    action_id, _ = await _park(wired, "shown to an approver")
    waiting = {row["action_id"]: row for row in (await console.get(f"/apps/{wired.app_uuid}/pending")).json()}

    assert action_id in waiting
    row = waiting[action_id]
    # 19.4: agent, acts-for, tool, diff, risk, reason.
    assert row["agent_id"] == str(wired.agent_id)
    assert row["acts_for"] == str(wired.alice)
    assert row["tool"] == "db_update"
    assert row["risk"]
    assert row["diff"]
    assert row["diff_hash"]
    assert row["expires_at"]


async def test_one_click_approves_one_action(console, wired):
    action_id, row_id = await _park(wired, "approved from the console")
    answer = await console.post(
        f"/actions/{action_id}/approve", json={"approver": wired.admin}
    )

    assert answer.status_code == 200
    assert answer.json()["status"] == ActionStatus.verified.value
    assert await wired.name_of(row_id) == "approved from the console"


async def test_rejecting_changes_nothing(console, wired):
    action_id, row_id = await _park(wired, "rejected from the console")
    was = await wired.name_of(row_id)

    answer = await console.post(
        f"/actions/{action_id}/reject", json={"approver": wired.admin}
    )
    assert answer.json()["status"] == ActionStatus.rejected.value
    assert await wired.name_of(row_id) == was


async def test_the_undo_button_puts_the_row_back(console, wired):
    action_id, row_id = await _park(wired, "done then undone")
    was_before_any_of_this = await wired.name_of(row_id)
    await console.post(f"/actions/{action_id}/approve", json={"approver": wired.admin})
    assert await wired.name_of(row_id) == "done then undone"

    answer = await console.post(
        f"/actions/{action_id}/undo", json={"approver": wired.admin}
    )
    assert answer.json()["status"] == ActionStatus.undone.value
    assert await wired.name_of(row_id) == was_before_any_of_this


async def test_the_console_cannot_approve_what_the_pipeline_refuses(console, wired):
    """The rules are not re-checked here, so the test is that they are not
    *skipped* here either: alice is a member, not an admin, and 19.4 says the
    approver must be an app admin."""
    action_id, row_id = await _park(wired, "approved by the wrong person")
    was = await wired.name_of(row_id)

    answer = await console.post(
        f"/actions/{action_id}/approve", json={"approver": "alice@example.com"}
    )
    assert answer.json()["ok"] is False
    assert await wired.name_of(row_id) == was


async def test_an_action_that_does_not_exist_is_not_approvable(console, wired):
    answer = await console.post(
        f"/actions/{uuid.uuid4()}/approve", json={"approver": wired.admin}
    )
    assert answer.json()["status"] == ActionStatus.denied.value


# ---------------------------------------------------------------------------
# 19.6, 19.7, 23
# ---------------------------------------------------------------------------


async def test_the_audit_chain_can_be_checked_from_the_console(console):
    answer = (await console.get("/audit")).json()
    assert answer["intact"]
    assert answer["entries"] > 0


async def test_the_evidence_report_comes_with_its_sentence(console, wired):
    answer = (await console.get(f"/apps/{wired.app_uuid}/evidence")).json()
    assert answer["total"] > 0
    # Demo step 11 is a sentence, not a table, so the report hands one over
    # ready to read out.
    assert "actions" in answer["summary"]
    assert "Audit chain intact." in answer["summary"]


async def test_the_activity_page_says_what_happened_and_why(console, wired):
    action_id, _ = await _park(wired, "listed on the activity page")
    rows = (await console.get(f"/apps/{wired.app_uuid}/activity")).json()
    listed = {row["action_id"]: row for row in rows}

    assert action_id in listed
    assert listed[action_id]["status"] == ActionStatus.pending_approval.value
    assert listed[action_id]["decision"] == "approve"
    # Nothing has run, so there is nothing to undo.
    assert listed[action_id]["undoable"] is False


async def test_both_doors_are_open_on_the_one_process(wired):
    """`gateway.app.build` is what uvicorn is pointed at, so the thing worth
    testing is the composition: the agent's door needs a bearer key and the
    console's needs its own, and neither one's guard is in front of the other.
    """
    from tests.gateway.test_mcp_server import Lifespan

    config = ControlPlaneConfig(
        database_url=CONTROL_URL,
        app_admin_dsn=DEFAULT_APP_DSN,
        sidecar_api_key="unused here",
        state_root=wired.gateway._store.root.parent,
        public_url="http://127.0.0.1:0",
        gateway_api_key=CONSOLE_KEY,
    )
    both = gateway_app.build(config)
    async with Lifespan(both):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=both), base_url="http://127.0.0.1:8200"
        ) as client:
            # The console's key does not open the agent's door...
            assert (
                await client.post(
                    "/mcp", json={}, headers={"x-vd-console-key": CONSOLE_KEY}
                )
            ).status_code == 401
            # ...and the agent's key does not open the console's.
            assert (
                await client.get(
                    f"{gateway_app.INTERNAL}/audit",
                    headers={"authorization": f"Bearer {wired.key}"},
                )
            ).status_code == 403
            # Each with its own key, each open.
            assert (
                await client.get(
                    f"{gateway_app.INTERNAL}/audit",
                    headers={"x-vd-console-key": CONSOLE_KEY},
                )
            ).json()["intact"]
            opened = await client.post(
                "/mcp",
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2025-06-18",
                        "capabilities": {},
                        "clientInfo": {"name": "test", "version": "1"},
                    },
                },
                headers={
                    "authorization": f"Bearer {wired.key}",
                    "accept": "application/json, text/event-stream",
                    "content-type": "application/json",
                },
            )
            assert opened.status_code == 200
            assert opened.headers["mcp-session-id"]


async def test_a_verified_write_offers_an_undo_and_a_read_does_not(console, wired):
    action_id, _ = await _park(wired, "undoable")
    await console.post(f"/actions/{action_id}/approve", json={"approver": wired.admin})
    await wired.gateway.call(await wired.session_id(), "db_query", {"table": "customers"})

    rows = {
        row["action_id"]: row
        for row in (await console.get(f"/apps/{wired.app_uuid}/activity")).json()
    }
    assert rows[action_id]["undoable"] is True
    reads = [row for row in rows.values() if row["tool"] == "db_query"]
    assert reads
    assert not any(row["undoable"] for row in reads)

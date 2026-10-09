"""The demo, run end to end. Readme.md section 26, M14.

Every step below goes through a real process boundary, because the point of the
demo is that these are separate things that agree:

  - the **control plane** is its own uvicorn process, and the agent is created
    on its dashboard the way a builder would create one;
  - the **gateway** is its own uvicorn process, and the agent talks to it over
    MCP with a bearer key — the console never touches an `agent-*` secret;
  - the **app** is the one the pipeline just deployed, with no security code in
    it at all, and the rows come back filtered by Postgres.

Steps 1, 2 and 3 are the deploy half, and they have their own files: the clean
branch reaching two browsers is `test_clean.py`, and the planted bug being
caught by name is `test_planted_bugs.py`. Repeating a 20-second deploy here
would not test anything new, so this file picks the script up at step 4 and
carries it to step 11. Steps 9 and 12 are AWS and are not written yet.
"""

from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass

import asyncpg
import httpx
import pytest_asyncio

from control_plane.models import ActionStatus, DeploymentStatus
from tests.gateway.test_mcp_server import Agent
from tests.pipeline.conftest import settle, sign_in

ALICE = ("alice@example.com", "alice-password-1")
BOB = ("bob@example.com", "bob-password-2")
BOSS = ("boss@example.com", "boss-password-3")

HERS = "alice buys milk"
HER_DRAFT = "alice drafts a plan"
HIS = "bob books a flight"

# Section 20.1 builds the table and column allowlist from the schema this
# deployment proved, so a policy can only name what really went live.
#
# The modes are the demo's: updating is `auto`, which is what makes step 7 an
# immediate "no rows you can access matched" rather than something a person has
# to look at; deleting is `approve`, which is step 8. `users: {}` is a table the
# agent may not touch at all, so step 7 also has something to be refused.
POLICY = """
agent: todo-helper
acts_for: alice@example.com
session_ttl_minutes: 30
rate_limit_per_minute: 240
postgres:
  default: {read: auto}
  tables:
    todos: {read: auto, update: auto, delete: approve}
    users: {}
"""


@dataclass
class Demo:
    deployment_id: uuid.UUID
    app_id: str
    endpoint: str
    key: str
    approver: str


async def _todos(pipeline, app_id: str) -> dict[str, bool]:
    """The app's own table, read as the admin. Nothing else does this."""
    conn = await asyncpg.connect(pipeline.config.app_admin_dsn)
    try:
        rows = await conn.fetch(f'SELECT title, done FROM "{app_id}".todos')
    finally:
        await conn.close()
    return {row["title"]: row["done"] for row in rows}


def _console(pipeline) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        base_url=pipeline.config.public_url, follow_redirects=True, timeout=30
    )


@pytest_asyncio.fixture(scope="session")
async def demo(pipeline):
    """Steps 2 and 5: a deployed app with people and todos in it, and an agent
    created on the dashboard."""
    deployment_id = await pipeline.deploy("clean", name="demo todo")
    await pipeline.confirm(deployment_id)
    deployment = await pipeline.deployment(deployment_id)
    assert deployment.status == DeploymentStatus.live.value, deployment.blocked_reason

    app = await pipeline.app_of(deployment_id)
    alice = await pipeline.add_person(deployment_id, *ALICE)
    bob = await pipeline.add_person(deployment_id, *BOB)
    # Somebody who may approve what the agent asks for (19.4).
    await pipeline.add_person(deployment_id, *BOSS, role="admin")

    await pipeline.give_todo(app.app_id, alice.principal_key, HERS)
    await pipeline.give_todo(app.app_id, alice.principal_key, HER_DRAFT)
    await pipeline.give_todo(app.app_id, bob.principal_key, HIS)

    async with _console(pipeline) as browser:
        on = await browser.post(f"/apps/{app.app_id}/agents-enabled", data={"on": "true"})
        assert on.status_code == 200, on.text
        page = await browser.post(
            f"/apps/{app.app_id}/agents",
            data={
                "name": "todo-helper",
                "acts_for": str(alice.id),
                "policy_yaml": POLICY,
            },
        )
        assert page.status_code == 200, page.text
        # The key is on that page once, and that is the only time it exists
        # outside the agent's own configuration.
        shown = re.search(r"vd_agent_[A-Za-z0-9_.-]+", page.text)
        assert shown, page.text

    return Demo(
        deployment_id=deployment_id,
        app_id=app.app_id,
        endpoint=deployment.endpoint,
        key=shown.group(0),
        approver=BOSS[0],
    )


@pytest_asyncio.fixture(scope="session")
async def robot(pipeline, demo):
    """The agent, connected to the gateway over MCP. Step 5."""
    async with httpx.AsyncClient(base_url=pipeline.gateway_url, timeout=30) as http:
        agent = Agent(http, demo.key)
        opened = await agent.initialize()
        assert opened.status_code == 200, opened.text
        yield agent


@pytest_asyncio.fixture
async def browser(pipeline):
    async with _console(pipeline) as client:
        yield client


# ---------------------------------------------------------------------------
# Step 4: two people, one unfiltered endpoint
# ---------------------------------------------------------------------------


async def test_step_4_two_people_see_only_their_own(demo):
    await settle()
    hers = await sign_in(demo.endpoint, *ALICE)
    his = await sign_in(demo.endpoint, *BOB)
    try:
        assert sorted(t["title"] for t in (await hers.get("/todos")).json()["todos"]) == [
            HERS,
            HER_DRAFT,
        ]
        assert [t["title"] for t in (await his.get("/todos")).json()["todos"]] == [HIS]
    finally:
        await hers.aclose()
        await his.aclose()


# ---------------------------------------------------------------------------
# Steps 5 and 6: the agent is handed the person's access and nothing else
# ---------------------------------------------------------------------------


async def test_step_5_only_the_tools_the_policy_leaves_open_are_offered(robot):
    offered = set(await robot.tools())
    assert "db_query" in offered
    assert "db_update" in offered
    assert "db_delete" in offered
    # Forbidden tools are not listed and not callable: creating a row was never
    # mentioned in the policy, so it does not exist for this agent.
    assert "db_create" not in offered


async def test_step_6_the_agent_reads_only_alices_todos(robot):
    answer = await robot.call("db_query", table="todos")
    assert answer["is_error"] is False, answer
    titles = sorted(row["title"] for row in answer["body"]["rows"])
    assert titles == [HERS, HER_DRAFT]
    assert HIS not in titles


# ---------------------------------------------------------------------------
# Step 7: the escape attempt
# ---------------------------------------------------------------------------


async def test_step_7_aiming_at_bobs_todo_changes_nothing_and_is_logged(
    pipeline, robot, demo, browser
):
    answer = await robot.call(
        "db_update",
        table="todos",
        filters=[{"column": "title", "op": "=", "value": HIS}],
        values={"done": True},
    )

    assert answer["body"]["affected"] == 0
    assert answer["body"]["reason"] == "No rows you can access matched."
    # Bob's todo is untouched, and nothing told the agent that a row it cannot
    # see exists.
    assert (await _todos(pipeline, demo.app_id))[HIS] is False
    assert HIS not in json.dumps(answer["body"])

    page = await browser.get(f"/apps/{demo.app_id}/activity")
    assert "No rows you can access matched." in page.text


async def test_step_7_a_table_the_policy_withholds_is_refused_outright(robot):
    """The other half of the escape attempt: aiming at a whole table.

    `users: {}` names the table and permits nothing, so this is a refusal
    before any connection is opened — not an empty result set. The agent is
    told it may not, and not what is in there.
    """
    refused = await robot.call("db_query", table="users")
    assert refused["is_error"] is True
    assert refused["body"]["status"] == ActionStatus.denied.value
    assert refused["body"]["reason"] == "This agent may not query `users`."


# ---------------------------------------------------------------------------
# Step 8: a write waits for a person, then is approved, then is undone
# ---------------------------------------------------------------------------


async def test_step_8_a_write_waits_is_approved_and_can_be_undone(
    pipeline, robot, demo, browser
):
    asked = await robot.call(
        "db_delete",
        table="todos",
        filters=[{"column": "title", "op": "=", "value": HER_DRAFT}],
    )
    action_id = asked["body"]["action_id"]
    assert asked["body"]["status"] == ActionStatus.pending_approval.value
    # Nothing has happened yet, which is the whole point of parking it.
    assert HER_DRAFT in await _todos(pipeline, demo.app_id)

    # The approver sees what would change before deciding (19.4).
    waiting = await browser.get(f"/apps/{demo.app_id}/approvals")
    assert action_id in waiting.text
    assert "What would change, hashed" in waiting.text
    assert "todo-helper" in waiting.text
    assert ALICE[0] in waiting.text

    approved = await browser.post(
        f"/apps/{demo.app_id}/actions/{action_id}/approve",
        data={"approver": demo.approver},
    )
    assert approved.status_code == 200, approved.text
    assert HER_DRAFT not in await _todos(pipeline, demo.app_id)

    # The agent finds out by asking, because there is nothing to poll over MCP.
    told = await robot.call("get_action_status", action_id=action_id)
    assert told["body"]["status"] == ActionStatus.verified.value

    activity = await browser.get(f"/apps/{demo.app_id}/activity")
    assert f"/apps/{demo.app_id}/actions/{action_id}/undo" in activity.text
    undone = await browser.post(
        f"/apps/{demo.app_id}/actions/{action_id}/undo",
        data={"approver": demo.approver},
    )
    assert undone.status_code == 200, undone.text
    assert HER_DRAFT in await _todos(pipeline, demo.app_id)


# ---------------------------------------------------------------------------
# Step 10: the kill switch
# ---------------------------------------------------------------------------


async def test_step_10_the_kill_switch_denies_the_next_call(
    pipeline, robot, demo, browser
):
    stopped = await browser.post(
        "/kill-switch", data={"on": "true", "back": f"/apps/{demo.app_id}/agents"}
    )
    assert stopped.status_code == 200

    # The session was already open, and it is still refused: the six checks in
    # 19.1 are made on every call and none of them is cached.
    denied = await robot.call("db_query", table="todos")
    assert denied["is_error"] is True
    assert denied["body"]["status"] == ActionStatus.denied.value

    running = await browser.post(
        "/kill-switch", data={"on": "false", "back": f"/apps/{demo.app_id}/agents"}
    )
    assert running.status_code == 200
    assert (await robot.call("db_query", table="todos"))["is_error"] is False


# ---------------------------------------------------------------------------
# Step 11: the sentence
# ---------------------------------------------------------------------------


async def test_step_11_the_evidence_report_is_one_sentence(demo, browser):
    page = await browser.get(f"/apps/{demo.app_id}/evidence")
    assert page.status_code == 200
    assert "Audit chain intact." in page.text

    report = (await browser.get(f"/apps/{demo.app_id}/evidence.json")).json()
    assert report["total"] > 0
    assert report["approved"] >= 1
    assert report["undone"] >= 1
    assert report["denied"] >= 1
    assert report["chain"]["intact"] is True
    # Step 11's headline: an agent reached nothing beyond the person it works
    # for, and that is counted from the audit log rather than asserted.
    assert report["outside_access"]["proven"] is True
    assert report["outside_access"]["reached"] == 0


async def test_step_11_the_audit_chain_page_agrees(browser):
    page = await browser.get("/audit")
    assert "Intact" in page.text

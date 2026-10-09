"""The pages a person actually clicks. Readme.md 19.4, 19.5, 19.6, 19.7, 23.

Everything here is a form post or a page load against the real control plane
app, wired to the real gateway over the real internal API. Nothing calls
`control_plane.service` or `gateway.pipeline` directly, because the claim being
tested is that the surfaces section 23 lists are *connected*: that the approve
button approves, that the undo button undoes, and that the key is shown once.

The one thing replaced is the socket. `Loopback` is the console's own client
with its transport pointed at the gateway's ASGI app instead of at port 8200,
so the request still goes through `X-VD-Console-Key`, through the internal API
and through the pipeline. What is not tested here is anything about *whether*
an action may be approved — that lives in `test_approvals.py`, and the two
tests below about a wrong approver and a down gateway are here only to show
that this layer does not skip it or soften it.
"""

from __future__ import annotations

import re

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import select

from control_plane.app import create_app
from control_plane.config import DEFAULT_APP_DSN, ControlPlaneConfig
from control_plane.gateway_client import KEY_HEADER, TIMEOUT, GatewayClient
from control_plane.models import (
    ActionStatus,
    Agent,
    AgentPolicy,
    AgentStatus,
    App,
    GlobalSettings,
)
from gateway import app as gateway_app
from gateway.errors import Denied
from tests.gateway.conftest import wire
from tests.gateway.test_mcp_server import Lifespan

CONTROL_URL = (
    "postgresql+asyncpg://vd_admin:vd_local_password@127.0.0.1:55432"
    "/vibedeploy_console_test"
)

CONSOLE_KEY = "the-console-key-and-nothing-else"

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

ALICE = "alice@example.com"


class Loopback(GatewayClient):
    """The console's client with its socket replaced and nothing else.

    Subclassed rather than mocked so that the key header, the `/internal`
    prefix, the status handling and `GatewayDown` are all still the real
    ones: a test double here could pass while the console could not reach a
    gateway at all.
    """

    def __init__(self, internal, *, api_key: str):
        super().__init__(base_url="http://gateway", api_key=api_key)
        self._internal = internal

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self._internal),
            base_url=self._base,
            headers={KEY_HEADER: self._key},
            timeout=TIMEOUT,
        )


def _config(wired) -> ControlPlaneConfig:
    return ControlPlaneConfig(
        database_url=CONTROL_URL,
        app_admin_dsn=DEFAULT_APP_DSN,
        sidecar_api_key="unused here",
        state_root=wired.gateway._store.root.parent,
        public_url="http://127.0.0.1:0",
        gateway_api_key=CONSOLE_KEY,
    )


@pytest_asyncio.fixture(scope="module")
async def wired(gateway_admin, tmp_path_factory):
    async with wire(
        gateway_admin,
        tmp_path_factory.mktemp("console"),
        control_url=CONTROL_URL,
        policy=POLICY,
    ) as wired:
        yield wired


@pytest_asyncio.fixture(scope="module")
async def served(wired):
    """The control plane process, talking to the gateway process."""
    config = _config(wired)
    console = create_app(config)
    async with Lifespan(console):
        # The gateway the console was handed on startup wants a port. This one
        # wants the same gateway, in this process.
        console.state.gateway = Loopback(
            gateway_app.internal(wired.gateway, config), api_key=CONSOLE_KEY
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=console),
            base_url="http://console",
            follow_redirects=True,
            timeout=30,
        ) as client:
            yield console, client


@pytest.fixture
def browser(served):
    return served[1]


@pytest_asyncio.fixture
async def app_id(wired):
    async with wired.sessions() as session:
        return (await session.get(App, wired.app_uuid)).app_id


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
# Agents. 19.5 and section 23.
# ---------------------------------------------------------------------------


async def test_the_key_is_on_the_page_once_and_never_again(browser, wired, app_id):
    page = await browser.post(
        f"/apps/{app_id}/agents",
        data={
            "name": "shown-once",
            "acts_for": str(wired.alice),
            "policy_yaml": POLICY,
        },
    )
    assert page.status_code == 200, page.text
    assert "vd_agent_" in page.text

    # The same page, loaded again. There is nowhere left to read the key from,
    # because it was never stored in a form that could be read back.
    again = await browser.get(f"/apps/{app_id}/agents")
    assert "shown-once" in again.text
    assert "vd_agent_" not in again.text


async def test_a_policy_that_will_not_parse_is_not_saved(browser, wired, app_id):
    page = await browser.post(
        f"/apps/{app_id}/agents",
        data={
            "name": "never-exists",
            "acts_for": str(wired.alice),
            "policy_yaml": "agent: nobody-to-act-for\npostgres: {}\n",
        },
    )
    assert page.status_code == 200
    # The policy loader's own sentence, shown to the person who wrote it.
    assert "acts_for" in page.text
    assert "never-exists" not in page.text.split("<h2>Add an agent</h2>")[0]

    async with wired.sessions() as session:
        assert (
            await session.execute(select(Agent).where(Agent.name == "never-exists"))
        ).scalar_one_or_none() is None


async def test_every_policy_edit_is_a_new_version(browser, wired, app_id):
    page = await browser.post(
        f"/apps/{app_id}/agents",
        data={"name": "edited", "acts_for": str(wired.alice), "policy_yaml": POLICY},
    )
    assert page.status_code == 200
    async with wired.sessions() as session:
        agent = (
            await session.execute(select(Agent).where(Agent.name == "edited"))
        ).scalar_one()

    saved = await browser.post(
        f"/apps/{app_id}/agents/{agent.id}/policy",
        data={"policy_yaml": POLICY.replace("240", "120")},
    )
    assert saved.status_code == 200, saved.text

    # Contract 7.5: the first version is still there, unchanged, because an
    # action that was decided by it has to keep its reason.
    async with wired.sessions() as session:
        versions = (
            (
                await session.execute(
                    select(AgentPolicy)
                    .where(AgentPolicy.agent_id == agent.id)
                    .order_by(AgentPolicy.version)
                )
            )
            .scalars()
            .all()
        )
    assert [row.version for row in versions] == [1, 2]
    assert "240" in versions[0].policy_yaml
    assert "120" in versions[1].policy_yaml
    assert "Save as version 3" in saved.text


async def test_a_policy_that_will_not_parse_does_not_become_a_version(
    browser, wired, app_id
):
    page = await browser.post(
        f"/apps/{app_id}/agents",
        data={"name": "unedited", "acts_for": str(wired.alice), "policy_yaml": POLICY},
    )
    assert page.status_code == 200
    async with wired.sessions() as session:
        agent = (
            await session.execute(select(Agent).where(Agent.name == "unedited"))
        ).scalar_one()

    refused = await browser.post(
        f"/apps/{app_id}/agents/{agent.id}/policy",
        data={"policy_yaml": "postgres: {}\n"},
    )
    assert refused.status_code == 200
    assert "agent" in refused.text
    async with wired.sessions() as session:
        versions = (
            (
                await session.execute(
                    select(AgentPolicy).where(AgentPolicy.agent_id == agent.id)
                )
            )
            .scalars()
            .all()
        )
    assert [row.version for row in versions] == [1]


async def test_the_revoke_button_closes_the_door(browser, wired, app_id):
    """19.5 one click, four things. The one that matters from out here is that
    the key stops working, so that is what is checked."""
    page = await browser.post(
        f"/apps/{app_id}/agents",
        data={"name": "revoked", "acts_for": str(wired.alice), "policy_yaml": POLICY},
    )
    # `vd_agent_<key id>.<secret>`, so the dot is part of it.
    shown = re.search(r"vd_agent_[A-Za-z0-9_.-]+", page.text)
    assert shown, page.text
    key = shown.group(0)
    assert (await wired.gateway.open_session(key)).session_id

    async with wired.sessions() as session:
        agent = (
            await session.execute(select(Agent).where(Agent.name == "revoked"))
        ).scalar_one()
    after = await browser.post(f"/apps/{app_id}/agents/{agent.id}/revoke")
    assert after.status_code == 200

    with pytest.raises(Denied):
        await wired.gateway.open_session(key)
    async with wired.sessions() as session:
        assert (await session.get(Agent, agent.id)).status == AgentStatus.disabled.value


async def test_the_kill_switch_stops_every_agent_and_lets_them_back(
    browser, wired, app_id
):
    on = await browser.post(
        "/kill-switch", data={"on": "true", "back": f"/apps/{app_id}/agents"}
    )
    assert on.status_code == 200
    async with wired.sessions() as session:
        assert (await session.get(GlobalSettings, 1)).agent_kill_switch is True
    with pytest.raises(Denied):
        await wired.session_id()

    off = await browser.post(
        "/kill-switch", data={"on": "false", "back": f"/apps/{app_id}/agents"}
    )
    assert off.status_code == 200
    assert await wired.session_id()


async def test_the_switch_cannot_send_somebody_somewhere_else(browser, wired, app_id):
    """`back` comes off a form, so an absolute URL there would be an open
    redirect out of the console."""
    answer = await browser.post(
        "/kill-switch", data={"on": "false", "back": "https://example.com/phish"}
    )
    assert answer.status_code == 200
    assert "example.com" not in str(answer.url)


async def test_turning_an_apps_agents_off_denies_the_next_call(browser, wired, app_id):
    off = await browser.post(f"/apps/{app_id}/agents-enabled", data={"on": "false"})
    assert off.status_code == 200
    with pytest.raises(Denied):
        await wired.session_id()

    on = await browser.post(f"/apps/{app_id}/agents-enabled", data={"on": "true"})
    assert on.status_code == 200
    assert await wired.session_id()


# ---------------------------------------------------------------------------
# Approvals, activity, audit, evidence. 19.4, 19.6, 19.7.
# ---------------------------------------------------------------------------


async def test_what_is_waiting_is_shown_with_what_would_change(
    browser, wired, app_id
):
    action_id, _ = await _park(wired, "shown on the approvals page")
    page = await browser.get(f"/apps/{app_id}/approvals")

    assert page.status_code == 200
    assert action_id in page.text
    assert "db_update" in page.text
    assert ALICE in page.text
    # The approval is bound to a hash of what would change, so the page shows
    # the hash rather than asking a person to take it on trust.
    assert "What would change, hashed" in page.text
    assert "Approving as" in page.text
    assert wired.admin in page.text


async def test_approving_from_the_page_changes_the_row(browser, wired, app_id):
    action_id, row_id = await _park(wired, "approved from the page")
    answer = await browser.post(
        f"/apps/{app_id}/actions/{action_id}/approve", data={"approver": wired.admin}
    )

    assert answer.status_code == 200, answer.text
    assert await wired.name_of(row_id) == "approved from the page"
    assert action_id not in (await browser.get(f"/apps/{app_id}/approvals")).text


async def test_rejecting_from_the_page_changes_nothing(browser, wired, app_id):
    action_id, row_id = await _park(wired, "rejected from the page")
    was = await wired.name_of(row_id)

    answer = await browser.post(
        f"/apps/{app_id}/actions/{action_id}/reject", data={"approver": wired.admin}
    )
    assert answer.status_code == 200
    assert await wired.name_of(row_id) == was


async def test_the_page_cannot_approve_what_the_pipeline_refuses(
    browser, wired, app_id
):
    """The form only offers admins, so this posts past the form. 19.4 says the
    approver must be an admin of the app, and alice is a member."""
    action_id, row_id = await _park(wired, "approved by the wrong person")
    was = await wired.name_of(row_id)

    answer = await browser.post(
        f"/apps/{app_id}/actions/{action_id}/approve", data={"approver": ALICE}
    )
    assert answer.status_code == 200
    assert await wired.name_of(row_id) == was


async def test_the_undo_button_is_on_the_activity_page_and_works(
    browser, wired, app_id
):
    action_id, row_id = await _park(wired, "undone from the page")
    was_before_any_of_this = await wired.name_of(row_id)
    await browser.post(
        f"/apps/{app_id}/actions/{action_id}/approve", data={"approver": wired.admin}
    )
    assert await wired.name_of(row_id) == "undone from the page"

    page = await browser.get(f"/apps/{app_id}/activity")
    assert action_id in page.text
    assert f"/apps/{app_id}/actions/{action_id}/undo" in page.text

    undone = await browser.post(
        f"/apps/{app_id}/actions/{action_id}/undo", data={"approver": wired.admin}
    )
    assert undone.status_code == 200
    assert await wired.name_of(row_id) == was_before_any_of_this


async def test_a_read_has_no_undo_button(browser, wired, app_id):
    await wired.gateway.call(
        await wired.session_id(), "db_query", {"table": "customers"}
    )
    page = await browser.get(f"/apps/{app_id}/activity")
    assert "db_query" in page.text
    # One undo form per undoable write, and a read is not one of them.
    assert page.text.count("/undo") <= page.text.count("db_update")


async def test_the_audit_page_says_the_chain_is_intact(browser):
    page = await browser.get("/audit")
    assert page.status_code == 200
    assert "Intact" in page.text


async def test_the_evidence_page_leads_with_the_sentence(browser, app_id):
    page = await browser.get(f"/apps/{app_id}/evidence")
    assert page.status_code == 200
    assert "actions" in page.text
    assert "Audit chain intact." in page.text


async def test_the_evidence_report_downloads_as_json(browser, app_id):
    answer = await browser.get(f"/apps/{app_id}/evidence.json")
    assert answer.status_code == 200
    assert "attachment" in answer.headers["content-disposition"]
    report = answer.json()
    assert report["total"] > 0
    assert report["chain"]["intact"] is True


async def test_a_nonsense_date_is_refused_rather_than_ignored(browser, app_id):
    assert (
        await browser.get(f"/apps/{app_id}/evidence?since=last+tuesday")
    ).status_code == 400


# ---------------------------------------------------------------------------
# The gateway not answering
# ---------------------------------------------------------------------------


async def test_a_gateway_that_is_not_answering_says_so_and_changes_nothing(
    served, wired, app_id
):
    """Fail soft on the page, which is not the same as fail open: nothing is
    approved, nothing is switched, one page just cannot be filled in."""
    console, browser = served
    action_id, row_id = await _park(wired, "nobody could approve this")
    was = await wired.name_of(row_id)
    working = console.state.gateway
    # A port with nothing behind it: the same client, no gateway.
    console.state.gateway = GatewayClient(
        base_url="http://127.0.0.1:1", api_key=CONSOLE_KEY
    )
    try:
        for path in ("approvals", "activity"):
            page = await browser.get(f"/apps/{app_id}/{path}")
            assert page.status_code == 200
            assert "not answering" in page.text
        assert "not answering" in (await browser.get("/audit")).text
        assert "not answering" in (await browser.get(f"/apps/{app_id}/evidence")).text

        clicked = await browser.post(
            f"/apps/{app_id}/actions/{action_id}/approve",
            data={"approver": wired.admin},
        )
        assert clicked.status_code == 200
        assert await wired.name_of(row_id) == was
    finally:
        console.state.gateway = working


async def test_a_console_with_the_wrong_key_is_refused_by_the_gateway(
    served, wired, app_id
):
    console, browser = served
    working = console.state.gateway
    console.state.gateway = Loopback(
        gateway_app.internal(wired.gateway, _config(wired)), api_key="not-the-key"
    )
    try:
        page = await browser.get("/audit")
        assert page.status_code == 200
        assert "would not accept this console" in page.text
    finally:
        console.state.gateway = working

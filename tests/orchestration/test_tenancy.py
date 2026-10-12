"""Many accounts on one installation, and what each of them may touch.

Until sign-up existed this file could not have failed. There was one operator,
`web.routes.index` listed every app because every app was theirs, and
`_app_or_404` looked an app up by the id in the URL and handed it over. Open the
console to strangers and that is a directory of other people's work plus a
deploy button for each of them, reachable by guessing eight hex characters.

So the two properties below are the ones worth a test apiece:

  - **an account is permission to create apps of your own and nothing else.**
    Not a 403 either: a short id that can be probed is an existence oracle, so
    somebody else's app answers exactly as a nonexistent one does.
  - **administrator is not something an account can become.** It is the oldest
    `builders` row — no column, no form, no route — so the test that matters is
    not "is the check right" but "is there anything to check": a session that
    *says* it is an administrator is not one, because the claim is derived from
    the database on every request and never read from the cookie.

No Docker and no app schemas. Deleting an app is checked as far as the queue:
that the row stops being deployable, gives up its route and leaves a teardown
job behind. What that job then does to Postgres belongs with the tests that
have a real app in a real database.
"""

from __future__ import annotations

import re
import uuid
from contextlib import asynccontextmanager
from typing import AsyncIterator

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import select, text

from control_plane import auth, jobs, service
from control_plane.app import create_app
from control_plane.config import DEFAULT_APP_DSN, ControlPlaneConfig
from control_plane.db import make_sessionmaker
from control_plane.models import SCHEMA, App, Builder, Job
from control_plane.web import routes as web
from tests.orchestration.conftest import CONTROL_URL

SECRET = "a-session-secret-that-is-not-a-default-anybody-has"
PASSWORD = "a-password-long-enough"

HOME = web.HOME

# In the order they are created, which is the order that decides everything
# here: the first row is the administrator.
BOSS = "boss@tests.invalid"
ALICE = "alice@tests.invalid"
BOB = "bob@tests.invalid"


class Browser(httpx.AsyncClient):
    """A client that carries the CSRF token off the last page it was shown.

    Read from the rendered HTML rather than minted, so a form that stops
    carrying one fails here instead of passing quietly.
    """

    csrf: str = ""

    async def post(self, url, *, data=None, **kwargs):  # type: ignore[override]
        body = dict(data or {})
        body.setdefault("csrf", self.csrf)
        return await super().post(url, data=body, **kwargs)

    async def sign_in(self, email: str) -> None:
        landed = await self.post(
            "/login", data={"email": email, "password": PASSWORD}, follow_redirects=True
        )
        assert landed.url.path == HOME, f"the login was refused: {landed.url}"
        self._remember(landed.text)

    def _remember(self, html: str) -> None:
        found = re.search(r'name="csrf" value="([^"]+)"', html)
        assert found, "the console rendered a page with no CSRF token in it"
        self.csrf = found.group(1)

    async def add_app(self, name: str) -> str:
        made = await self.post("/apps", data={"name": name, "repo": "/nowhere"})
        assert made.status_code == 303, made.text
        return made.headers["location"].rsplit("/", 1)[-1]


def _config(root) -> ControlPlaneConfig:
    return ControlPlaneConfig(
        database_url=CONTROL_URL,
        app_admin_dsn=DEFAULT_APP_DSN,
        sidecar_api_key="unused-here",
        session_secret=SECRET,
        state_root=root,
        public_url="http://127.0.0.1:0",
    )


@pytest_asyncio.fixture
async def installation(_queue_engine, tmp_path):
    """Three accounts, created oldest first, and nobody logged in.

    `boss` is first and is therefore the administrator. That is the whole of
    how it is decided, which is why the fixture does not set a flag anywhere.
    """
    _, engine = _queue_engine
    async with engine.begin() as conn:
        await conn.execute(text(f'TRUNCATE "{SCHEMA}".builders CASCADE'))
        await conn.execute(text(f'TRUNCATE "{SCHEMA}".jobs CASCADE'))

    app = create_app(_config(tmp_path))
    async with app.router.lifespan_context(app):
        sessions = make_sessionmaker(engine)
        async with sessions() as session:
            for email in (BOSS, ALICE, BOB):
                await service.create_builder(session, email=email, password=PASSWORD)
                # Committed one at a time so the `created_at` ordering the
                # administrator is read from is the order written above and not
                # three rows sharing a timestamp.
                await session.commit()
        yield app, sessions


async def browser(app) -> Browser:
    return Browser(
        transport=httpx.ASGITransport(app=app), base_url="http://console"
    )


@asynccontextmanager
async def _as(app, email: str) -> AsyncIterator[Browser]:
    """A logged-in client. A context manager and not a plain factory because
    signing in is itself a request, and httpx refuses to enter a client that has
    already sent one."""
    async with await browser(app) as client:
        await client.sign_in(email)
        yield client


# ---------------------------------------------------------------------------
# An app belongs to the account that made it
# ---------------------------------------------------------------------------


async def test_your_apps_are_the_only_ones_you_are_shown(installation):
    app, _ = installation
    async with _as(app, ALICE) as alice:
        hers = await alice.add_app("hers")
    async with _as(app, BOB) as bob:
        his = await bob.add_app("his")
        listing = await bob.get(HOME)

    assert his in listing.text
    assert hers not in listing.text, "the dashboard listed somebody else's app"


# Every app-shaped URL on the private router, which is what makes this a
# property of the console rather than of the handful somebody remembered. The
# fillers are valid shapes on purpose: a 422 for a malformed UUID would be a
# refusal that proves nothing.
def _app_routes() -> list[tuple[str, str]]:
    out = []
    for route in web.router.routes:
        path = route.path  # type: ignore[attr-defined]
        if "{app_id}" not in path:
            continue
        for method in sorted(route.methods or []):  # type: ignore[attr-defined]
            if method in ("HEAD", "OPTIONS"):
                continue
            out.append((method, path))
    assert len(out) > 10, "the route table looks truncated"
    return out


# A superset of every form field those routes require. Sent whole to all of
# them, because FastAPI ignores the ones a given handler does not declare and
# refuses with 422 before the handler runs if one it does declare is missing —
# and a 422 is a refusal that proves nothing about who owns the app. The values
# are the right shape and deliberately harmless: `confirm` is not any app's id,
# `on` is false, the env sets nothing. Nothing here is the empty string, because
# a blank urlencoded field arrives as absent and is refused with the same 422 as
# if it had not been sent.
POST_BODY = {
    "env": "# nothing",
    "ref": "main",
    "email": "nobody@tests.invalid",
    "password": PASSWORD,
    "confirm": "not-an-app-id",
    "name": "probe",
    "acts_for": str(uuid.uuid4()),
    "policy_yaml": "version: 1",
    "on": "false",
    "approver": str(uuid.uuid4()),
}


@pytest.mark.parametrize("method,path", _app_routes())
async def test_somebody_elses_app_does_not_exist(installation, method, path):
    """404, on every route that takes an app id, for an app that is not yours.

    404 and not 403, because 403 is an answer: it confirms the app exists. The
    id is eight hex characters and can be probed, and there is nothing a person
    can do with the difference.
    """
    app, _ = installation
    async with _as(app, ALICE) as alice:
        hers = await alice.add_app("hers")

    filled = path.replace("{app_id}", hers)
    for name, value in (
        ("{deployment_id}", str(uuid.uuid4())),
        ("{agent_id}", str(uuid.uuid4())),
        ("{action_id}", str(uuid.uuid4())),
        ("{verdict}", "approve"),
    ):
        filled = filled.replace(name, value)
    assert "{" not in filled, f"no filler for a parameter in {path}"

    async with _as(app, BOB) as bob:
        answer = await bob.request(
            method,
            filled,
            data=(POST_BODY | {"csrf": bob.csrf}) if method != "GET" else None,
            headers={"accept": "text/html"},
        )
    assert answer.status_code == 404, (
        f"{method} {filled} -> {answer.status_code}: {answer.text[:400]}"
    )


async def test_your_own_app_is_reachable(installation):
    """The control. Without it every assertion above would pass on a console
    that answered 404 to everything."""
    app, _ = installation
    async with _as(app, ALICE) as alice:
        hers = await alice.add_app("hers")
        page = await alice.get(f"/apps/{hers}")
    assert page.status_code == 200
    assert hers in page.text


async def test_a_deployment_of_somebody_elses_app_does_not_exist(installation):
    """The deployment page is scoped through its app, not on its own.

    A deployment id is a UUID and far harder to guess than an app id, which is
    exactly why it must not be the one unchecked way in.
    """
    app, sessions = installation
    async with _as(app, ALICE) as alice:
        hers = await alice.add_app("hers")

    async with sessions() as session:
        row = (
            await session.execute(select(App).where(App.app_id == hers))
        ).scalar_one()
        row.answers = {
            "audience": "me",
            "size": "small",
            "visibility": "own_data",
            "sensitive": False,
        }
        deployment = await service.start_deployment(session, app=row, ref="main")
        await session.commit()
        deployment_id = deployment.id

    async with _as(app, BOB) as bob:
        answer = await bob.get(f"/deployments/{deployment_id}")
    assert answer.status_code == 404

    async with _as(app, ALICE) as alice:
        assert (await alice.get(f"/deployments/{deployment_id}")).status_code == 200


async def test_two_people_may_both_call_an_app_todo(installation):
    """The subdomain is unique, and two strangers picking the same name is not
    an error either of them made.

    With one operator this was invisible. With sign-up it is the first thing
    anybody hits, and a 500 is not an answer.
    """
    app, sessions = installation
    async with _as(app, ALICE) as alice:
        first = await alice.add_app("todo")
    async with _as(app, BOB) as bob:
        second = await bob.add_app("todo")

    async with sessions() as session:
        rows = (
            (
                await session.execute(
                    select(App).where(App.app_id.in_([first, second]))
                )
            )
            .scalars()
            .all()
        )
    subdomains = {row.subdomain for row in rows}
    assert len(subdomains) == 2, subdomains
    assert "todo" in subdomains


# ---------------------------------------------------------------------------
# Administrator
# ---------------------------------------------------------------------------


async def test_the_administrator_is_the_oldest_account(installation):
    _, sessions = installation
    async with sessions() as session:
        assert (await auth.administrator(session)).email == BOSS


async def test_the_administrator_sees_everybody(installation):
    app, _ = installation
    async with _as(app, ALICE) as alice:
        hers = await alice.add_app("hers")

    async with _as(app, BOSS) as boss:
        page = await boss.get("/admin")
        assert page.status_code == 200
        assert ALICE in page.text and BOB in page.text
        assert hers in page.text, "the administrator cannot see what was pushed"

        # And can open it, which is the half that makes the page useful.
        theirs = await boss.get(f"/apps/{hers}")
        assert theirs.status_code == 200
        assert "not your app" in theirs.text


async def test_nobody_else_gets_that_page(installation):
    app, _ = installation
    async with _as(app, ALICE) as alice:
        refused = await alice.get("/admin")
    # 403 and not 404, which is the opposite of `_app_or_404` and deliberate:
    # this URL is fixed and public knowledge, so pretending it is absent fools
    # nobody. What is worth hiding is whether a particular app id exists.
    assert refused.status_code == 403


async def test_a_session_cannot_claim_to_be_the_administrator(installation):
    """The claim is derived per request and never read from the cookie.

    A correctly signed token carrying `is_admin` is still not an administrator,
    because nothing looks at that field: `require_operator` asks the database
    who the oldest builder is on every request. This is the test that would
    catch somebody "optimising" it into the token.
    """
    app, sessions = installation
    async with sessions() as session:
        alice = (
            await session.execute(select(Builder).where(Builder.email == ALICE))
        ).scalar_one()

    real = auth.Sessions(secret=SECRET, secure=False)
    token = real._seal(
        {
            "sub": str(alice.id),
            "email": alice.email,
            "csrf": "x" * 24,
            "exp": 2**31,
            # The lie.
            "is_admin": True,
            "admin": True,
        }
    )
    async with await browser(app) as client:
        client.cookies.set(auth.COOKIE, token)
        assert (await client.get(HOME)).status_code == 200, "a good cookie was refused"
        assert (await client.get("/admin")).status_code == 403


async def test_there_is_no_route_that_grants_it(installation):
    """`is_admin` has no setter, so there is no form or bug that can flip it.

    Asserted over the route table rather than by reading the code, so a page
    added later has to come past this test.
    """
    paths = {
        route.path  # type: ignore[attr-defined]
        for route in list(web.router.routes) + list(web.public.routes)
    }
    assert {path for path in paths if "admin" in path} == {"/admin"}

    # And that one is a GET. Nothing about who the administrator is can be
    # written over HTTP at all.
    admin = next(r for r in web.router.routes if r.path == "/admin")  # type: ignore[attr-defined]
    assert set(admin.methods or []) <= {"GET", "HEAD"}  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# Signing up
# ---------------------------------------------------------------------------


async def test_anybody_may_sign_up_and_gains_nothing_by_it(installation):
    app, _ = installation
    async with _as(app, ALICE) as alice:
        hers = await alice.add_app("hers")

    async with await browser(app) as stranger:
        made = await stranger.post(
            "/signup",
            data={"email": "new@tests.invalid", "password": PASSWORD},
            follow_redirects=True,
        )
        assert made.url.path == HOME, made.url
        # An account, and that is all it is.
        assert (await stranger.get("/admin")).status_code == 403
        assert (await stranger.get(f"/apps/{hers}")).status_code == 404


async def test_signup_will_not_take_a_short_password_or_a_taken_address(installation):
    app, sessions = installation
    async with await browser(app) as stranger:
        short = await stranger.post(
            "/signup", data={"email": "new@tests.invalid", "password": "short"}
        )
        assert short.status_code == 200
        assert "too short" in short.text

        taken = await stranger.post(
            "/signup", data={"email": ALICE, "password": PASSWORD}
        )
        assert taken.status_code == 200
        assert "already an account" in taken.text

    async with sessions() as session:
        rows = (await session.execute(select(Builder))).scalars().all()
    assert {row.email for row in rows} == {BOSS, ALICE, BOB}


async def test_signing_up_does_not_make_you_the_administrator(installation):
    """However many people arrive, the oldest row does not move."""
    app, sessions = installation
    async with await browser(app) as stranger:
        await stranger.post(
            "/signup", data={"email": "new@tests.invalid", "password": PASSWORD}
        )
    async with sessions() as session:
        assert (await auth.administrator(session)).email == BOSS


# ---------------------------------------------------------------------------
# Removing an app
# ---------------------------------------------------------------------------


async def _app_row(sessions, app_id: str) -> App | None:
    async with sessions() as session:
        return (
            await session.execute(select(App).where(App.app_id == app_id))
        ).scalar_one_or_none()


async def _teardown_jobs(sessions) -> list[Job]:
    async with sessions() as session:
        rows = await session.execute(
            select(Job).where(Job.kind == jobs.KIND_TEARDOWN)
        )
        return list(rows.scalars())


async def test_the_owner_can_remove_their_own_app(installation):
    app, sessions = installation
    async with _as(app, ALICE) as alice:
        hers = await alice.add_app("hers")
        before = await _app_row(sessions, hers)
        generation = before.desired_generation

        gone = await alice.post(f"/apps/{hers}/delete", data={"confirm": hers})
        assert gone.status_code == 303
        assert gone.headers["location"] == HOME

    row = await _app_row(sessions, hers)
    # Still there — the work is a job — but already undeployable and already
    # without a route, which is the half that has to be true before any worker
    # picks it up.
    assert row.status == "deleting"
    assert row.live_deployment_id is None
    assert row.desired_generation > generation, "a worker in flight was not fenced out"

    queued = await _teardown_jobs(sessions)
    assert len(queued) == 1
    # Null on purpose: `Job.app_id` cascades, so a teardown job that named its
    # own app would be deleted by the row it is deleting.
    assert queued[0].app_id is None
    assert queued[0].payload["app_id"] == hers


async def test_removing_it_needs_the_id_typed(installation):
    app, sessions = installation
    async with _as(app, ALICE) as alice:
        hers = await alice.add_app("hers")
        refused = await alice.post(
            f"/apps/{hers}/delete", data={"confirm": "app_something_else"}
        )
    assert refused.status_code == 400
    assert (await _app_row(sessions, hers)).status != "deleting"
    assert await _teardown_jobs(sessions) == []


async def test_a_deleting_app_cannot_be_deployed_or_deleted_twice(installation):
    app, sessions = installation
    async with _as(app, ALICE) as alice:
        hers = await alice.add_app("hers")
        await alice.post(f"/apps/{hers}/delete", data={"confirm": hers})

        again = await alice.post(f"/apps/{hers}/delete", data={"confirm": hers})
        assert again.status_code == 409
        deployed = await alice.post(f"/apps/{hers}/deploy", data={"ref": "main"})
        assert deployed.status_code == 409

    assert len(await _teardown_jobs(sessions)) == 1


async def test_a_stranger_cannot_remove_your_app(installation):
    app, sessions = installation
    async with _as(app, ALICE) as alice:
        hers = await alice.add_app("hers")
    async with _as(app, BOB) as bob:
        answer = await bob.post(f"/apps/{hers}/delete", data={"confirm": hers})
    assert answer.status_code == 404
    assert (await _app_row(sessions, hers)).status != "deleting"
    assert await _teardown_jobs(sessions) == []


async def test_the_administrator_can_remove_anybodys_app(installation):
    """The reason the page exists: an installation strangers push to is one
    whose owner has to be able to take something away."""
    app, sessions = installation
    async with _as(app, ALICE) as alice:
        hers = await alice.add_app("hers")
    async with _as(app, BOSS) as boss:
        # The token has to come off the page that was actually rendered for
        # this session, and that page is the app's own.
        page = await boss.get(f"/apps/{hers}")
        boss._remember(page.text)
        gone = await boss.post(f"/apps/{hers}/delete", data={"confirm": hers})
    assert gone.status_code == 303
    assert gone.headers["location"] == "/admin"
    assert (await _app_row(sessions, hers)).status == "deleting"
    assert len(await _teardown_jobs(sessions)) == 1

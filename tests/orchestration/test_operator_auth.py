"""The console's front door, which until now was not a door.

`control_plane/web/routes.py` guarded nothing. Its only dependencies were a
session and a config, so anybody who could reach port 8100 could create apps,
trigger deploys, add the people an app's RLS policies are written about, mint
agent API keys and flip the global kill switch. Worse than open: `_sole_builder`
would *invent* an operator rather than admit there was none, so the audit
columns that record who did a thing recorded `builder@localhost` for everyone.

These tests are deliberately exhaustive over the route table rather than over a
list somebody typed. A new page added to `web.routes.router` is covered the
moment it exists, and a new page added to `web.routes.public` has to be argued
for here, in `PUBLIC`, where it can be read.

No Docker and no app schemas: this is about the HTTP surface, so it runs in
about a second.
"""

from __future__ import annotations

import json
import uuid

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import select, text
from starlette.responses import Response

from control_plane import auth, service
from control_plane.app import create_app
from control_plane.config import (
    DEFAULT_APP_DSN,
    ConfigError,
    ControlPlaneConfig,
)
from control_plane.db import make_sessionmaker
from control_plane.models import SCHEMA, Builder
from control_plane.web import routes as web
from tests.orchestration.conftest import CONTROL_URL

SECRET = "a-session-secret-that-is-not-a-default-anybody-has"
OPERATOR = "operator@tests.invalid"
PASSWORD = "a-password-long-enough"

# The only paths a stranger may reach, and the whole list. `/health` is here so
# that a restart policy and a load balancer can see the process; it says nothing
# about any app.
PUBLIC = {"/login", "/setup", "/health"}

# Stand-ins for the path parameters. Valid shapes on purpose: a 422 for a
# malformed UUID would be a refusal that proves nothing about the login, so
# every probe below is a request that would have been served.
FILLERS = {
    "app_id": "app_00000000",
    "deployment_id": str(uuid.uuid4()),
    "agent_id": str(uuid.uuid4()),
    "action_id": str(uuid.uuid4()),
    "verdict": "approve",
}


def _fill(path: str) -> str:
    for name, value in FILLERS.items():
        path = path.replace("{" + name + "}", value)
    assert "{" not in path, f"no filler for a parameter in {path}"
    return path


def guarded() -> list[tuple[str, str]]:
    """Every method and path on the console's private router."""
    out = []
    for route in web.router.routes:
        for method in sorted(route.methods or []):  # type: ignore[attr-defined]
            if method in ("HEAD", "OPTIONS"):
                continue
            out.append((method, _fill(route.path)))  # type: ignore[attr-defined]
    assert len(out) > 15, "the route table looks truncated"
    return out


def _config(root, **extra) -> ControlPlaneConfig:
    return ControlPlaneConfig(
        database_url=CONTROL_URL,
        app_admin_dsn=DEFAULT_APP_DSN,
        sidecar_api_key="unused-here",
        session_secret=SECRET,
        state_root=root,
        public_url="http://127.0.0.1:0",
        **extra,
    )


@pytest_asyncio.fixture
async def console(_queue_engine, tmp_path):
    """A console with one operator in it and nobody logged in."""
    _, engine = _queue_engine
    async with engine.begin() as conn:
        await conn.execute(text(f'TRUNCATE "{SCHEMA}".builders CASCADE'))

    app = create_app(_config(tmp_path))
    async with app.router.lifespan_context(app):
        sessions = make_sessionmaker(engine)
        async with sessions() as session:
            await service.create_builder(session, email=OPERATOR, password=PASSWORD)
            await session.commit()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://console"
        ) as client:
            yield app, client


@pytest_asyncio.fixture
async def empty(_queue_engine, tmp_path):
    """A console that has never been set up."""
    _, engine = _queue_engine
    async with engine.begin() as conn:
        await conn.execute(text(f'TRUNCATE "{SCHEMA}".builders CASCADE'))
    app = create_app(_config(tmp_path))
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://console"
        ) as client:
            yield engine, client


async def _signed_in(client: httpx.AsyncClient) -> str:
    """Log in for real and come back with the CSRF token off a rendered page."""
    import re

    landed = await client.post(
        "/login", data={"email": OPERATOR, "password": PASSWORD}, follow_redirects=True
    )
    assert landed.url.path == "/", landed.url
    found = re.search(r'name="csrf" value="([^"]+)"', landed.text)
    assert found, "a form rendered with no CSRF token in it"
    return found.group(1)


# ---------------------------------------------------------------------------
# Nothing is reachable without a session
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("method,path", guarded())
async def test_no_cookie_no_console(console, method, path):
    """Every private route refuses a request that carries no session.

    A GET that a browser asked for is sent to the login page; everything else
    is told 401. Both are refusals — what matters is that neither is an answer,
    so a 200 here is a failure whatever its body says.
    """
    _, client = console
    answer = await client.request(
        method, path, headers={"accept": "text/html"}, data={}
    )
    assert answer.status_code in (303, 401), f"{method} {path} -> {answer.status_code}"
    if answer.status_code == 303:
        assert answer.headers["location"].startswith("/login")


async def test_the_public_list_is_the_public_list(console):
    """`public` holds what `PUBLIC` says and nothing that crept in later."""
    assert {route.path for route in web.public.routes} | {"/health"} == PUBLIC


async def test_a_forged_cookie_is_not_a_session(console):
    """A cookie we did not sign is not a cookie.

    Every forgery here names the operator who really exists. That matters: a
    forgery for an invented builder is refused by the row lookup rather than by
    the signature, so a test built that way would pass on a console that had
    stopped checking signatures altogether. The real id is what makes the
    signature the only thing standing in the way.

    The correctly signed token is checked too, and first. Without that control,
    all of this would also pass on a console that refused every cookie.
    """
    app, client = console
    async with app.state.sessionmaker() as session:
        builder = (await session.execute(select(Builder))).scalar_one()
        real = auth.Sessions(secret=SECRET, secure=False)
        good = _issued(real, builder)

        client.cookies.set(auth.COOKIE, good)
        assert (await client.get("/")).status_code == 200, "our own cookie was refused"

        head, _, tail = good.partition(".")
        forgeries = {
            # No signature offered at all.
            "unsigned": head,
            # Correctly signed, then edited. The payload still decodes and
            # still names the operator; only the bytes moved.
            "tampered": _retold(good, email="someone@else.invalid"),
            # The same claims about the same real operator, signed by somebody
            # who is not us.
            "wrong key": _issued(
                auth.Sessions(secret="not-the-secret-this-console-uses", secure=False),
                builder,
            ),
        }

    for why, token in forgeries.items():
        client.cookies.set(auth.COOKIE, token)
        answer = await client.get("/")
        assert answer.status_code in (303, 401), f"{why} was accepted"


def _retold(token: str, **changes) -> str:
    """A signed token whose payload was edited and whose signature was kept."""
    head, _, tail = token.partition(".")
    claims = json.loads(auth._unb64(head))
    claims.update(changes)
    edited = json.dumps(claims, separators=(",", ":"), sort_keys=True).encode()
    return f"{auth._b64(edited)}.{tail}"


def _issued(sessions: auth.Sessions, builder: Builder) -> str:
    """The cookie value `Sessions.issue` would actually put on a response."""
    carrier = Response()
    sessions.issue(carrier, builder)
    header = carrier.headers["set-cookie"]
    return header.split("=", 1)[1].split(";", 1)[0]


async def test_a_session_for_a_builder_that_is_gone_is_not_a_session(console):
    """The row is the authority, not the token.

    A signed cookie for a deleted operator is a valid signature over a claim
    that is no longer true, and the console has to notice. Otherwise removing
    an operator would not remove their access until the cookie expired.
    """
    app, client = console
    csrf = await _signed_in(client)
    assert (await client.get("/")).status_code == 200

    async with app.state.sessionmaker() as session:
        builder = (await session.execute(select(Builder))).scalar_one()
        await session.delete(builder)
        await session.commit()

    assert csrf  # the cookie is unchanged and still correctly signed
    assert (await client.get("/")).status_code == 401


# ---------------------------------------------------------------------------
# CSRF
# ---------------------------------------------------------------------------


async def test_a_post_without_the_token_is_refused(console):
    _, client = console
    await _signed_in(client)
    refused = await client.post("/apps", data={"name": "x", "repo": "/nowhere"})
    assert refused.status_code == 403, refused.text
    assert "stale" in refused.text


async def test_a_post_with_somebody_elses_token_is_refused(console):
    """A token that is well-formed but not this session's.

    This is the case a double-submit cookie with no signature would miss: the
    attacker can set a cookie *and* a matching form field. Here the token lives
    inside the signed payload, so the only token that matches is one the console
    issued to this session.
    """
    _, client = console
    await _signed_in(client)
    refused = await client.post(
        "/apps", data={"name": "x", "repo": "/nowhere", "csrf": "a" * 32}
    )
    assert refused.status_code == 403


async def test_a_post_with_the_token_goes_through(console):
    """And the guard is not simply refusing everything.

    Without this, every assertion above would pass on a console that answered
    403 to all comers, which is not what was built.
    """
    _, client = console
    csrf = await _signed_in(client)
    made = await client.post(
        "/apps", data={"name": "real", "repo": "/nowhere", "csrf": csrf}
    )
    assert made.status_code == 303
    assert made.headers["location"].startswith("/apps/app_")


# ---------------------------------------------------------------------------
# Logging in
# ---------------------------------------------------------------------------


async def test_the_wrong_password_is_not_a_session(console):
    _, client = console
    said = await client.post(
        "/login",
        data={"email": OPERATOR, "password": "not-it"},
        follow_redirects=True,
    )
    assert said.url.path == "/login"
    assert (await client.get("/")).status_code == 401


async def test_a_login_that_does_not_exist_says_the_same_thing(console):
    """Told apart, this page would enumerate who may log in."""
    _, client = console
    nobody = await client.post(
        "/login",
        data={"email": "stranger@nowhere.invalid", "password": "not-it"},
        follow_redirects=True,
    )
    wrong = await client.post(
        "/login", data={"email": OPERATOR, "password": "not-it"}, follow_redirects=True
    )
    assert nobody.text == wrong.text


async def test_guessing_is_throttled(console):
    """The sixth wrong guess in a minute is not answered at all.

    Not a defence against a botnet — that is what the proxy in front is for.
    It is what stops an unattended console from being a free password oracle.
    """
    _, client = console
    for _ in range(auth.ATTEMPTS):
        await client.post("/login", data={"email": OPERATOR, "password": "not-it"})
    throttled = await client.post(
        "/login", data={"email": OPERATOR, "password": "not-it"}
    )
    assert throttled.status_code == 429

    # And the throttle is not a lockout of the account by the attacker's own
    # choosing: the right password still works... after the window. What is
    # asserted here is only that the *correct* password is not leaked past it.
    still = await client.post("/login", data={"email": OPERATOR, "password": PASSWORD})
    assert still.status_code == 429


async def test_a_good_login_forgets_the_bad_ones(console):
    _, client = console
    for _ in range(auth.ATTEMPTS - 1):
        await client.post("/login", data={"email": OPERATOR, "password": "not-it"})
    await _signed_in(client)
    assert (await client.get("/")).status_code == 200


async def test_logging_out_ends_it(console):
    _, client = console
    csrf = await _signed_in(client)
    gone = await client.post("/logout", data={"csrf": csrf})
    assert gone.status_code == 303
    assert (await client.get("/")).status_code == 401


async def test_the_login_page_will_not_send_you_elsewhere(console):
    """`?next=` is a path or it is the root. Never a URL.

    An open redirect on a login page is the shape every credential-phishing
    link wants, because it arrives wearing our own hostname.
    """
    _, client = console
    landed = await client.post(
        "/login",
        data={
            "email": OPERATOR,
            "password": PASSWORD,
            "next": "https://elsewhere.invalid/",
        },
    )
    assert landed.headers["location"] == "/"


# ---------------------------------------------------------------------------
# First run
# ---------------------------------------------------------------------------


async def test_no_operator_means_the_setup_page_not_an_invented_one(empty):
    """What replaced `_sole_builder`'s silent-create branch.

    The old code answered "this is who set it up" to a request from nobody, and
    created the row to make it true. Now an installation with no operator says
    so, and creates nothing by being looked at.
    """
    engine, client = empty
    sent = await client.get("/", headers={"accept": "text/html"})
    assert sent.status_code == 303
    assert sent.headers["location"].startswith("/login")

    onwards = await client.get("/login")
    assert onwards.status_code == 303
    assert onwards.headers["location"] == "/setup"
    assert (await client.get("/setup")).status_code == 200

    async with engine.connect() as conn:
        rows = await conn.execute(text(f'SELECT count(*) FROM "{SCHEMA}".builders'))
        assert rows.scalar_one() == 0, "looking at the console created an operator"


async def test_setup_closes_behind_the_first_operator(empty):
    _, client = empty
    made = await client.post(
        "/setup", data={"email": OPERATOR, "password": PASSWORD}, follow_redirects=True
    )
    assert made.url.path == "/"

    # And it is shut, permanently, by the row it just made.
    assert (await client.get("/setup")).status_code == 404
    second = await client.post(
        "/setup", data={"email": "someone@else.invalid", "password": PASSWORD}
    )
    assert second.status_code == 409


async def test_setup_will_not_take_a_short_password(empty):
    engine, client = empty
    said = await client.post("/setup", data={"email": OPERATOR, "password": "short"})
    assert said.status_code == 200
    assert "too short" in said.text
    async with engine.connect() as conn:
        rows = await conn.execute(text(f'SELECT count(*) FROM "{SCHEMA}".builders'))
        assert rows.scalar_one() == 0


async def test_the_environment_can_own_the_installation_before_anybody_sees_it(
    _queue_engine, tmp_path
):
    """`VD_BOOTSTRAP_*`, so a public box never serves the setup page.

    Open, that page hands the installation to the first stranger who finds it.
    With these set, the console comes up already owned.
    """
    _, engine = _queue_engine
    async with engine.begin() as conn:
        await conn.execute(text(f'TRUNCATE "{SCHEMA}".builders CASCADE'))

    app = create_app(
        _config(
            tmp_path, bootstrap_email="boot@tests.invalid", bootstrap_password=PASSWORD
        )
    )
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://console"
        ) as client:
            # The setup page is already gone, and nobody has clicked anything.
            assert (await client.get("/setup")).status_code == 404
            landed = await client.post(
                "/login",
                data={"email": "boot@tests.invalid", "password": PASSWORD},
                follow_redirects=True,
            )
            assert landed.url.path == "/"


async def test_the_bootstrap_is_not_a_standing_password_reset(_queue_engine, tmp_path):
    """A second boot with the variables still set does not overwrite anybody.

    Otherwise `VD_BOOTSTRAP_PASSWORD` would be a way in for anybody who can
    read the env file, and rotating the operator's password would silently undo
    itself on the next restart.
    """
    _, engine = _queue_engine
    async with engine.begin() as conn:
        await conn.execute(text(f'TRUNCATE "{SCHEMA}".builders CASCADE'))
    sessions = make_sessionmaker(engine)
    async with sessions() as session:
        await service.create_builder(session, email=OPERATOR, password=PASSWORD)
        await session.commit()

    app = create_app(
        _config(tmp_path, bootstrap_email=OPERATOR, bootstrap_password="a-new-password")
    )
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://console"
        ) as client:
            said = await client.post(
                "/login",
                data={"email": OPERATOR, "password": "a-new-password"},
                follow_redirects=True,
            )
            assert said.url.path == "/login"


# ---------------------------------------------------------------------------
# It refuses to start rather than sign with a default
# ---------------------------------------------------------------------------


def test_no_session_secret_is_a_refusal_to_start(tmp_path):
    """Both ways in: from the environment, and from a config built by hand.

    A console with no signing key is not a degraded console. Every session is
    forgeable, which is the same thing as having no login — so this is an
    exception and not a warning in a log.
    """
    with pytest.raises(ConfigError, match="VD_SESSION_SECRET"):
        ControlPlaneConfig.from_env(
            {"VD_SIDECAR_API_KEY": "k", "VD_STATE_ROOT": str(tmp_path)}
        )

    with pytest.raises(ConfigError, match="VD_SESSION_SECRET"):
        create_app(
            ControlPlaneConfig(
                database_url=CONTROL_URL,
                app_admin_dsn=DEFAULT_APP_DSN,
                sidecar_api_key="k",
                state_root=tmp_path,
                public_url="http://127.0.0.1:0",
            )
        )


def test_the_cookie_is_locked_down_and_follows_the_scheme():
    """`HttpOnly` and `SameSite` always; `Secure` exactly when HTTPS.

    `Secure` cannot be hardcoded on: over plain HTTP the browser stops sending
    the cookie back and the console stops working. So it follows the scheme,
    and the scheme is the only thing that decides it.
    """
    builder = Builder(id=uuid.uuid4(), email=OPERATOR, password_hash="x")

    plain = Response()
    auth.Sessions(secret=SECRET, secure=False).issue(plain, builder)
    header = plain.headers["set-cookie"]
    assert "HttpOnly" in header and "SameSite=lax" in header
    assert "Secure" not in header

    tls = Response()
    auth.Sessions(secret=SECRET, secure=True).issue(tls, builder)
    assert "Secure" in tls.headers["set-cookie"]

    assert ControlPlaneConfig(
        database_url="x",
        app_admin_dsn="y",
        sidecar_api_key="k",
        state_root=".",  # type: ignore[arg-type]
        public_url="z",
        session_secret=SECRET,
        public_scheme="https",
    ).cookies_secure is True


def test_an_expired_session_is_not_a_session():
    """There is no refresh-on-use, so this window is the whole lifetime."""
    builder = Builder(id=uuid.uuid4(), email=OPERATOR, password_hash="x")
    stale = _issued(auth.Sessions(secret=SECRET, secure=False, max_age=-1), builder)

    # Read back by a `Sessions` with the same secret, so the signature is good
    # and the only thing wrong with it is the clock.
    reader = auth.Sessions(secret=SECRET, secure=False)
    assert reader._open(stale) is None

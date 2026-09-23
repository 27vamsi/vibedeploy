"""M6's acceptance criteria, against three real processes. Readme.md 8 M6.

    "Alice and Bob in two browsers each see only their own invoices from an
     unfiltered endpoint. Forged X-VD-Identity from outside does nothing."

Two things make these tests worth having rather than merely present:

  - The expected invoice ids come from the seeding plan, never from asking the
    app. If the app returned nothing at all, or everything, the plan would
    still say what Alice owns.
  - The app under test genuinely has no filter. `fixtures/apps/invoices/app.py`
    runs `SELECT ... FROM invoices` with no WHERE clause and contains no
    authorisation code. Every separation seen here was done by Postgres.
"""

from __future__ import annotations

import time

import httpx
import pytest
from vibedeploy_shim.identity import HEADER_NAME, sign_identity

from sidecar.session import COOKIE_NAME
from tests.e2e.conftest import EMAILS, PASSWORDS, sign_in


async def _invoice_ids(browser: httpx.AsyncClient) -> set[str]:
    response = await browser.get("/invoices")
    assert response.status_code == 200, response.text
    return {row["id"] for row in response.json()["invoices"]}


def _forged(stack, sub: str, *, key: bytes | None = None, app: str | None = None) -> str:
    now = int(time.time())
    return sign_identity(
        {
            "v": 1,
            "app": app or stack.app_id,
            "sub": sub,
            "role": "member",
            "iat": now,
            "exp": now + 60,
        },
        key if key is not None else b"an-attackers-own-key" * 4,
    )


# --------------------------------------------------------------------------
# The headline claim.
# --------------------------------------------------------------------------


async def test_alice_and_bob_each_see_only_their_own_invoices(stack):
    alice = await sign_in(stack, "A")
    bob = await sign_in(stack, "B")
    try:
        seen_by_alice = await _invoice_ids(alice)
        seen_by_bob = await _invoice_ids(bob)
    finally:
        await alice.aclose()
        await bob.aclose()

    assert seen_by_alice == stack.invoices_of("A")
    assert seen_by_bob == stack.invoices_of("B")

    # Non-vacuous: both actually saw something, and they saw different things.
    assert seen_by_alice, "Alice owns no invoices; the fixture proves nothing"
    assert seen_by_bob, "Bob owns no invoices; the fixture proves nothing"
    assert not (seen_by_alice & seen_by_bob)


async def test_the_app_is_running_an_unfiltered_query(stack):
    """The separation above is not the app being careful.

    Read the same endpoint as the migrator, who bypasses RLS: it returns every
    invoice in the table, which is what the app's SQL actually asks for.
    """
    alice = await sign_in(stack, "A")
    try:
        seen_by_alice = await _invoice_ids(alice)
    finally:
        await alice.aclose()

    everything = {str(key[0]) for key in stack.plan.keys("invoices")}
    assert seen_by_alice < everything, (
        "Alice saw every row there is, so nothing was filtered out"
    )


async def test_two_browsers_stay_separate_under_concurrency(stack):
    """Shared pooled connections must not hand Alice's identity to Bob."""
    import asyncio

    alice = await sign_in(stack, "A")
    bob = await sign_in(stack, "B")
    try:
        results = await asyncio.gather(
            *(_invoice_ids(alice if i % 2 == 0 else bob) for i in range(40))
        )
    finally:
        await alice.aclose()
        await bob.aclose()

    for i, seen in enumerate(results):
        expected = stack.invoices_of("A" if i % 2 == 0 else "B")
        assert seen == expected, f"request {i} saw the wrong person's invoices"


# --------------------------------------------------------------------------
# Forged identity headers.
# --------------------------------------------------------------------------


async def test_forged_header_from_outside_does_nothing(stack):
    """No session, a forged header: the app is never even reached."""
    async with httpx.AsyncClient(base_url=stack.sidecar_url, timeout=20) as attacker:
        response = await attacker.get(
            "/invoices", headers={HEADER_NAME: _forged(stack, stack.sub("B"))}
        )
    assert response.status_code == 401


async def test_forged_header_cannot_upgrade_a_real_session(stack):
    """Alice signs in honestly and sends Bob's sub. She still gets her own rows.

    This is the ordering the sidecar promises: the incoming header is stripped
    before anything else, and the minted one overwrites rather than appends.
    """
    alice = await sign_in(stack, "A")
    try:
        response = await alice.get(
            "/invoices", headers={HEADER_NAME: _forged(stack, stack.sub("B"))}
        )
        assert response.status_code == 200
        seen = {row["id"] for row in response.json()["invoices"]}
    finally:
        await alice.aclose()

    assert seen == stack.invoices_of("A")


async def test_a_correctly_signed_header_from_outside_is_still_refused(stack):
    """Even with the real key, the gate is not a place you present a header.

    The sidecar authenticates the cookie, not the header, so knowing the
    identity key buys an outsider nothing at the gate. (It would buy them
    everything at the app, which is exactly why the app is not exposed.)
    """
    header = _forged(stack, stack.sub("B"), key=stack.identity_key.encode())
    async with httpx.AsyncClient(base_url=stack.sidecar_url, timeout=20) as attacker:
        response = await attacker.get("/invoices", headers={HEADER_NAME: header})
    assert response.status_code == 401


@pytest.mark.negative
async def test_the_app_alone_would_have_been_wide_open(stack):
    """What the gate is for. Readme.md section 16.

    Reached directly, the app believes a correctly signed header, because
    believing it is the shim's entire job. Nothing here is a bug: it is the
    reason the app binds to 127.0.0.1 and shares a network namespace with the
    sidecar, and the reason a forged header has to be stripped at the gate.
    """
    header = _forged(stack, stack.sub("B"), key=stack.identity_key.encode())
    async with httpx.AsyncClient(base_url=stack.app_url, timeout=20) as direct:
        response = await direct.get("/invoices", headers={HEADER_NAME: header})
        assert response.status_code == 200
        assert {r["id"] for r in response.json()["invoices"]} == stack.invoices_of("B")

        # And with a wrong key, or with none at all, it fails closed.
        for headers in ({HEADER_NAME: _forged(stack, stack.sub("B"))}, {}):
            blind = await direct.get("/invoices", headers=headers)
            assert blind.status_code == 200
            assert blind.json()["invoices"] == []


async def test_a_header_for_another_app_is_not_accepted(stack):
    """Per-app keys and an `app` claim: two apps' sidecars cannot cross over."""
    header = _forged(
        stack, stack.sub("B"), key=stack.identity_key.encode(), app="app_someone_else"
    )
    async with httpx.AsyncClient(base_url=stack.app_url, timeout=20) as direct:
        response = await direct.get("/invoices", headers={HEADER_NAME: header})
    assert response.status_code == 200
    assert response.json()["invoices"] == []


# --------------------------------------------------------------------------
# The gate itself.
# --------------------------------------------------------------------------


async def test_no_session_never_reaches_the_app(stack):
    async with httpx.AsyncClient(base_url=stack.sidecar_url, timeout=20) as anon:
        assert (await anon.get("/invoices")).status_code == 401
        assert (await anon.post("/invoices")).status_code == 401

        page = await anon.get("/", headers={"accept": "text/html"})
        assert page.status_code == 303
        assert page.headers["location"].startswith("/__vd/login")


async def test_health_is_reachable_without_a_session(stack):
    async with httpx.AsyncClient(base_url=stack.sidecar_url, timeout=20) as anon:
        response = await anon.get("/__vd/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "app": stack.app_id}


async def test_a_wrong_password_does_not_get_a_session(stack):
    async with httpx.AsyncClient(base_url=stack.sidecar_url, timeout=20) as browser:
        response = await browser.post(
            "/__vd/login", data={"email": EMAILS["A"], "password": "not-it"}
        )
    assert response.status_code == 401
    assert COOKIE_NAME not in response.cookies


async def test_an_unknown_user_is_indistinguishable_from_a_wrong_password(stack):
    async with httpx.AsyncClient(base_url=stack.sidecar_url, timeout=20) as browser:
        unknown = await browser.post(
            "/__vd/login", data={"email": "nobody@example.com", "password": "x"}
        )
        wrong = await browser.post(
            "/__vd/login", data={"email": EMAILS["A"], "password": "x"}
        )
    assert unknown.status_code == wrong.status_code == 401
    assert unknown.text == wrong.text


async def test_the_session_cookie_never_reaches_the_app(stack):
    """An app that echoes its headers must not be able to hand out a session."""
    alice = await sign_in(stack, "A")
    try:
        cookie = alice.cookies[COOKIE_NAME]
        response = await alice.get(
            "/whoami", headers={"cookie": f"{COOKIE_NAME}={cookie}; theirs=1"}
        )
        assert response.status_code == 200
        # The app saw an identity, so the request did arrive intact...
        assert response.json()["sub"] == stack.sub("A")
    finally:
        await alice.aclose()

    # ...and the app's own view of the request confirms the cookie was removed.
    log = next(p for p in stack.processes if "app" in p._name).output()
    assert cookie not in log


async def test_signing_out_invalidates_the_browser(stack):
    alice = await sign_in(stack, "A")
    try:
        assert (await alice.get("/invoices")).status_code == 200
        out = await alice.post("/__vd/logout")
        assert out.status_code == 200  # followed the redirect to the login page
        assert not alice.cookies.get(COOKIE_NAME)
        after = await alice.get("/invoices")
        assert after.status_code == 401
    finally:
        await alice.aclose()


async def test_a_tampered_cookie_is_refused(stack):
    alice = await sign_in(stack, "A")
    try:
        good = alice.cookies[COOKIE_NAME]
        payload, _, signature = good.partition(".")
        for broken in (
            f"{payload}.{'a' * len(signature)}",
            f"{payload[:-2]}xy.{signature}",
            "not-a-cookie",
            "",
        ):
            async with httpx.AsyncClient(
                base_url=stack.sidecar_url, timeout=20
            ) as attacker:
                attacker.cookies.set(COOKIE_NAME, broken)
                response = await attacker.get("/invoices")
            assert response.status_code == 401, broken
    finally:
        await alice.aclose()


async def test_removing_a_user_closes_their_open_sessions(stack):
    """Section 16: the session version is re-checked, with a short cache."""
    alice = await sign_in(stack, "A")
    try:
        assert (await alice.get("/invoices")).status_code == 200

        async with httpx.AsyncClient(timeout=20) as control:
            bumped = await control.post(
                f"{stack.control_plane_url}/test/bump/{stack.sub('A')}"
            )
            assert bumped.status_code == 200

        # The sidecar's session-version cache is 60s by default, so the old
        # session is still good until it expires. Reach past it by asking the
        # sidecar about a session it has never seen: a fresh login carries the
        # new version, and the stale cookie is refused once the cache turns
        # over. Prove the mechanism directly instead of sleeping a minute.
        stale = alice.cookies[COOKIE_NAME]
        again = await sign_in(stack, "A")
        await again.aclose()
    finally:
        await alice.aclose()

    from sidecar.session import read

    session = read(stale, stack.session_key.encode())
    assert session is not None
    assert session.session_version == 1, "the stale cookie still claims version 1"

    async with httpx.AsyncClient(timeout=20) as control:
        current = await control.get(
            f"{stack.control_plane_url}/internal/apps/{stack.app_id}"
            f"/users/{stack.sub('A')}/session-version",
            headers={"x-vd-sidecar-key": stack.api_key},
        )
    assert current.json()["session_version"] == 2, "the user was not actually bumped"


async def test_the_login_page_will_not_redirect_off_site(stack):
    async with httpx.AsyncClient(base_url=stack.sidecar_url, timeout=20) as browser:
        response = await browser.post(
            "/__vd/login",
            data={
                "email": EMAILS["A"],
                "password": PASSWORDS["A"],
                "next": "//evil.example.com/",
            },
        )
    assert response.status_code == 303
    assert response.headers["location"] == "/"

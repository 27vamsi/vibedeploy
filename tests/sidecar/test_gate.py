"""What gets past the gate, and what the app is allowed to see of a request.

No upstream is reachable from these tests, so any request that got proxied would
raise rather than quietly pass.
"""

from __future__ import annotations

import time

import pytest
from vibedeploy_shim.identity import HEADER_NAME, verify_identity

from sidecar.app import _cookie_header_without_ours, _forwardable, _mint
from sidecar.session import COOKIE_NAME, Session, issue
from tests.sidecar.conftest import KEY, SESSION_KEY, config


def _cookie(sub="user-a", role="member", version=1, ttl=3600, key=SESSION_KEY):
    return issue(
        Session(sub=sub, role=role, session_version=version, exp=int(time.time()) + ttl),
        key.encode(),
    )


async def test_no_session_is_a_flat_401(browser):
    for method in ("get", "post", "put", "patch", "delete"):
        response = await getattr(browser, method)("/invoices")
        assert response.status_code == 401, method
        assert response.json() == {"error": "not signed in"}


async def test_a_browser_is_sent_to_the_login_page(browser):
    response = await browser.get("/invoices", headers={"accept": "text/html"})
    assert response.status_code == 303
    assert response.headers["location"] == "/__vd/login?next=/invoices"


async def test_a_browsers_post_is_not_redirected(browser):
    """Redirecting a POST loses the body; 401 is the honest answer."""
    response = await browser.post("/invoices", headers={"accept": "text/html"})
    assert response.status_code == 401


@pytest.mark.parametrize(
    "cookie",
    [
        _cookie(key="ff" * 32),           # signed with somebody else's key
        _cookie(ttl=-1),                  # expired
        "not-a-cookie",
        "",
    ],
)
async def test_a_cookie_we_did_not_issue_is_not_a_session(browser, cookie):
    browser.cookies.set(COOKIE_NAME, cookie)
    assert (await browser.get("/invoices")).status_code == 401


async def test_a_stale_session_version_is_not_a_session(browser, directory):
    browser.cookies.set(COOKIE_NAME, _cookie(version=1))
    directory.versions["user-a"] = 2
    assert (await browser.get("/invoices")).status_code == 401


async def test_a_directory_that_cannot_answer_is_a_refusal(browser, directory):
    """Section 3: unsure means deny. Not knowing is not a yes."""
    browser.cookies.set(COOKIE_NAME, _cookie())
    directory.reachable = False
    assert (await browser.get("/invoices")).status_code == 401


async def test_health_needs_nothing(browser):
    response = await browser.get("/__vd/health")
    assert response.status_code == 200
    assert response.json()["app"] == "app_test"


# --------------------------------------------------------------------------
# What is forwarded. These are the functions the proxy path is built from.
# --------------------------------------------------------------------------


def test_our_cookie_is_removed_and_the_others_are_kept():
    assert (
        _cookie_header_without_ours(f"theirs=1; {COOKIE_NAME}=secret; other=2")
        == "theirs=1; other=2"
    )
    assert _cookie_header_without_ours(f"{COOKIE_NAME}=secret") is None
    assert _cookie_header_without_ours(None) is None
    # A cookie whose name merely contains ours is somebody else's cookie.
    assert (
        _cookie_header_without_ours(f"not_{COOKIE_NAME}=1") == f"not_{COOKIE_NAME}=1"
    )


def test_an_incoming_identity_header_never_survives():
    forwarded = _forwardable(
        [
            (HEADER_NAME.encode(), b"forged"),
            (b"X-VD-Identity", b"forged in another case"),
            (b"accept", b"application/json"),
        ]
    )
    assert [name.lower() for name, _ in forwarded] == [b"accept"]


def test_hop_by_hop_and_reframed_headers_are_dropped():
    forwarded = _forwardable(
        [
            (b"connection", b"keep-alive"),
            (b"transfer-encoding", b"chunked"),
            (b"content-length", b"999"),
            (b"host", b"gate.test"),
            (b"content-type", b"application/json"),
        ]
    )
    assert forwarded == [(b"content-type", b"application/json")]


def test_the_minted_header_says_who_and_expires_in_a_minute():
    settings = config()
    header = _mint(Session("user-a", "member", 1, exp=0), settings)
    identity = verify_identity(header, KEY.encode(), app_id="app_test")
    assert identity is not None
    assert (identity.sub, identity.role) == ("user-a", "member")

    # Another app's shim, holding another key, gets nothing from it.
    assert verify_identity(header, b"cc" * 32, app_id="app_test") is None
    assert verify_identity(header, KEY.encode(), app_id="app_other") is None


def test_the_minted_header_is_not_the_session(browser):
    """The identity header lives 60s; the session lives 8h. Confusing them
    would hand the app a token good for the rest of the day."""
    settings = config()
    now = int(time.time())
    header = _mint(
        Session("user-a", "member", 1, exp=now + settings.session_ttl), settings
    )
    identity = verify_identity(header, KEY.encode(), app_id="app_test")
    assert identity is not None
    assert identity.exp - now <= settings.identity_ttl

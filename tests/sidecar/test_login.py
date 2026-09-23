"""The login half of the gate, at its real settings. Readme.md section 16."""

from __future__ import annotations

import pytest

from sidecar.config import LOGIN_EMAIL_LIMIT, LOGIN_IP_LIMIT
from sidecar.session import COOKIE_NAME, read
from tests.sidecar.conftest import PASSWORD, SESSION_KEY


async def _attempt(browser, email="alice@example.com", password=PASSWORD, **extra):
    return await browser.post(
        "/__vd/login", data={"email": email, "password": password, **extra}
    )


async def test_the_right_password_gets_a_session(browser):
    response = await _attempt(browser)
    assert response.status_code == 303
    assert response.headers["location"] == "/"

    session = read(browser.cookies[COOKIE_NAME], SESSION_KEY.encode())
    assert session is not None
    assert (session.sub, session.role, session.session_version) == (
        "user-a",
        "member",
        1,
    )


async def test_the_cookie_is_httponly_and_lax(browser):
    response = await _attempt(browser)
    raw = response.headers["set-cookie"].lower()
    assert "httponly" in raw
    assert "samesite=lax" in raw
    # Host-only: a Domain attribute would let a neighbouring subdomain be sent
    # this cookie.
    assert "domain=" not in raw


@pytest.mark.parametrize(
    "email,password",
    [
        ("alice@example.com", "wrong"),
        ("nobody@example.com", PASSWORD),
        ("", ""),
    ],
)
async def test_bad_credentials_get_no_session(browser, email, password):
    response = await _attempt(browser, email=email, password=password)
    assert response.status_code == 401
    assert COOKIE_NAME not in response.cookies


async def test_a_control_plane_that_is_down_is_a_refusal(browser, directory):
    directory.reachable = False
    response = await _attempt(browser)
    assert response.status_code == 401
    assert "Wrong email or password" in response.text


async def test_the_email_limit_is_five_attempts(gate, directory):
    async with gate() as browser:
        for attempt in range(LOGIN_EMAIL_LIMIT):
            response = await _attempt(browser, password="guess")
            assert "Too many attempts" not in response.text, attempt
        blocked = await _attempt(browser, password="guess")
    assert blocked.status_code == 401
    assert "Too many attempts" in blocked.text


async def test_the_limit_is_spent_before_the_password_is_looked_at(gate, directory):
    """A correct password after five wrong ones is still refused.

    Otherwise the response time, and the outcome, tell an attacker which
    guesses were close.
    """
    async with gate() as browser:
        for _ in range(LOGIN_EMAIL_LIMIT):
            await _attempt(browser, password="guess")
        before = directory.logins
        blocked = await _attempt(browser)
    assert "Too many attempts" in blocked.text
    assert directory.logins == before, "the control plane was asked anyway"


async def test_the_ip_limit_covers_a_list_of_accounts(gate):
    """Per-email alone would let one machine try every account once."""
    async with gate() as browser:
        for attempt in range(LOGIN_IP_LIMIT):
            response = await _attempt(browser, email=f"user{attempt}@example.com")
            assert "Too many attempts" not in response.text, attempt
        blocked = await _attempt(browser, email="one-more@example.com")
    assert "Too many attempts" in blocked.text


async def test_a_blocked_attacker_does_not_lock_out_a_different_email(gate):
    async with gate(login_ip_limit=10_000) as browser:
        for _ in range(LOGIN_EMAIL_LIMIT + 3):
            await _attempt(browser, email="victim@example.com", password="guess")
        response = await _attempt(browser)
    assert response.status_code == 303


@pytest.mark.parametrize(
    "asked,used",
    [
        ("/invoices", "/invoices"),
        ("//evil.example.com/", "/"),
        ("https://evil.example.com/", "/"),
        ("/__vd/login?next=/x", "/"),
        ("", "/"),
    ],
)
async def test_next_is_only_ever_a_path_on_this_site(browser, asked, used):
    response = await _attempt(browser, next=asked)
    assert response.status_code == 303
    assert response.headers["location"] == used


async def test_signing_out_clears_the_cookie(browser):
    await _attempt(browser)
    assert browser.cookies.get(COOKIE_NAME)
    response = await browser.post("/__vd/logout")
    assert response.status_code == 303
    assert response.headers["location"] == "/__vd/login"
    assert not browser.cookies.get(COOKIE_NAME)


async def test_the_login_page_is_reachable_without_a_session(browser):
    response = await browser.get("/__vd/login")
    assert response.status_code == 200
    assert "Sign in" in response.text


async def test_the_error_message_is_escaped(browser):
    """The only text on that page that anybody else chooses is `next`."""
    response = await browser.get("/__vd/login", params={"next": "/<script>x</script>"})
    assert "<script>" not in response.text

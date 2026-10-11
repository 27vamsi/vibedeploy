"""Who is allowed to drive the console.

Until now the dashboard guarded nothing: reaching the port was the whole of the
authorisation, and `routes._sole_builder` would invent an identity rather than
admit it had none. That is survivable on a laptop and indefensible anywhere
else, because this console creates apps, triggers deploys, adds the people an
app's RLS policies are written about, mints agent API keys and holds the global
agent kill switch.

So: a password checked by `control_plane.passwords` (the same argon2id path the
app's own people go through — one place in the product compares a password) and
a signed cookie carrying the builder's id.

Signed, not stored. A server-side session table would be a second thing to
migrate, expire and reconcile, and it buys nothing here: there is one operator
per installation and the only revocation we need is changing the secret. The
signature is HMAC-SHA256 over the payload with `VD_SESSION_SECRET`, which is
stdlib, so the console gains no dependency for it.

The CSRF token rides *inside* the signed payload rather than in a second cookie.
Every POST form must echo it back in a hidden field, and because the cookie is
`HttpOnly` and signed, a page on another origin can neither read the token nor
forge one. A forged cookie fails the signature before its contents are looked
at; a tampered one fails for the same reason.
"""

from __future__ import annotations

import base64
import hmac
import json
import secrets
import time
import uuid
from dataclasses import dataclass, field
from hashlib import sha256
from typing import Annotated, Any

from fastapi import Depends, HTTPException, Request, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from control_plane.api.deps import get_session
from control_plane.config import ConfigError
from control_plane.models import Builder
from control_plane.passwords import verify_password

COOKIE = "vd_session"
CSRF_FIELD = "csrf"

# Long enough to get a deploy done without logging in twice, short enough that a
# forgotten browser is not a standing key. There is no refresh-on-use: a session
# that outlives its window by being busy is a session with no expiry at all.
MAX_AGE = 12 * 60 * 60

# Login attempts per email per window. Not a defence against a botnet, which is
# what the proxy in front is for; it is what stops an unattended console from
# being a free password oracle.
ATTEMPTS = 5
WINDOW = 60.0


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


@dataclass(frozen=True)
class Operator:
    """The person this request is being made by. Never guessed."""

    builder_id: uuid.UUID
    email: str
    csrf: str


@dataclass
class Sessions:
    """Issues and reads the cookie. One of these per process.

    `secure` follows `VD_PUBLIC_SCHEME`: a `Secure` cookie over plain HTTP is
    simply never sent back, so it cannot be hardcoded on, and sending a session
    cookie in clear over a public hostname cannot be allowed either. The scheme
    is the one fact that decides it.
    """

    secret: str
    secure: bool
    max_age: int = MAX_AGE
    _attempts: dict[str, list[float]] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        if not self.secret:
            raise ConfigError(
                "VD_SESSION_SECRET is not set. The console refuses to start"
                " without it rather than sign its sessions with a default"
                " everybody already has."
            )

    # -- the signature ------------------------------------------------------

    def _mac(self, payload: bytes) -> str:
        return _b64(hmac.new(self.secret.encode(), payload, sha256).digest())

    def _seal(self, claims: dict[str, Any]) -> str:
        payload = json.dumps(claims, separators=(",", ":"), sort_keys=True).encode()
        return f"{_b64(payload)}.{self._mac(payload)}"

    def _open(self, token: str) -> dict[str, Any] | None:
        head, _, tail = token.partition(".")
        if not head or not tail:
            return None
        try:
            payload = _unb64(head)
        except (ValueError, TypeError):
            return None
        # compare_digest, so a wrong signature takes the same time as any other
        # wrong signature.
        if not hmac.compare_digest(self._mac(payload), tail):
            return None
        try:
            claims = json.loads(payload)
        except ValueError:
            return None
        if not isinstance(claims, dict):
            return None
        if float(claims.get("exp", 0)) < time.time():
            return None
        return claims

    # -- the cookie ---------------------------------------------------------

    def issue(self, response: Response, builder: Builder) -> Operator:
        operator = Operator(
            builder_id=builder.id, email=builder.email, csrf=secrets.token_urlsafe(24)
        )
        token = self._seal(
            {
                "sub": str(operator.builder_id),
                "email": operator.email,
                "csrf": operator.csrf,
                "exp": int(time.time()) + self.max_age,
            }
        )
        response.set_cookie(
            COOKIE,
            token,
            max_age=self.max_age,
            httponly=True,
            samesite="lax",
            secure=self.secure,
            path="/",
        )
        return operator

    def clear(self, response: Response) -> None:
        response.delete_cookie(COOKIE, path="/")

    def read(self, request: Request) -> Operator | None:
        token = request.cookies.get(COOKIE)
        if not token:
            return None
        claims = self._open(token)
        if claims is None:
            return None
        try:
            builder_id = uuid.UUID(str(claims.get("sub")))
        except ValueError:
            return None
        email, csrf = claims.get("email"), claims.get("csrf")
        if not isinstance(email, str) or not isinstance(csrf, str):
            return None
        return Operator(builder_id=builder_id, email=email, csrf=csrf)

    # -- the login throttle -------------------------------------------------

    def too_many(self, who: str) -> bool:
        now = time.monotonic()
        recent = [at for at in self._attempts.get(who, []) if now - at < WINDOW]
        self._attempts[who] = recent
        return len(recent) >= ATTEMPTS

    def record_attempt(self, who: str) -> None:
        self._attempts.setdefault(who, []).append(time.monotonic())

    def forget(self, who: str) -> None:
        self._attempts.pop(who, None)


def sessions(request: Request) -> Sessions:
    return request.app.state.sessions


class NotLoggedIn(HTTPException):
    """A browser is sent to the login page; anything else is told 401.

    Both are refusals. The redirect exists so that a person who bookmarked a
    deployment page lands somewhere useful, and it is only ever offered to a
    request that asked for HTML with a method that changes nothing.
    """

    def __init__(self, request: Request) -> None:
        wants_html = "text/html" in request.headers.get("accept", "")
        if request.method in ("GET", "HEAD") and wants_html:
            nxt = request.url.path
            super().__init__(
                status_code=303, headers={"location": f"/login?next={nxt}"}
            )
        else:
            super().__init__(status_code=401, detail="not logged in")


async def require_operator(
    request: Request, session: Annotated[AsyncSession, Depends(get_session)]
) -> Operator:
    """Every console route depends on this, directly or through the router.

    It does two jobs because they share the cookie they both need: it says who
    this is, and on anything that changes state it says the request came from
    our own page. Splitting them would mean reading and verifying the cookie
    twice.
    """
    operator = sessions(request).read(request)
    if operator is None:
        raise NotLoggedIn(request)

    # The cookie survives the builder being deleted, so the row is the
    # authority and not the token. Fail closed: no row, no session.
    builder = await session.get(Builder, operator.builder_id)
    if builder is None:
        raise NotLoggedIn(request)

    if request.method not in ("GET", "HEAD", "OPTIONS"):
        form = await request.form()
        sent = form.get(CSRF_FIELD)
        if not isinstance(sent, str) or not hmac.compare_digest(sent, operator.csrf):
            raise HTTPException(status_code=403, detail="that form is stale")

    # So that every template can print the token without every handler having
    # to pass it. See `web.routes._page`.
    request.state.operator = operator
    return operator


async def sole_builder(session: AsyncSession) -> Builder | None:
    """The one operator of this installation, if it has one yet.

    Returns `None` rather than inventing a builder. "Nobody has set this up"
    and "this is who set it up" are different answers and the console behaves
    differently for each: the first gets the setup page, the second gets a
    login form.
    """
    return (await session.execute(select(Builder).limit(1))).scalar_one_or_none()


async def check_password(session: AsyncSession, *, email: str, password: str) -> Builder | None:
    builder = (
        await session.execute(select(Builder).where(Builder.email == email.strip().lower()))
    ).scalar_one_or_none()
    if builder is None:
        return None
    if not verify_password(builder.password_hash, password):
        return None
    return builder

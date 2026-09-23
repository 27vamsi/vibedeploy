"""The browser session cookie. Readme.md section 16.

Signed, not encrypted: the contents are not secret, but they must be
unforgeable. The same `base64url(payload).base64url(hmac)` shape as the
identity header, and the same constant-time compare, because there is no reason
for two hand-rolled token formats in one product.

The cookie carries `session_version` so that removing a user can invalidate
every session they have open without keeping server-side session state.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from dataclasses import dataclass

from vibedeploy_shim.identity import b64url_decode, b64url_encode, canonical_json

# Host-only by construction: no Domain attribute is ever set, so the cookie
# cannot be read by a sibling app on a neighbouring subdomain.
COOKIE_NAME = "vd_session"

SUPPORTED_VERSION = 1


@dataclass(frozen=True)
class Session:
    sub: str
    role: str
    session_version: int
    exp: int


def issue(session: Session, key: bytes) -> str:
    payload = {
        "v": SUPPORTED_VERSION,
        "sub": session.sub,
        "role": session.role,
        "sv": session.session_version,
        "exp": session.exp,
    }
    body = canonical_json(payload)
    mac = hmac.new(key, body, hashlib.sha256).digest()
    return f"{b64url_encode(body)}.{b64url_encode(mac)}"


def read(cookie: str | None, key: bytes, *, now: int | None = None) -> Session | None:
    """The session a cookie proves, or None.

    None covers absent, malformed, wrongly signed, expired and unknown version.
    The caller cannot tell them apart and must not carry on with a partial
    result: every one of them means nobody is logged in.
    """
    if not cookie or not isinstance(cookie, str):
        return None

    part, _, signature = cookie.partition(".")
    if not part or not signature or "." in signature:
        return None

    try:
        body = b64url_decode(part)
        provided = b64url_decode(signature)
    except (ValueError, TypeError):
        return None

    expected = hmac.new(key, body, hashlib.sha256).digest()
    if not hmac.compare_digest(expected, provided):
        return None

    try:
        payload = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        return None
    if not isinstance(payload, dict) or payload.get("v") != SUPPORTED_VERSION:
        return None

    sub, role = payload.get("sub"), payload.get("role")
    version, exp = payload.get("sv"), payload.get("exp")
    if not isinstance(sub, str) or not sub:
        return None
    if not isinstance(role, str):
        return None
    if not isinstance(version, int) or isinstance(version, bool):
        return None
    if not isinstance(exp, int) or isinstance(exp, bool):
        return None

    current = int(time.time()) if now is None else now
    if exp <= current:
        return None

    return Session(sub=sub, role=role, session_version=version, exp=exp)

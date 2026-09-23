"""The identity header and the per-request identity. Readme.md sections 7.1, 14.

    X-VD-Identity: base64url(payload) "." base64url(HMAC_SHA256(payload, key))

Everything here fails closed. `verify_identity` returns None for anything it is
not completely sure about, and None means both settings are written as '' at the
next transaction, which under RLS means zero rows.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import logging
import time
from contextvars import ContextVar, Token
from dataclasses import dataclass

log = logging.getLogger("vibedeploy_shim")

HEADER_NAME = "x-vd-identity"

# Only version 1 exists. An unknown version is refused rather than parsed on a
# best-effort basis, because a future version may change what a field means.
SUPPORTED_VERSION = 1

# No clock skew allowance on exp. The sidecar and the app share a network
# namespace (Readme.md section 16), so they share a clock, and any slack here
# would only widen the replay window.


@dataclass(frozen=True)
class Identity:
    sub: str
    role: str
    app: str
    exp: int


_current: ContextVar[Identity | None] = ContextVar(
    "vibedeploy_identity", default=None
)


def current_identity() -> Identity | None:
    return _current.get()


def set_current_identity(identity: Identity | None) -> Token:
    return _current.set(identity)


def reset_current_identity(token: Token) -> None:
    """Always called in a finally, so one request can never inherit another's
    identity from a reused worker task."""
    _current.reset(token)


def b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def b64url_decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding)


def canonical_json(payload: dict) -> bytes:
    """Both sides must serialise identically or the HMAC will not match."""
    return json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()


def sign_identity(payload: dict, key: bytes) -> str:
    """Used by the sidecar. Kept beside the verifier so the two cannot drift."""
    body = canonical_json(payload)
    mac = hmac.new(key, body, hashlib.sha256).digest()
    return f"{b64url_encode(body)}.{b64url_encode(mac)}"


def verify_identity(
    header: str | None,
    key: bytes,
    *,
    app_id: str | None = None,
    now: int | None = None,
) -> Identity | None:
    """Return the identity a header proves, or None.

    None covers every failure: absent, malformed, wrongly signed, expired, wrong
    app, unknown version. The caller must not be able to tell these apart, and
    must not be tempted to carry on with a partial result.
    """
    if not header or not isinstance(header, str):
        return None

    part, _, signature = header.partition(".")
    if not part or not signature or "." in signature:
        return None

    try:
        body = b64url_decode(part)
        provided_mac = b64url_decode(signature)
    except (binascii.Error, ValueError):
        return None

    expected_mac = hmac.new(key, body, hashlib.sha256).digest()
    if not hmac.compare_digest(expected_mac, provided_mac):
        return None

    try:
        payload = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        return None
    if not isinstance(payload, dict):
        return None

    if payload.get("v") != SUPPORTED_VERSION:
        return None

    sub = payload.get("sub")
    app = payload.get("app")
    exp = payload.get("exp")
    if not isinstance(sub, str) or not sub:
        return None
    if not isinstance(app, str) or not app:
        return None
    if not isinstance(exp, int) or isinstance(exp, bool):
        return None

    if app_id is not None and app != app_id:
        return None

    current = int(time.time()) if now is None else now
    if exp <= current:
        return None

    role = payload.get("role")
    if not isinstance(role, str):
        role = ""

    return Identity(sub=sub, role=role, app=app, exp=exp)

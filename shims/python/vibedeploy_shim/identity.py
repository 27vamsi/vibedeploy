"""The X-VD-Identity contract. Readme.md section 7.1.

Everything here fails closed. A missing, malformed, unsigned, expired or
unknown-version header produces no identity, and no identity means the shim
sets an empty `app.user_id`, which means the database returns zero rows.

The header value is:

    base64url(payload_json) + "." + base64url(HMAC_SHA256(payload_json, KEY))

Section 7.1 fixes the payload and says the key is 32 random bytes but does not
say how those bytes travel in an environment variable. We encode them the same
way as the header: base64url, no padding. The sidecar must match.
"""

from __future__ import annotations

import base64
import binascii
import hmac
import json
import os
import time
from contextvars import ContextVar, Token
from dataclasses import dataclass
from hashlib import sha256
from typing import Any, Mapping

HEADER_NAME = "X-VD-Identity"
HEADER_NAME_BYTES = b"x-vd-identity"
KEY_ENV = "VD_IDENTITY_KEY"
VERSION = 1
TTL_SECONDS = 60
ROLES = frozenset({"member", "admin"})


@dataclass(frozen=True)
class Identity:
    app: str
    sub: str
    role: str


_current: ContextVar[Identity | None] = ContextVar("vibedeploy_identity", default=None)


def current() -> Identity | None:
    return _current.get()


def set_current(value: Identity | None) -> Token:
    return _current.set(value)


def reset_current(token: Token) -> None:
    _current.reset(token)


def b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def b64url_decode(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def load_key(env: Mapping[str, str] | None = None) -> bytes | None:
    """Returns None rather than raising: a keyless shim still fails closed."""
    raw = (env if env is not None else os.environ).get(KEY_ENV)
    if not raw:
        return None
    try:
        key = b64url_decode(raw)
    except (binascii.Error, ValueError):
        return None
    return key or None


def sign(payload: Mapping[str, Any], key: bytes) -> str:
    """Used by the sidecar to mint headers, and by tests to forge them."""
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    signature = hmac.new(key, raw, sha256).digest()
    return f"{b64url_encode(raw)}.{b64url_encode(signature)}"


def make_header(
    *, app: str, sub: str, role: str, key: bytes, now: float | None = None
) -> str:
    issued = int(time.time() if now is None else now)
    return sign(
        {
            "v": VERSION,
            "app": app,
            "sub": sub,
            "role": role,
            "iat": issued,
            "exp": issued + TTL_SECONDS,
        },
        key,
    )


def verify(
    header_value: str | bytes | None,
    key: bytes | None,
    *,
    now: float | None = None,
    app: str | None = None,
) -> Identity | None:
    """Every rejection path returns None. Nothing here raises."""
    if not key or not header_value:
        return None

    if isinstance(header_value, bytes):
        try:
            header_value = header_value.decode("ascii")
        except UnicodeDecodeError:
            return None

    encoded_payload, sep, encoded_signature = header_value.partition(".")
    if not sep or "." in encoded_signature:
        return None

    try:
        payload_bytes = b64url_decode(encoded_payload)
        signature = b64url_decode(encoded_signature)
    except (binascii.Error, ValueError):
        return None

    expected = hmac.new(key, payload_bytes, sha256).digest()
    if not hmac.compare_digest(expected, signature):
        return None

    try:
        payload = json.loads(payload_bytes)
    except (ValueError, UnicodeDecodeError):
        return None
    if not isinstance(payload, dict):
        return None

    # Shims reject unknown versions rather than guessing at the payload shape.
    if payload.get("v") != VERSION:
        return None

    expires = payload.get("exp")
    if not isinstance(expires, (int, float)) or isinstance(expires, bool):
        return None
    if (time.time() if now is None else now) >= expires:
        return None

    app_id, sub, role = payload.get("app"), payload.get("sub"), payload.get("role")
    if not isinstance(app_id, str) or not app_id:
        return None
    if not isinstance(sub, str) or not sub:
        return None
    if role not in ROLES:
        return None
    if app is not None and app_id != app:
        return None

    return Identity(app=app_id, sub=sub, role=role)

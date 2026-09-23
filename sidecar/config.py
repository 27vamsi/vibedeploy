"""Everything the sidecar is told, and nothing it decides for itself.

Keys arrive as hex from Secrets Manager (Readme.md section 18.1). They are
per-app: one app's sidecar can never mint a header another app's shim accepts,
because it does not have that app's key.
"""

from __future__ import annotations

import binascii
import os
from dataclasses import dataclass
from typing import Mapping

# Readme.md section 7.1: the identity header lives for 60 seconds. It is minted
# fresh per request, so this only bounds how long a captured one is worth.
IDENTITY_TTL = 60

# Section 16: an 8 hour browser session.
SESSION_TTL = 8 * 60 * 60

# Section 16: removed users are noticed within a minute.
SESSION_VERSION_TTL = 60

# Section 16: "Login rate limit per IP and per email." Both allowances are
# settable because both have legitimate reasons to be wrong for a deployment:
# every browser can arrive from one address (a corporate NAT, a test harness on
# loopback), and a shared account can be signed into from many places. The
# windows are not settable, because widening them is the only way to make these
# numbers meaningless, and nobody should be able to do that by accident.
LOGIN_IP_LIMIT, LOGIN_IP_WINDOW = 20, 60.0
LOGIN_EMAIL_LIMIT, LOGIN_EMAIL_WINDOW = 5, 300.0


class ConfigError(RuntimeError):
    """The sidecar cannot start safely, so it must not start at all."""


def _required(env: Mapping[str, str], name: str) -> str:
    value = env.get(name)
    if not value:
        raise ConfigError(
            f"{name} is not set. The sidecar refuses to start without it"
            " rather than fall back to a default nobody chose."
        )
    return value


def _key(env: Mapping[str, str], name: str) -> bytes:
    """Checked as hex, used as the literal bytes of the variable.

    The shim HMACs `os.environ["VD_IDENTITY_KEY"].encode()` and must stay that
    simple: it runs before the app and is deliberately incapable of failing.
    So the sidecar signs over the same bytes. The hex form is still required,
    because that is what section 18.1 puts in Secrets Manager and it is how we
    know the secret carries its full 256 bits rather than being a short
    passphrase somebody typed.
    """
    raw = _required(env, name)
    try:
        decoded = bytes.fromhex(raw)
    except (ValueError, binascii.Error) as exc:
        raise ConfigError(f"{name} must be hex") from exc
    if len(decoded) < 32:
        raise ConfigError(f"{name} must be at least 32 bytes of hex")
    return raw.encode("ascii")


@dataclass(frozen=True)
class SidecarConfig:
    app_id: str
    identity_key: bytes
    session_key: bytes
    control_plane_url: str
    sidecar_api_key: str
    upstream: str = "http://127.0.0.1:3000"
    cookie_secure: bool = True
    session_ttl: int = SESSION_TTL
    identity_ttl: int = IDENTITY_TTL
    session_version_ttl: int = SESSION_VERSION_TTL
    login_ip_limit: int = LOGIN_IP_LIMIT
    login_email_limit: int = LOGIN_EMAIL_LIMIT

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "SidecarConfig":
        env = os.environ if env is None else env
        return cls(
            app_id=_required(env, "VD_APP_ID"),
            identity_key=_key(env, "VD_IDENTITY_KEY"),
            session_key=_key(env, "VD_SESSION_KEY"),
            control_plane_url=_required(env, "VD_CONTROL_PLANE_URL").rstrip("/"),
            sidecar_api_key=_required(env, "VD_SIDECAR_API_KEY"),
            upstream=env.get("VD_UPSTREAM", "http://127.0.0.1:3000"),
            # Defaults to on. Local HTTP has to ask for it to be off, so that
            # forgetting to set it can only ever make the cookie stricter.
            cookie_secure=env.get("VD_COOKIE_SECURE", "1") != "0",
            login_ip_limit=int(env.get("VD_LOGIN_IP_LIMIT") or LOGIN_IP_LIMIT),
            login_email_limit=int(
                env.get("VD_LOGIN_EMAIL_LIMIT") or LOGIN_EMAIL_LIMIT
            ),
        )

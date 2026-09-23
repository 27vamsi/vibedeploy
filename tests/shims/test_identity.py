"""The identity header contract, Readme.md section 7.1.

    X-VD-Identity: base64url(payload) "." base64url(HMAC_SHA256(payload, key))

This header is the only thing standing between "the sidecar says this is Alice"
and the database. Every malformed, stale or wrongly-signed variant must produce
*no* identity rather than a partial one, because no identity means zero rows.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time

import pytest

from vibedeploy_shim.identity import Identity, sign_identity, verify_identity

KEY = b"a" * 32
OTHER_KEY = b"b" * 32
APP = "app_123"


def b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def make_header(payload: dict, key: bytes = KEY) -> str:
    body = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    mac = hmac.new(key, body, hashlib.sha256).digest()
    return f"{b64(body)}.{b64(mac)}"


def payload(**overrides) -> dict:
    now = int(time.time())
    base = {
        "v": 1,
        "app": APP,
        "sub": "8f14e45f-ea1a-4f1e-9a1b-2c3d4e5f6a7b",
        "role": "member",
        "iat": now,
        "exp": now + 60,
    }
    base.update(overrides)
    return base


def test_a_well_formed_header_verifies():
    ident = verify_identity(make_header(payload()), KEY, app_id=APP)
    assert isinstance(ident, Identity)
    assert ident.sub == "8f14e45f-ea1a-4f1e-9a1b-2c3d4e5f6a7b"
    assert ident.role == "member"


def test_sign_and_verify_round_trip():
    """The sidecar signs with the same function the shim verifies with."""
    header = sign_identity(payload(), KEY)
    assert verify_identity(header, KEY, app_id=APP) is not None


def test_a_header_signed_with_another_key_is_rejected():
    """The per-app key is what stops one app forging another's identities."""
    assert verify_identity(make_header(payload(), OTHER_KEY), KEY, app_id=APP) is None


def test_a_tampered_payload_is_rejected():
    """Swap the subject for someone else's and the signature no longer matches."""
    good = make_header(payload())
    forged_body = b64(
        json.dumps(payload(sub="somebody-else"), separators=(",", ":"), sort_keys=True).encode()
    )
    tampered = f"{forged_body}.{good.split('.')[1]}"
    assert verify_identity(tampered, KEY, app_id=APP) is None


def test_an_expired_header_is_rejected():
    now = int(time.time())
    stale = payload(iat=now - 600, exp=now - 1)
    assert verify_identity(make_header(stale), KEY, app_id=APP) is None


def test_an_unknown_version_is_rejected():
    """Unknown v is refused rather than best-effort parsed."""
    assert verify_identity(make_header(payload(v=2)), KEY, app_id=APP) is None


def test_a_header_for_another_app_is_rejected():
    assert verify_identity(make_header(payload(app="app_999")), KEY, app_id=APP) is None


@pytest.mark.parametrize(
    "header",
    [
        "",
        ".",
        "onlyonepart",
        "too.many.parts",
        "!!!notbase64!!!.abc",
        "e30.abc",  # valid base64, empty JSON object, no required fields
    ],
)
def test_malformed_headers_are_rejected(header):
    assert verify_identity(header, KEY, app_id=APP) is None


def test_a_missing_header_is_rejected():
    assert verify_identity(None, KEY, app_id=APP) is None


def test_a_payload_missing_a_subject_is_rejected():
    body = payload()
    del body["sub"]
    assert verify_identity(make_header(body), KEY, app_id=APP) is None


def test_an_empty_subject_is_rejected():
    """An empty sub would render as '' and read as 'no identity' downstream."""
    assert verify_identity(make_header(payload(sub="")), KEY, app_id=APP) is None


def test_verification_uses_a_constant_time_compare(monkeypatch):
    """Signature comparison must not leak timing. Readme.md section 7.1."""
    calls = []
    real = hmac.compare_digest

    def spy(a, b):
        calls.append((a, b))
        return real(a, b)

    monkeypatch.setattr("vibedeploy_shim.identity.hmac.compare_digest", spy)
    verify_identity(make_header(payload()), KEY, app_id=APP)
    assert calls, "expected hmac.compare_digest to be used"

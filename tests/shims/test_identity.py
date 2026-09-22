"""The X-VD-Identity contract, rejection by rejection. Readme.md section 7.1.

Every one of these must produce no identity, because no identity is what makes
`vd_user_id()` NULL, and NULL is what makes the database return zero rows.
"""

from __future__ import annotations

import json
import time

import pytest

from vibedeploy_shim.identity import (
    KEY_ENV,
    Identity,
    b64url_decode,
    b64url_encode,
    load_key,
    make_header,
    sign,
    verify,
)

KEY = b"\x01" * 32
OTHER_KEY = b"\x02" * 32


def header(**overrides) -> str:
    payload = {
        "v": 1,
        "app": "app_123",
        "sub": "8c0e",
        "role": "member",
        "iat": int(time.time()),
        "exp": int(time.time()) + 60,
    }
    payload.update(overrides)
    return sign(payload, KEY)


def test_a_well_formed_header_yields_the_identity():
    assert verify(header(), KEY) == Identity(app="app_123", sub="8c0e", role="member")


def test_bytes_headers_work_because_asgi_hands_us_bytes():
    assert verify(header().encode("ascii"), KEY) is not None


def test_exp_is_sixty_seconds_after_iat():
    raw = json.loads(b64url_decode(make_header(
        app="app_123", sub="8c0e", role="member", key=KEY, now=1726900000
    ).split(".")[0]))
    assert raw["iat"] == 1726900000
    assert raw["exp"] == 1726900060


@pytest.mark.parametrize(
    "value",
    [
        None,
        "",
        "not-a-header",
        "onlyonepart",
        "a.b.c",
        "!!!.???",
        header() + "x",
        header().split(".")[0] + ".",
    ],
    ids=[
        "none", "empty", "no-dot", "one-part", "three-parts",
        "not-base64", "tampered", "no-signature",
    ],
)
def test_malformed_headers_are_rejected(value):
    assert verify(value, KEY) is None


def test_a_header_signed_with_another_key_is_rejected():
    """A forged header is the whole reason the signature exists."""
    assert verify(header(), OTHER_KEY) is None


def test_a_swapped_payload_with_a_valid_signature_is_rejected():
    payload = json.dumps({"v": 1, "app": "app_123", "sub": "attacker",
                          "role": "admin", "iat": 0, "exp": 9e9}).encode()
    good = header()
    assert verify(f"{b64url_encode(payload)}.{good.split('.')[1]}", KEY) is None


def test_an_expired_header_is_rejected():
    now = time.time()
    assert verify(header(iat=now - 120, exp=now - 60), KEY) is None


def test_expiry_is_exclusive_at_the_boundary():
    assert verify(header(exp=1000), KEY, now=1000) is None
    assert verify(header(exp=1000), KEY, now=999) is not None


@pytest.mark.parametrize("version", [0, 2, "1", None])
def test_unknown_versions_are_rejected(version):
    """Shims reject versions they do not understand rather than guessing."""
    assert verify(header(v=version), KEY) is None


@pytest.mark.parametrize("role", ["", "root", "Admin", None, 1])
def test_unknown_roles_are_rejected(role):
    assert verify(header(role=role), KEY) is None


@pytest.mark.parametrize("sub", ["", None, 5, {"a": 1}])
def test_a_missing_or_non_string_sub_is_rejected(sub):
    assert verify(header(sub=sub), KEY) is None


def test_a_header_for_another_app_is_rejected_when_the_app_is_known():
    assert verify(header(app="app_999"), KEY, app="app_123") is None
    assert verify(header(app="app_123"), KEY, app="app_123") is not None


def test_no_key_means_no_identity():
    """A shim that never got its key must not accept anything."""
    assert verify(header(), None) is None
    assert verify(header(), b"") is None


@pytest.mark.parametrize("value", ["", "!!!not base64!!!"])
def test_a_missing_or_unusable_key_env_var_loads_as_none(value):
    assert load_key({KEY_ENV: value}) is None
    assert load_key({}) is None


def test_the_key_env_var_round_trips():
    assert load_key({KEY_ENV: b64url_encode(KEY)}) == KEY

"""The two halves of Phase B: what the installation refuses to start without,
and what an app's credentials look like on disk.

Nothing here needs a database, Docker or a web server. That is deliberate — a
test that answers "is this safe to put on the internet" should not be one that
only runs when the stack is up.

The test worth reading twice is `test_the_password_is_not_on_the_disk`. Every
other test in this file would still pass if `put` wrote the plaintext and
`get` read it back, because round-tripping is exactly what plaintext does
best. That one is what makes the encryption non-vacuous.
"""

from __future__ import annotations

import json

import pytest

from control_plane import secrets as secret_store
from control_plane.config import (
    DEFAULT_APP_DSN,
    DEFAULT_BUILD_DSN,
    DEFAULT_CONTROL_URL,
    ConfigError,
    ControlPlaneConfig,
)
from control_plane.secrets import SecretNotFound, SecretStore, SecretUnreadable, secret_name

KEY = "a-key-that-only-this-installation-has"

# Everything a production installation is required to carry. Each of these is
# something that has a working default on a laptop, and each of those defaults
# is either a password that is in this repository or an empty string that means
# "no lock".
PROD = {
    "VD_ENV": "prod",
    "VD_CONTROL_DATABASE_URL": "postgresql+asyncpg://u:p@db/control",
    "VD_APP_ADMIN_DSN": "postgresql://u:p@db/apps",
    "VD_BUILD_DATABASE_URL": "postgresql://u:p@buildpg/build",
    "VD_SIDECAR_API_KEY": "sidecar-key",
    "VD_SESSION_SECRET": "session-secret",
    "VD_SECRET_KEY": KEY,
    "VD_GATEWAY_API_KEY": "gateway-key",
    "VD_GITHUB_WEBHOOK_SECRET": "webhook-secret",
}

REQUIRED = sorted(name for name in PROD if name != "VD_ENV")


# ---------------------------------------------------------------------------
# What prod refuses to start without
# ---------------------------------------------------------------------------


def test_a_full_environment_starts():
    """The control. Without this, the tests below could be passing because
    `from_env` raises for some reason that has nothing to do with the switch."""
    config = ControlPlaneConfig.from_env(PROD)
    assert config.env == "prod"
    assert config.secret_key == KEY
    assert config.database_url == PROD["VD_CONTROL_DATABASE_URL"]


@pytest.mark.parametrize("missing", REQUIRED)
def test_in_prod_every_secret_is_required_and_says_which(missing):
    env = {name: value for name, value in PROD.items() if name != missing}
    with pytest.raises(ConfigError) as raised:
        ControlPlaneConfig.from_env(env)
    # Naming itself matters: "the control plane refuses to start" with no
    # further detail is a message an operator cannot act on.
    assert missing in str(raised.value)


def test_a_laptop_keeps_its_defaults():
    """`VD_ENV` unset is the laptop, and the laptop's defaults are the reason
    the suite does not need fifteen environment variables. If this breaks,
    everything breaks."""
    config = ControlPlaneConfig.from_env(
        {"VD_SIDECAR_API_KEY": "k", "VD_SESSION_SECRET": "s"}
    )
    assert config.env == "local"
    assert config.database_url == DEFAULT_CONTROL_URL
    assert config.app_admin_dsn == DEFAULT_APP_DSN
    assert config.build_dsn == DEFAULT_BUILD_DSN
    # And the ones with no default have none, rather than quietly becoming the
    # laptop's.
    assert config.secret_key == ""
    assert config.gateway_api_key == ""
    assert config.github_webhook_secret == ""


def test_the_session_secret_is_required_even_on_a_laptop():
    """The one exception to "the laptop gets defaults". A shared signing key is
    not a weaker login, it is no login."""
    with pytest.raises(ConfigError) as raised:
        ControlPlaneConfig.from_env({"VD_SIDECAR_API_KEY": "k"})
    assert "VD_SESSION_SECRET" in str(raised.value)


def test_the_key_can_come_from_a_file(tmp_path):
    """So that it need not appear in a process listing or a `docker inspect`.
    The trailing newline an editor leaves is not part of the key."""
    path = tmp_path / "kek"
    path.write_text(KEY + "\n", encoding="utf-8")
    env = {**PROD, "VD_SECRET_KEY_FILE": str(path)}
    del env["VD_SECRET_KEY"]
    assert ControlPlaneConfig.from_env(env).secret_key == KEY


def test_the_scheme_decides_the_cookie():
    assert ControlPlaneConfig.from_env(PROD).cookies_secure is False
    assert (
        ControlPlaneConfig.from_env({**PROD, "VD_PUBLIC_SCHEME": "https"}).cookies_secure
        is True
    )


def test_the_store_is_built_from_the_configured_key(tmp_path):
    config = ControlPlaneConfig.from_env(
        {**PROD, "VD_STATE_ROOT": str(tmp_path)}
    )
    store = secret_store.store_for(config)
    assert store.key == KEY
    assert store.root == tmp_path / "secrets"


# ---------------------------------------------------------------------------
# What the file on disk looks like
# ---------------------------------------------------------------------------

APP = "app_c0ffee01"
RUNTIME = secret_name(APP, secret_store.RUNTIME)
PASSWORD = "correct-horse-battery-staple-2f9a1c"


@pytest.fixture
def store(tmp_path):
    return SecretStore(tmp_path / "secrets", key=KEY)


def _on_disk(store: SecretStore, name: str) -> str:
    return (store.root.joinpath(*name.split("/")).with_suffix(".json")).read_text(
        encoding="utf-8"
    )


def test_a_secret_comes_back_as_it_went_in(store):
    store.put(RUNTIME, {"user": "app_c0ffee01_runtime", "password": PASSWORD})
    assert store.get(RUNTIME) == {
        "user": "app_c0ffee01_runtime",
        "password": PASSWORD,
    }


def test_the_password_is_not_on_the_disk(store):
    """The test that makes the rest of this file mean something."""
    store.put(RUNTIME, {"user": "app_c0ffee01_runtime", "password": PASSWORD})
    body = _on_disk(store, RUNTIME)
    assert PASSWORD not in body
    assert "password" not in body, "even the key names are inside the envelope"
    # And it is recognisably an envelope rather than some other encoding that
    # happens not to contain the word.
    assert json.loads(body)[secret_store.ENVELOPE] == 1


def test_a_ciphertext_moved_from_another_app_is_not_readable(store):
    """The secret's name is the AAD, and this is what that buys.

    Without it, copying one app's `agent` file over another's would make the
    gateway hand an agent working for Alice the credentials for a database
    that is not hers — and nothing would report an error, because the
    ciphertext decrypts perfectly well. It is the *name* that does not match.
    """
    theirs = secret_name("app_someoneelse", secret_store.AGENT)
    mine = secret_name(APP, secret_store.AGENT)
    store.put(theirs, {"password": PASSWORD})
    stolen = _on_disk(store, theirs)

    path = store.root.joinpath(*mine.split("/")).with_suffix(".json")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(stolen, encoding="utf-8")

    with pytest.raises(SecretUnreadable):
        store.get(mine)


def test_the_wrong_key_is_a_refusal_not_a_guess(store):
    store.put(RUNTIME, {"password": PASSWORD})
    other = SecretStore(store.root, key="the-key-from-a-different-installation")
    with pytest.raises(SecretUnreadable):
        other.get(RUNTIME)


def test_no_key_at_all_cannot_read_a_sealed_secret(store):
    """Fail closed. "We cannot decrypt this" must never become "here is
    nothing" — a deploy that proceeds with no password is worse than one that
    stops."""
    store.put(RUNTIME, {"password": PASSWORD})
    with pytest.raises(SecretUnreadable):
        SecretStore(store.root).get(RUNTIME)


def test_a_damaged_file_is_a_refusal(store):
    store.put(RUNTIME, {"password": PASSWORD})
    path = store.root.joinpath(*RUNTIME.split("/")).with_suffix(".json")
    body = json.loads(path.read_text(encoding="utf-8"))
    body["ct"] = body["ct"][:-2] + ("00" if not body["ct"].endswith("00") else "11")
    path.write_text(json.dumps(body), encoding="utf-8")
    with pytest.raises(SecretUnreadable):
        store.get(RUNTIME)


def test_a_missing_secret_is_a_different_answer(store):
    """`SecretNotFound` and `SecretUnreadable` are not interchangeable: the
    first is "this app has not been provisioned yet", which several callers
    handle, and the second is "stop"."""
    with pytest.raises(SecretNotFound):
        store.get(RUNTIME)


def test_a_laptop_that_never_had_a_key_still_works(tmp_path):
    plain = SecretStore(tmp_path / "secrets")
    plain.put(RUNTIME, {"password": PASSWORD})
    assert plain.get(RUNTIME) == {"password": PASSWORD}
    assert PASSWORD in _on_disk(plain, RUNTIME), "nothing to decrypt, nothing hidden"


def test_a_laptop_that_gains_a_key_seals_as_it_reads(tmp_path):
    """There is no migration command, because nobody would run it. The first
    read after the key arrives rewrites the file sealed."""
    root = tmp_path / "secrets"
    SecretStore(root).put(RUNTIME, {"password": PASSWORD})
    assert PASSWORD in _on_disk(SecretStore(root), RUNTIME)

    now_keyed = SecretStore(root, key=KEY)
    assert now_keyed.get(RUNTIME) == {"password": PASSWORD}
    assert PASSWORD not in _on_disk(now_keyed, RUNTIME)
    # And it is still readable afterwards, by the sealed path this time.
    assert now_keyed.get(RUNTIME) == {"password": PASSWORD}


def test_merging_into_a_secret_leaves_it_sealed(store):
    """`update` is how an app's own settings land next to its password
    (`service.set_app_env`). A merge that wrote the result in clear would undo
    the encryption for every app that has one variable set."""
    store.put(RUNTIME, {"password": PASSWORD})
    merged = store.update(RUNTIME, {"FEATURE_FLAG": "1"})
    assert merged == {"password": PASSWORD, "FEATURE_FLAG": "1"}
    assert store.get(RUNTIME) == merged
    assert PASSWORD not in _on_disk(store, RUNTIME)


def test_settings_can_wait_for_a_password_that_has_not_arrived(store):
    """A builder sets their variables before the app is provisioned, which is
    the order a person would use."""
    assert store.update(RUNTIME, {"FEATURE_FLAG": "1"}) == {"FEATURE_FLAG": "1"}
    assert store.get(RUNTIME) == {"FEATURE_FLAG": "1"}

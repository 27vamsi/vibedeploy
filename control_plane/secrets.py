"""Where credentials live, which is never the control plane's database.

Readme.md section 18.1 gives every app four secrets under `vd/apps/<app_id>/`:
`runtime`, `sidecar`, `migrator` and `agent`. Splitting them that way is what
lets section 21 give the gateway read access to `agent-*` and nothing else.

Locally that is a directory of files instead of Secrets Manager. The names and
the shape are identical, so M8 swaps the backend and nothing else.

What is *in* those files is now encrypted. Mode 0600 is a real protection and
it is not the only one worth having: it protects nothing in a backup, nothing
in a snapshot, nothing in a stray `tar` of the state root and nothing against
any process running as the same user — which, before the app runtime was put in
a container, included every deployed app. So each file holds an AES-256-GCM
envelope under a key that lives outside the state root entirely
(`VD_SECRET_KEY`, or a file named by `VD_SECRET_KEY_FILE`).

The secret's own name is the AAD. That is the part worth more than the
encryption: without it, a ciphertext could be copied from one app's `agent`
secret over another app's, and the reader would decrypt it happily and hand an
agent credentials for a database that is not its own. With it, the move is
detected as tampering.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import TYPE_CHECKING, Any, Mapping

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

if TYPE_CHECKING:
    from control_plane.config import ControlPlaneConfig

RUNTIME = "runtime"
SIDECAR = "sidecar"
MIGRATOR = "migrator"
AGENT = "agent"

# Marks a file as an envelope rather than the bare JSON this used to write.
# A name nothing else could plausibly be: an app's own settings end up in these
# files too (`service.set_app_env`), and a builder is allowed to name a
# variable almost anything, so "does it have a `ct` key" is not a safe question
# to decide a file's format on.
ENVELOPE = "__vd_envelope__"


class SecretNotFound(KeyError):
    pass


class SecretUnreadable(RuntimeError):
    """The file is there and will not open.

    A wrong key, a truncated file, or a ciphertext that was moved from another
    secret. Never recovered from by falling back to something: a secret that
    cannot be read is a deploy that stops, not a deploy that proceeds without
    a password.
    """


def secret_name(app_id: str, kind: str) -> str:
    return f"vd/apps/{app_id}/{kind}"


@dataclass(frozen=True)
class SecretStore:
    root: Path
    # The key-encryption key. Empty means "write these in clear", which is only
    # reachable on a laptop: `config._secret_key` makes `VD_SECRET_KEY`
    # mandatory as soon as `VD_ENV=prod`.
    key: str = ""

    def _path(self, name: str) -> Path:
        # The name is built by `secret_name` from an app id that
        # kernel.render.validate_app_id has already restricted to
        # `app_[a-z0-9_]`, so no component can climb out of the root.
        return self.root.joinpath(*name.split("/")).with_suffix(".json")

    def _cipher(self) -> AESGCM:
        # SHA-256 of whatever was configured, so the key may be a passphrase, a
        # 32-byte hex string or a base64 blob and all of them work. This is not
        # a password hash and does not need to be: the input is a machine
        # secret with full entropy, not something a person chose.
        return AESGCM(sha256(self.key.encode()).digest())

    def _write(self, path: Path, body: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        # Opened 0600 before anything is in it, so the window where a secret
        # exists at default permissions never opens. Still worth doing with the
        # contents encrypted: it is the cheapest of the two protections and the
        # only one that applies to the file's existence.
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(body)

    def put(self, name: str, value: Mapping[str, Any]) -> None:
        plain = json.dumps(dict(value)).encode()
        if not self.key:
            self._write(self._path(name), plain.decode())
            return
        nonce = os.urandom(12)
        sealed = self._cipher().encrypt(nonce, plain, name.encode())
        self._write(
            self._path(name),
            json.dumps(
                {
                    ENVELOPE: 1,
                    "alg": "AES-256-GCM",
                    "nonce": nonce.hex(),
                    "ct": sealed.hex(),
                }
            ),
        )

    def get(self, name: str) -> dict[str, Any]:
        path = self._path(name)
        if not path.exists():
            raise SecretNotFound(name)
        body = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(body, dict) or ENVELOPE not in body:
            # Written before this installation had a key, by a laptop that
            # still has none, or by one that has just been given one. The last
            # case is the interesting one: the file is rewritten sealed on the
            # way out, so a laptop that gains a key converts as it is used
            # rather than needing a migration nobody would run.
            if self.key:
                self.put(name, body)
            return body
        if not self.key:
            raise SecretUnreadable(
                f"{name} is encrypted and VD_SECRET_KEY is not set"
            )
        try:
            opened = self._cipher().decrypt(
                bytes.fromhex(body["nonce"]), bytes.fromhex(body["ct"]), name.encode()
            )
        except (InvalidTag, ValueError) as exc:
            raise SecretUnreadable(
                f"{name} will not decrypt: wrong VD_SECRET_KEY, or the file was"
                " changed or moved from another secret"
            ) from exc
        return json.loads(opened)

    def update(self, name: str, fields: Mapping[str, Any]) -> dict[str, Any]:
        """Merge `fields` into a secret, creating it if it is not there yet.

        A builder can set their app's variables before anything has provisioned
        it, which is the order a person would use, so there may be no secret to
        merge into. The variables wait in it for the password to arrive.
        """
        try:
            current = self.get(name)
        except SecretNotFound:
            current = {}
        merged = {**current, **fields}
        self.put(name, merged)
        return merged


def store_for(config: ControlPlaneConfig) -> SecretStore:
    """The one place the local store's location and key are decided."""
    return SecretStore(config.state_root / "secrets", key=config.secret_key)

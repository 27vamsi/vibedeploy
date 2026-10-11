"""Reading a push from GitHub, without trusting it. Readme.md section 15.

Nothing here touches the database, which is the point: deciding whether a
request really came from GitHub, and what it is asking for, is pure work over
bytes that arrived from a stranger. The handler in `api/hooks.py` is then short
enough to read in one go, and every judgement it makes is tested here directly.

Two things are worth saying about the signature. It is computed over the exact
bytes that arrived rather than over a re-serialised payload, because anything
that round-trips through `json` can differ from what was signed and then the
check is of our own output. And it is compared with `hmac.compare_digest`,
because a normal `==` on a hex digest returns at the first wrong character and
that timing is enough to recover the right one a character at a time.

A secret that is not set means "reject everything". The other choice — treating
an unset secret as "no checking" — is an endpoint that queues builds for anybody
who finds the URL, and it would look exactly like working.
"""

from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass
from typing import Any, Mapping

GITHUB = "github"

# GitHub's own header names. Starlette matches them case-insensitively.
EVENT_HEADER = "X-GitHub-Event"
DELIVERY_HEADER = "X-GitHub-Delivery"
SIGNATURE_HEADER = "X-Hub-Signature-256"

PUSH = "push"
BRANCH_PREFIX = "refs/heads/"

# What one delivery turned out to mean. Fixed strings because they are written
# to a row a builder reads when they ask why their push did nothing, and
# because the tests assert on them rather than on prose.
DEPLOYED = "deployed"
ALREADY_HANDLED = "already_handled"
NOT_A_PUSH = "not_a_push"
NOT_A_BRANCH = "not_a_branch"
BRANCH_DELETED = "branch_deleted"
NOT_TRACKED = "ref_not_tracked"
NO_SUCH_APP = "no_app_for_repository"
REFUSED = "refused"

NOT_JSON = "That body is not JSON, so there is nothing to read in it."
NO_DELIVERY_ID = (
    "That request carries no delivery id, so a retry of it could not be told"
    " apart from a second push."
)


def verify(secret: str, body: bytes, presented: str | None) -> bool:
    """Is this really GitHub? Fails closed on a missing secret or signature."""
    if not secret or not presented:
        return False
    expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, presented.strip())


def repo_key(repo: str) -> str:
    """One name for one repository, however the builder wrote it down.

    A builder pastes whatever GitHub's green button gave them — an HTTPS URL, an
    SSH URL, `owner/name` — and the push payload names the same repository in
    three more ways. Comparing them as strings would match none of them, so both
    sides are reduced to `owner/name` first.

    A local path is left alone rather than squeezed into that shape. Most of
    this project's own fixtures are folders, no push is ever about one, and
    taking their last two segments would make `/a/repos/todo` and
    `/b/repos/todo` the same repository.
    """
    value = repo.strip().replace("\\", "/").rstrip("/").lower()
    if _is_a_path(value):
        return value

    if "://" in value:
        after_scheme = value.split("://", 1)[1]
        value = after_scheme.split("/", 1)[1] if "/" in after_scheme else after_scheme
    elif "@" in value and ":" in value:
        # `git@github.com:owner/name.git`, which is not a URL at all.
        value = value.split(":", 1)[1]

    if value.endswith(".git"):
        value = value[: -len(".git")]
    parts = [part for part in value.split("/") if part]
    return "/".join(parts[-2:])


def _is_a_path(value: str) -> bool:
    return (
        value.startswith(("/", "."))
        or (len(value) > 1 and value[1] == ":" and value[0].isalpha())
    )


@dataclass(frozen=True)
class Push:
    """What a push payload says, reduced to the four things we act on."""

    branch: str | None
    head: str | None
    deleted: bool
    repos: frozenset[str]

    @property
    def ref(self) -> str | None:
        return self.branch


def parse_push(payload: Mapping[str, Any]) -> Push:
    """Read a push payload. Anything missing comes back as "we cannot act".

    A tag push, a branch deletion and a payload from a GitHub we do not
    recognise all arrive here, and none of them is an error: they are deliveries
    this product has nothing to do about, and the caller records them as such.
    """
    ref = str(payload.get("ref") or "")
    branch = ref[len(BRANCH_PREFIX) :] if ref.startswith(BRANCH_PREFIX) else None
    repository = payload.get("repository")
    repository = repository if isinstance(repository, Mapping) else {}
    named = [
        repository.get("full_name"),
        repository.get("clone_url"),
        repository.get("ssh_url"),
        repository.get("git_url"),
        repository.get("html_url"),
    ]
    return Push(
        branch=branch or None,
        head=str(payload.get("after")) if payload.get("after") else None,
        deleted=bool(payload.get("deleted")),
        repos=frozenset(repo_key(str(name)) for name in named if name),
    )

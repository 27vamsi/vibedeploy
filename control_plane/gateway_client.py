"""How the console talks to the gateway. Readme.md 19.4, 19.7, 23.

The console shows approvals, activity, the audit chain and the evidence report,
and it computes none of them. It asks, over HTTP, with one key. The reason is
the rule in CLAUDE.md: `vd/apps/*/agent-*` belongs to the gateway and to
nothing else, and approving an action redoes the dry run against the app's
database. A console that did that itself would need the secret, and then the
split into two processes with two roles would be decoration.

Everything here fails **soft**, not closed, and the distinction matters. A
gateway that is not answering is not permission to do anything — no action is
approved, no switch is thrown, nothing changes. It only means a page cannot be
filled in, so the page says so instead of returning a 500 that tells a builder
nothing. The fail-closed decisions all live on the other side of this call.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

import httpx

KEY_HEADER = "X-VD-Console-Key"
TIMEOUT = 30.0


class GatewayDown(RuntimeError):
    """The gateway did not answer, so this page cannot be filled in.

    Carries a sentence a builder can read. Never carries the gateway's own
    error body: a refusal from there is either our key being wrong, which is
    our problem to fix, or something about an app we should not be relaying.
    """


class GatewayClient:
    def __init__(self, *, base_url: str, api_key: str):
        self._base = base_url.rstrip("/")
        self._key = api_key

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=f"{self._base}/internal",
            headers={KEY_HEADER: self._key},
            timeout=TIMEOUT,
        )

    async def _ask(self, method: str, path: str, **kwargs: Any) -> Any:
        try:
            async with self._client() as client:
                answer = await client.request(method, path, **kwargs)
        except httpx.HTTPError as exc:
            raise GatewayDown(
                "The gateway is not answering, so this cannot be shown right"
                " now. Nothing has changed."
            ) from exc
        if answer.status_code == 403:
            raise GatewayDown(
                "The gateway would not accept this console's key, so this"
                " cannot be shown right now. Nothing has changed."
            )
        if answer.status_code >= 400:
            raise GatewayDown(
                "The gateway could not answer that, so nothing has changed."
            )
        return answer.json()

    # -- 19.4 ----------------------------------------------------------------

    async def pending(self, app_uuid: uuid.UUID) -> list[dict[str, Any]]:
        return await self._ask("GET", f"/apps/{app_uuid}/pending")

    async def activity(self, app_uuid: uuid.UUID, *, limit: int = 50) -> list[dict[str, Any]]:
        return await self._ask(
            "GET", f"/apps/{app_uuid}/activity", params={"limit": limit}
        )

    async def decide(self, action_id: str, *, verdict: str, approver: str) -> dict[str, Any]:
        """`verdict` is one of approve, reject, undo.

        Deliberately not three methods: the three differ only in the word in
        the URL, and the gateway is what decides whether any of them is
        allowed. Three wrappers here would read like three decisions.
        """
        if verdict not in ("approve", "reject", "undo"):
            raise GatewayDown("That is not something that can be decided.")
        return await self._ask(
            "POST", f"/actions/{action_id}/{verdict}", json={"approver": approver}
        )

    # -- 19.6, 19.7 ----------------------------------------------------------

    async def audit(self) -> dict[str, Any]:
        return await self._ask("GET", "/audit")

    async def evidence(
        self,
        app_uuid: uuid.UUID,
        *,
        since: datetime | None = None,
        until: datetime | None = None,
    ) -> dict[str, Any]:
        params = {}
        if since is not None:
            params["since"] = since.isoformat()
        if until is not None:
            params["until"] = until.isoformat()
        return await self._ask("GET", f"/apps/{app_uuid}/evidence", params=params)

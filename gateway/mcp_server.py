"""The MCP front door. Readme.md section 21, section 8 M13.

Section 21's last line is the specification for this whole file: "MCP is just
another front door." Nothing here decides anything. There is no policy check, no
risk score, no audit write and no SQL in this module — every one of those would
be a second copy of a rule that already has one in `gateway.pipeline`, and the
second copy is the one that drifts. What this file does is three things:

  1. Turn `Authorization: Bearer vd_agent_...` into a gateway session, before
     the MCP protocol layer has seen a byte. An unauthenticated request never
     reaches a handler, including `initialize`.
  2. Hand `tools/list` whatever `Gateway.tools` says, which is already filtered
     by the agent's policy. A forbidden tool is therefore not listed here
     because it was never listed anywhere — this file has no list of its own to
     forget to filter.
  3. Hand `tools/call` to `Gateway.call`, and shape the `Outcome` into JSON.

The one tool that exists only here is `get_action_status`, because waiting for a
person to approve something is a transport concern: over REST an agent polls
`GET /v1/actions/<id>`, and over MCP there is no GET, so it has to be a tool.
It is read-only and scoped to the calling agent.

**Transport sessions and gateway sessions are not the same thing.** MCP gives
the client an `Mcp-Session-Id` on initialize; we keep our own map from that to
the gateway session opened with the bearer key at the same moment, along with a
fingerprint of that key. A request arriving on somebody else's transport session
id with its own key is refused rather than adopted, so a guessed session id buys
nothing. None of that is a security boundary on its own: every call still goes
through `sessions.authenticate`, which re-checks all six of 19.1's switches
against the database.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from typing import Any

from mcp import types
from mcp.server.lowlevel import Server
from starlette.datastructures import Headers

from gateway.errors import Denied
from gateway.pipeline import Gateway

NAME = "vibedeploy"
PATH = "/mcp"
STATUS_TOOL = "get_action_status"
PENDING = "pending_approval"

_SPEC = types.Tool(
    name=STATUS_TOOL,
    description=(
        "Look up one action you asked for earlier. Use this to wait for a"
        " person to approve or reject something."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "action_id": {
                "type": "string",
                "description": "The action_id you were given when the action was parked.",
            }
        },
        "required": ["action_id"],
        "additionalProperties": False,
    },
)


@dataclass(frozen=True)
class Bound:
    """One transport session's worth of "who is this". Section 21: bound to
    agent + app + acts-for, which is exactly what a `Caller` is."""

    fingerprint: str
    session_id: uuid.UUID
    agent_id: uuid.UUID
    app_uuid: uuid.UUID


# ---------------------------------------------------------------------------
# The handlers
# ---------------------------------------------------------------------------


def _bound(context: Any) -> Bound:
    """Who the middleware said this is.

    A handler that cannot find it does not guess: there is no "default agent",
    and a request that reached here without passing the middleware is a request
    we do not understand.
    """
    request = context.request
    found = request.scope.get("state", {}).get("vd") if request is not None else None
    if not isinstance(found, Bound):
        raise Denied("no_session", "This session is not open. Start a new one with your key.")
    return found


def _reply(outcome: Any) -> dict[str, Any]:
    """Section 21: "either the result, or `{status, action_id}`"."""
    if outcome.status == PENDING:
        return {
            "status": PENDING,
            "action_id": str(outcome.action_id),
            "risk": outcome.risk,
            "affected": outcome.affected,
            "reason": (
                "A person has to approve this before it happens. Call"
                f" {STATUS_TOOL} with this action_id to find out what they decided."
            ),
        }
    return outcome.as_json()


def _content(payload: dict[str, Any], *, ok: bool) -> types.CallToolResult:
    return types.CallToolResult(
        content=[types.TextContent(text=json.dumps(payload, default=str))],
        structured_content=payload,
        is_error=not ok,
    )


def build(
    gateway: Gateway,
    *,
    json_response: bool = True,
    extra_routes: list[Any] | None = None,
) -> Any:
    """The ASGI app to serve. `POST /mcp`, Streamable HTTP.

    `extra_routes` is how the gateway's internal API gets mounted beside this
    one. It goes here rather than the other way round because the Streamable
    HTTP session manager is started by this app's lifespan, and a Starlette
    `Mount` does not run a mounted app's lifespan — so the MCP app has to be
    the host, or the transport never starts.
    """
    server: Server[None] = Server(
        NAME,
        instructions=(
            "Tools here act for one person, with exactly that person's access."
            " Anything that changes data may need their approval first."
        ),
        on_list_tools=lambda context, params: _list_tools(gateway, context),
        on_call_tool=lambda context, params: _call_tool(gateway, context, params),
    )
    return _Authenticated(
        server.streamable_http_app(
            streamable_http_path=PATH,
            json_response=json_response,
            custom_starlette_routes=extra_routes,
        ),
        gateway,
    )


async def _list_tools(gateway: Gateway, context: Any) -> types.ListToolsResult:
    """Only what the policy leaves open, plus the one way to ask about waiting.

    `Gateway.tools` has already dropped every forbidden tool, and it
    authenticates again on the way, so an agent revoked since initialize is
    told its session is not open rather than handed a menu.
    """
    caller = _bound(context)
    listed = [
        types.Tool(
            name=spec["name"],
            description=spec["description"],
            input_schema=spec["input_schema"],
        )
        for spec in await gateway.tools(caller.session_id)
    ]
    return types.ListToolsResult(tools=[*listed, _SPEC])


async def _call_tool(
    gateway: Gateway, context: Any, params: types.CallToolRequestParams
) -> types.CallToolResult:
    caller = _bound(context)
    args = dict(params.arguments or {})

    if params.name == STATUS_TOOL:
        try:
            return _content(
                await gateway.status_of(caller.session_id, args.get("action_id")),
                ok=True,
            )
        except Denied as refusal:
            return _content({"status": "denied", "reason": refusal.reason}, ok=False)

    # Everything else is the pipeline's business, including whether it exists.
    # `Gateway.call` never raises: 19.2 turns every failure into a refusal, and
    # a refusal is a result the agent is allowed to read.
    outcome = await gateway.call(caller.session_id, params.name, args)
    return _content(_reply(outcome), ok=outcome.ok or outcome.status == PENDING)


# ---------------------------------------------------------------------------
# The front door itself
# ---------------------------------------------------------------------------


class _Authenticated:
    """Bearer key in, gateway session out, before the MCP layer runs.

    This is deliberately raw ASGI rather than a `BaseHTTPMiddleware`: the
    Streamable HTTP transport streams its responses, and wrapping it in
    something that buffers them would break the transport to save three lines.
    """

    def __init__(self, inner: Any, gateway: Gateway):
        self._inner = inner
        self._gateway = gateway
        self._bound: dict[str, Bound] = {}

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] != "http" or scope["path"] != PATH:
            # Lifespan, which is how the session manager is started, and the
            # internal API, which the console reaches with its own key rather
            # than with an agent's. An agent key would be the wrong credential
            # there: approving is a person's act, not an agent's.
            await self._inner(scope, receive, send)
            return

        headers = Headers(scope=scope)
        key = _bearer(headers.get("authorization"))
        if key is None:
            await _refuse(send, 401, "Send your agent key as a bearer token.")
            return
        fingerprint = hashlib.sha256(key.encode()).hexdigest()

        transport = headers.get("mcp-session-id")
        caller = self._bound.get(transport) if transport else None
        if caller is not None and caller.fingerprint != fingerprint:
            # Somebody else's transport session. Not adopted, not explained.
            await _refuse(send, 404, "This session is not open.")
            return

        if caller is None:
            try:
                opened = await self._gateway.open_session(key)
            except Denied as refusal:
                await _refuse(send, 401, refusal.reason)
                return
            except Exception:
                await _refuse(send, 401, "That key does not work.")
                return
            caller = Bound(
                fingerprint=fingerprint,
                session_id=opened.session_id,
                agent_id=opened.agent_id,
                app_uuid=opened.app_uuid,
            )
            if transport:
                self._bound[transport] = caller

        scope.setdefault("state", {})["vd"] = caller

        if transport is None:
            # The initialize that is about to be answered is where MCP hands out
            # the transport session id, so that is where it gets bound to the
            # gateway session just opened for it.
            await self._inner(scope, receive, _remember(send, self._bound, caller))
            return

        await self._inner(scope, receive, send)
        if scope["method"] == "DELETE":
            self._bound.pop(transport, None)


def _remember(send: Any, known: dict[str, Bound], caller: Bound) -> Any:
    async def watched(message: Any) -> None:
        if message["type"] == "http.response.start":
            for name, value in message.get("headers", ()):
                if name.lower() == b"mcp-session-id":
                    known[value.decode()] = caller
        await send(message)

    return watched


def _bearer(header: str | None) -> str | None:
    if not header:
        return None
    scheme, _, value = header.partition(" ")
    if scheme.lower() != "bearer" or not value.strip():
        return None
    return value.strip()


async def _refuse(send: Any, status: int, reason: str) -> None:
    """A refusal before the protocol starts, so it is HTTP, not JSON-RPC."""
    body = json.dumps({"error": reason}).encode()
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode()),
                (b"www-authenticate", b'Bearer realm="vibedeploy"'),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})

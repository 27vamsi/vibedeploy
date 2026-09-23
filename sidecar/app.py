"""The gate. Readme.md section 16.

Every request that is not one of our own three routes goes through exactly one
path:

    1. Strip any incoming `X-VD-Identity`. Always, before anything else, so a
       forged one cannot survive even if everything below is buggy.
    2. Read our session cookie. No valid session, no proxy at all.
    3. Strip our cookie out of `Cookie`, so the app never sees it and cannot
       replay it.
    4. Mint a fresh 60 second identity header and forward.

Step 1 happens whether or not the request is going to be proxied, and step 4
overwrites rather than appends, so there is no ordering in which a caller's own
header reaches the app.

The app behind this is untrusted. It is assumed to be an AI-written CRUD app
that will happily run `SELECT * FROM invoices`. Nothing here tries to stop it;
the database does that. This only decides who the request is for.
"""

from __future__ import annotations

import contextlib
import html
import logging
import time
from typing import Iterable

import httpx
from starlette.applications import Starlette
from starlette.background import BackgroundTask
from starlette.requests import Request
from starlette.responses import (
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    Response,
    StreamingResponse,
)
from starlette.routing import Route
from vibedeploy_shim.identity import HEADER_NAME, sign_identity

from sidecar.config import LOGIN_EMAIL_WINDOW, LOGIN_IP_WINDOW, SidecarConfig
from sidecar.directory import Directory, Principal
from sidecar.ratelimit import RateLimiter
from sidecar.session import COOKIE_NAME, Session, issue, read

log = logging.getLogger("vibedeploy_sidecar")

LOGIN_PATH = "/__vd/login"
LOGOUT_PATH = "/__vd/logout"
HEALTH_PATH = "/__vd/health"

# Per RFC 7230 these describe one hop and must not be copied onto the next one.
HOP_BY_HOP = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
    }
)


def _cookie_header_without_ours(raw: str | None) -> str | None:
    """Everything the browser sent except our session cookie.

    The app has no business seeing it. If it could, an app that echoes headers
    back — a debug endpoint, an error page — would hand out a working session.
    """
    if not raw:
        return None
    kept = [
        part
        for part in raw.split(";")
        if part.strip().split("=", 1)[0].strip() != COOKIE_NAME
    ]
    joined = "; ".join(p.strip() for p in kept if p.strip())
    return joined or None


# Dropped because the proxy re-frames the body itself: httpx decides how to
# describe what it is actually sending, and a stale length is a smuggling bug.
REFRAMED = frozenset({"host", "content-length"})


def _forwardable(headers: Iterable[tuple[bytes, bytes]]) -> list[tuple[bytes, bytes]]:
    out: list[tuple[bytes, bytes]] = []
    for key, value in headers:
        name = key.decode("latin-1").lower()
        if name in HOP_BY_HOP or name in REFRAMED or name == HEADER_NAME:
            continue
        if name == "cookie":
            cleaned = _cookie_header_without_ours(value.decode("latin-1"))
            if cleaned is None:
                continue
            out.append((key, cleaned.encode("latin-1")))
            continue
        out.append((key, value))
    return out


_LOGIN_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>Sign in</title></head>
<body>
<h1>Sign in</h1>
{error}
<form method="post" action="{action}">
  <label>Email <input type="email" name="email" autocomplete="username"></label>
  <label>Password <input type="password" name="password"
     autocomplete="current-password"></label>
  <input type="hidden" name="next" value="{next}">
  <button type="submit">Sign in</button>
</form>
</body></html>
"""


def _safe_next(value: str | None) -> str:
    """Only same-site paths. An open redirect on the login page is a phishing
    tool with our domain name on it."""
    if not value or not value.startswith("/") or value.startswith("//"):
        return "/"
    if value.startswith(LOGIN_PATH):
        return "/"
    return value


def _login_page(*, error: str = "", next_to: str = "/") -> HTMLResponse:
    body = _LOGIN_PAGE.format(
        error=f"<p role='alert'>{html.escape(error)}</p>" if error else "",
        action=LOGIN_PATH,
        next=html.escape(_safe_next(next_to), quote=True),
    )
    return HTMLResponse(body, status_code=401 if error else 200)


def _set_cookie(response: Response, value: str, config: SidecarConfig) -> None:
    response.set_cookie(
        COOKIE_NAME,
        value,
        max_age=config.session_ttl,
        path="/",
        httponly=True,
        secure=config.cookie_secure,
        samesite="lax",
        # No `domain`: host-only, so a neighbouring app on another subdomain
        # can never be sent this cookie.
    )


def create_app(config: SidecarConfig, directory: Directory | None = None) -> Starlette:
    directory = directory or Directory(
        config.control_plane_url,
        config.sidecar_api_key,
        config.app_id,
        ttl=config.session_version_ttl,
    )
    upstream = httpx.AsyncClient(base_url=config.upstream, timeout=httpx.Timeout(30.0))
    by_ip = RateLimiter(config.login_ip_limit, LOGIN_IP_WINDOW)
    by_email = RateLimiter(config.login_email_limit, LOGIN_EMAIL_WINDOW)

    async def health(request: Request) -> Response:
        """Unauthenticated on purpose: the load balancer has no session."""
        return JSONResponse({"status": "ok", "app": config.app_id})

    async def login(request: Request) -> Response:
        if request.method == "GET":
            return _login_page(next_to=request.query_params.get("next", "/"))

        form = await request.form()
        email = str(form.get("email") or "").strip().lower()
        password = str(form.get("password") or "")
        next_to = _safe_next(str(form.get("next") or "/"))

        client = request.client.host if request.client else "unknown"
        # Both limits are consumed before the password is looked at, so a
        # wrong guess and a right one cost an attacker the same.
        if not by_ip.allow(client) or not by_email.allow(email):
            return _login_page(
                error="Too many attempts. Try again in a few minutes.",
                next_to=next_to,
            )

        principal = await directory.login(email, password)
        if principal is None:
            # One message for wrong password, unknown user and a control plane
            # that did not answer. Telling them apart is an account oracle.
            return _login_page(error="Wrong email or password.", next_to=next_to)

        response = RedirectResponse(next_to, status_code=303)
        _set_cookie(response, _issue_for(principal, config), config)
        return response

    async def logout(request: Request) -> Response:
        response = RedirectResponse(LOGIN_PATH, status_code=303)
        response.delete_cookie(
            COOKIE_NAME, path="/", httponly=True, secure=config.cookie_secure,
            samesite="lax",
        )
        return response

    async def proxy(request: Request) -> Response:
        session = await _authenticate(request, config, directory)
        if session is None:
            return _unauthenticated(request, config)

        headers = _forwardable(request.headers.raw)
        headers.append(
            (
                HEADER_NAME.encode("latin-1"),
                _mint(session, config).encode("latin-1"),
            )
        )

        url = httpx.URL(
            path=request.url.path, query=request.url.query.encode("latin-1")
        )
        # Streamed rather than buffered, so a large upload does not have to fit
        # in the gate's memory. Methods that cannot carry a body are sent
        # without one rather than as an empty chunked stream.
        body = None if request.method in ("GET", "HEAD", "OPTIONS") else request.stream()
        built = upstream.build_request(
            request.method, url, headers=headers, content=body
        )
        try:
            response = await upstream.send(built, stream=True)
        except httpx.HTTPError:
            return JSONResponse({"error": "the app did not respond"}, 502)

        proxied = StreamingResponse(
            response.aiter_raw(),
            status_code=response.status_code,
            background=BackgroundTask(response.aclose),
        )
        # Assigned rather than passed in, because Starlette's `headers` argument
        # is a mapping and an app is allowed to send the same header twice —
        # several `Set-Cookie`s, most obviously. `aiter_raw` hands back the body
        # exactly as it arrived, so the upstream's own framing headers stay
        # true.
        proxied.raw_headers = [
            (key, value)
            for key, value in response.headers.raw
            if key.decode("latin-1").lower() not in HOP_BY_HOP
        ]
        return proxied

    @contextlib.asynccontextmanager
    async def lifespan(_):
        yield
        await upstream.aclose()
        await directory.aclose()

    app = Starlette(
        lifespan=lifespan,
        routes=[
            Route(HEALTH_PATH, health, methods=["GET"]),
            Route(LOGIN_PATH, login, methods=["GET", "POST"]),
            Route(LOGOUT_PATH, logout, methods=["GET", "POST"]),
            Route("/{path:path}", proxy, methods=[
                "GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS",
            ]),
        ],
    )
    app.state.config = config
    app.state.directory = directory
    log.info("vibedeploy-sidecar active app=%s", config.app_id)
    return app


def _issue_for(principal: Principal, config: SidecarConfig) -> str:
    return issue(
        Session(
            sub=principal.sub,
            role=principal.role,
            session_version=principal.session_version,
            exp=int(time.time()) + config.session_ttl,
        ),
        config.session_key,
    )


def _mint(session: Session, config: SidecarConfig) -> str:
    """A fresh header per request. Readme.md section 7.1."""
    now = int(time.time())
    return sign_identity(
        {
            "v": 1,
            "app": config.app_id,
            "sub": session.sub,
            "role": session.role,
            "iat": now,
            "exp": now + config.identity_ttl,
        },
        config.identity_key,
    )


async def _authenticate(
    request: Request, config: SidecarConfig, directory: Directory
) -> Session | None:
    session = read(request.cookies.get(COOKIE_NAME), config.session_key)
    if session is None:
        return None
    # Section 16: a removed user's sessions stop working, within the cache
    # window. `None` here means we could not check, which is not a yes.
    current = await directory.session_version(session.sub)
    if current is None or current != session.session_version:
        return None
    return session


def _unauthenticated(request: Request, config: SidecarConfig) -> Response:
    """A browser gets the login page; anything else gets a flat 401.

    The app is never reached either way. A gate that proxies anonymous requests
    and leaves it to the database is not a gate, even though the database would
    in fact return nothing.
    """
    accept = request.headers.get("accept", "")
    if "text/html" in accept and request.method in ("GET", "HEAD"):
        target = f"{LOGIN_PATH}?next={request.url.path}"
        return RedirectResponse(target, status_code=303)
    return JSONResponse({"error": "not signed in"}, status_code=401)

"""The one way in. Readme.md section 16.

    browser -> Caddy :55080 -> sidecar -> app -> Postgres

An app is published at `<subdomain>.localhost` and at no other address. The
sidecar's port exists, because locally these are processes on a laptop rather
than a shared network namespace, but nothing ever hands it out: an app's
address is a hostname the proxy knows about, so "reachable only through its
login gate" does not rest on nobody guessing a port.

Three decisions worth the words:

**The route table is written, not configured.** There are no site blocks in
`infra/local/Caddyfile`. Every route is POSTed over the admin API from what this
database says is live, which is what lets the reconciler *re-assert* the proxy
rather than trust it. A file on disk that also claimed to say where traffic
goes would be a second answer to a single-valued question.

**`apply` sends the whole table, never a patch.** A write of one route is an
instruction ("add this"); a write of all of them is a statement ("this is what
is live"). Only the second one converges: a route for an app that was deleted,
or for a generation that lost the fence, disappears because it is absent from
the next statement, and nobody has to have remembered to remove it.

**The route carries the same three labels as every other external resource.**
`@id` is `vd|<app_id>|<generation>|<deployment_id>`, so a route can be traced
back to the pass of the deployment that asked for it without asking the worker
that asked — see `control_plane/generations.py`. Caddy will only hold one object
per `@id`, which is also what stops the same generation being published twice.

Caddy is itself in a container, so an upstream of `127.0.0.1:54321` would mean
Caddy's own loopback. `container.reachable_from_container` is what makes it mean
this laptop.
"""

from __future__ import annotations

from dataclasses import dataclass

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from control_plane.config import ControlPlaneConfig
from control_plane.models import App, Deployment
from worker.pipeline import container
from worker.runtimes import serving

# One named server, so that `apply` can replace its entire route table without
# touching anything else somebody has put in Caddy.
SERVER = "vd"


def _listen(config: ControlPlaneConfig) -> list[str]:
    """What the server we write listens on, inside Caddy's container.

    The host-side port it is published on is `config.app_port`, and the two are
    deliberately different: see infra/local/docker-compose.yml for why 80 is
    not available on a laptop.

    443 under HTTPS is not decoration either. Caddy decides whether to get a
    certificate for a hostname from whether the server carrying that hostname's
    route is an HTTPS one, so a route table written onto `:80` would be served
    in the clear no matter what the `Caddyfile` said about `auto_https`. The
    hostnames come from the `host` matchers in the table below, which is why
    the certificate for a newly deployed app is obtained by the same write that
    publishes it.
    """
    return [":443"] if config.public_scheme == "https" else [":80"]

ID_PREFIX = "vd"
ID_SEPARATOR = "|"

TIMEOUT = 10.0

# Last in the table, so it answers whatever the hostnames above did not. The
# alternative is Caddy's own empty 200, which reads as "the app is up and
# serving nothing" to anybody looking at it.
NO_SUCH_APP = "vibedeploy: no app is published at this address"

NO_PROXY = (
    "The local proxy is not running, and an app is only ever reachable through"
    " it. Start it with `docker compose -f infra/local/docker-compose.yml up -d"
    " caddy` and deploy again."
)


class ProxyUnavailable(RuntimeError):
    """There is nowhere to publish a route, so nothing gets published.

    Deliberately not recoverable by falling back to the sidecar's port. A
    deployment whose address is a port number is not the thing section 16
    describes, and shipping it under the same name as the thing it is not is
    how a security property quietly stops being true.
    """


@dataclass(frozen=True)
class Route:
    """One app's traffic, and the generation of it that has the traffic."""

    host: str
    # `host:port` as the proxy must dial it, which is not necessarily how this
    # process would dial it.
    upstream: str
    app_id: str
    generation: int
    deployment_id: str

    @property
    def id(self) -> str:
        return ID_SEPARATOR.join(
            [ID_PREFIX, self.app_id, str(self.generation), self.deployment_id]
        )


def parse_id(route_id: str) -> tuple[str, int, str] | None:
    """The three labels back off a route, or `None` if it is not one of ours.

    What a reaper reads. Caddy's config is the only record of a published
    route, so it has to be possible to look at one and say whose it is.
    """
    parts = route_id.split(ID_SEPARATOR)
    if len(parts) != 4 or parts[0] != ID_PREFIX:
        return None
    try:
        return parts[1], int(parts[2]), parts[3]
    except ValueError:
        return None


def host_for(config: ControlPlaneConfig, subdomain: str) -> str:
    return f"{subdomain}.{config.app_domain}"


def public_url(config: ControlPlaneConfig, subdomain: str) -> str:
    """The app's address, and the only one recorded anywhere.

    `Deployment.endpoint` is this. It is what a builder is shown and what a
    browser is pointed at, so it has to be the public hostname rather than the
    private port the proxy happens to be dialling today.
    """
    host = host_for(config, subdomain)
    default = 443 if config.public_scheme == "https" else 80
    port = "" if config.app_port == default else f":{config.app_port}"
    return f"{config.public_scheme}://{host}{port}"


def _route_json(route: Route) -> dict:
    return {
        "@id": route.id,
        "match": [{"host": [route.host]}],
        "handle": [
            {
                "handler": "reverse_proxy",
                "upstreams": [
                    {"dial": container.reachable_from_container(route.upstream)}
                ],
            }
        ],
    }


CONSOLE_ID = "vd-console"


def _console_route(config: ControlPlaneConfig) -> list[dict]:
    """The console's own route, if it is published through this proxy at all.

    Not one of `Route`'s: it has no app, no generation and no deployment, so it
    cannot carry the three labels, and `parse_id` deliberately does not
    recognise its id — a reaper walking the table must not take the console for
    an app whose deployment has gone away.

    It is first in the table. Caddy takes the first matching route, and the
    console's hostname is a specific name while an app's is a subdomain of
    another domain, so they cannot collide; being first simply means an
    operator cannot be routed somewhere else by a badly chosen subdomain.
    """
    if not config.console_domain:
        return []
    # The dial is a `host:port`, which is what the public URL is minus its
    # scheme. Rewritten for the same reason an app's upstream is: `127.0.0.1`
    # inside Caddy's container is Caddy.
    upstream = config.public_url.split("://", 1)[-1]
    return [
        {
            "@id": CONSOLE_ID,
            "match": [{"host": [config.console_domain]}],
            "handle": [
                {
                    "handler": "reverse_proxy",
                    "upstreams": [
                        {"dial": container.reachable_from_container(upstream)}
                    ],
                }
            ],
        }
    ]


def _table(config: ControlPlaneConfig, routes: list[Route]) -> dict:
    return {
        "listen": _listen(config),
        # Sorted by host so that the same set of live apps always produces
        # byte-identical config. A reconciler that compares what it is about to
        # write against what is there needs that to mean something.
        "routes": _console_route(config)
        + [
            _route_json(route) for route in sorted(routes, key=lambda one: one.host)
        ]
        + [
            {
                "handle": [
                    {
                        "handler": "static_response",
                        "status_code": 404,
                        "body": NO_SUCH_APP,
                    }
                ]
            }
        ],
    }


async def _admin(
    config: ControlPlaneConfig, method: str, path: str, body: dict | None = None
) -> httpx.Response:
    url = f"{config.proxy_admin_url}{path}"
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            response = await client.request(method, url, json=body)
    except httpx.HTTPError as exc:
        raise ProxyUnavailable(NO_PROXY) from exc
    if response.status_code >= 400:
        raise ProxyUnavailable(
            f"{NO_PROXY}\n(the proxy answered {response.status_code}:"
            f" {response.text[:500]})"
        )
    return response


async def require(config: ControlPlaneConfig) -> None:
    """Raise before anything is started, if there is nowhere to publish it.

    Called first in a release, so that a missing proxy stops the deploy with a
    sentence rather than leaving a process running at an address nobody was
    ever told.
    """
    await _admin(config, "GET", "/config/")


async def apply(config: ControlPlaneConfig, routes: list[Route]) -> None:
    """Make this exactly the set of published routes.

    `POST`, not `PUT`, and the difference is the whole behaviour of this
    module: in Caddy's admin API `PUT` *creates* and fails with 409 `key
    already exists` if something is there, while `POST` sets or replaces. A
    create-only write cannot state "this is what is live" twice, which is the
    one thing every caller here does.

    Caddy does **not** create intermediate objects, which is the one thing a
    writer of this table has to know. `infra/local/Caddyfile` is a global
    options block and nothing else, on purpose, so a proxy that has just
    started has no `apps` key at all — and a POST straight at
    `apps/http/servers/vd` against that answers 500 `invalid traversal path`.
    So the depth of the write depends on how much of the path is already there:
    the usual case writes just this server, and a proxy that was restarted
    empty gets the chain made for it on the way.

    The outer object is carried over rather than replaced, because `apps` is
    also where anything else somebody has configured in Caddy lives.
    """
    table = _table(config, routes)
    whole = (await _admin(config, "GET", "/config/")).json() or {}
    apps = whole.get("apps") or {}
    http = apps.get("http") or {}
    if "servers" in http:
        await _admin(config, "POST", f"/config/apps/http/servers/{SERVER}", table)
        return
    await _admin(
        config,
        "POST",
        "/config/apps",
        {**apps, "http": {**http, "servers": {SERVER: table}}},
    )


async def published(config: ControlPlaneConfig) -> list[dict]:
    """Every route the proxy is currently serving, as it holds them.

    The "actual" half of the comparison, read back out of the proxy rather than
    remembered, because what the proxy is doing is the only thing that decides
    where a request goes. The trailing catch-all is excluded: it has no `@id`
    because it belongs to no deployment.
    """
    try:
        response = await _admin(
            config, "GET", f"/config/apps/http/servers/{SERVER}/routes"
        )
    except ProxyUnavailable:
        return []
    return [route for route in response.json() or [] if "@id" in route]


async def desired(
    config: ControlPlaneConfig, sessions: async_sessionmaker
) -> list[Route]:
    """Every route the database says should exist, and where it should point.

    Desired and actual come from two different places on purpose. The database
    decides *which* generation of an app has the traffic; the runtime says
    *where* that generation is listening. Taking both from the same place would
    make a reconciler's comparison agree with itself.

    An app whose live generation the runtime has never heard of is left out
    rather than guessed at: there is nothing to dial, and a route to nothing
    answers 502 while claiming to be published.

    The address is `running.upstream` and never anything this module builds,
    because the proxy is in a container of its own: `127.0.0.1` here is Caddy,
    so an upstream that was right for the worker would be a 502 for everybody
    else.
    """
    async with sessions() as session:
        rows = (
            await session.execute(
                select(App, Deployment).join(
                    Deployment, Deployment.id == App.live_deployment_id
                )
            )
        ).all()

    routes = []
    for app, deployment in rows:
        running = await serving.read(config, app.app_id, deployment.generation)
        if running is None:
            continue
        routes.append(
            Route(
                host=host_for(config, app.subdomain),
                upstream=running.upstream,
                app_id=app.app_id,
                generation=deployment.generation,
                deployment_id=str(deployment.id),
            )
        )
    return routes


async def reassert(
    config: ControlPlaneConfig, sessions: async_sessionmaker
) -> list[Route]:
    """Write the whole route table from the database. Idempotent.

    The step that grants traffic is `generations.publish`, which is a fenced
    write to this database; this only carries out what that write decided. So a
    stale worker calling this does not get its own route published — it
    publishes whatever won, which is the whole point of re-asserting rather
    than appending.
    """
    routes = await desired(config, sessions)
    await apply(config, routes)
    return routes

"""An app is reached through the proxy, or not at all. Readme.md section 16.

Until now a deployment's address was the sidecar's port, which made two claims
in the README into claims about this laptop rather than about the product:

  - "an app is reachable through its login gate" was true because the port we
    happened to hand out was the sidecar's, not because anything stopped the
    app's own port being handed out instead;
  - "traffic goes where the control plane says it goes" had nothing to check.
    A port number in a column is not a route; nothing was publishing anything.

So the route is now a real one, written over Caddy's admin API from what this
database says is live. The tests below are about that distinction: that the
address is public and the port is private, that the route can be traced back to
the exact pass of the exact deployment that asked for it, and that a proxy which
is not there stops the deploy instead of quietly going back to handing out a
port.

On Windows `*.localhost` does not resolve, so every request here goes to the
proxy's address with the app's hostname in a `Host:` header. See
`conftest.visit` — the resolver is the only thing being skipped, and Caddy
routes on the header regardless.
"""

from __future__ import annotations

from dataclasses import replace

import httpx
import pytest
import pytest_asyncio

from control_plane.models import DeploymentStatus
from tests.pipeline.conftest import settle, sign_in, visit
from worker.pipeline import container
from worker.runtimes import proxy, serving

ALICE = ("alice@example.com", "correct-horse-1")


@pytest_asyncio.fixture(scope="session")
async def live(pipeline):
    """One real deployment, all the way to live and published."""
    deployment_id = await pipeline.deploy("clean", name="proxied")
    await pipeline.confirm(deployment_id)
    deployment = await pipeline.deployment(deployment_id)
    assert deployment.status == DeploymentStatus.live.value, deployment.blocked_reason
    await settle()
    return deployment_id


# ---------------------------------------------------------------------------
# The address is public; the port is private
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    serving.__name__.endswith("local"), reason="bare processes have no namespace"
)
async def test_the_app_has_no_address_of_its_own(pipeline, live):
    """Protected. What replaced "the app alone would have been wide open".

    That test could be written because the app had an address: it bound a port,
    and a correctly signed header sent straight to it was believed, because
    believing it is the shim's entire job. The demonstration was honest and the
    conclusion was uncomfortable — the gate was the only thing in the way, and
    the only thing keeping traffic at the gate was which port we chose to hand
    out.

    It is now unsatisfiable, and that is the improvement. The app container
    joins the gate's network namespace, so it has no interface, publishes no
    port and has no name on any network: there is nowhere to send that header.
    The shim still believes a signed header — nothing about that changed — but
    the only process that can reach it through loopback is the gate, which
    strips the header before proxying.

    Asserted of docker rather than by trying to connect, because "I could not
    connect" is what a wrong port looks like too.
    """
    app = await pipeline.app_of(live)
    deployment = await pipeline.deployment(live)
    running = await serving.read(pipeline.config, app.app_id, deployment.generation)
    assert running is not None

    # No published port: nothing on this host, or anywhere else, has a way in.
    assert await container.inspect(
        running.app_container, "{{json .NetworkSettings.Ports}}"
    ) in ("{}", "null")

    # And no network of its own to be named on: the namespace belongs to the
    # gate. Compared against the gate's id as well as its name because docker
    # resolves the one into the other.
    mode = await container.inspect(
        running.app_container, "{{.HostConfig.NetworkMode}}"
    )
    assert mode is not None and mode.startswith("container:")
    gate_id = await container.inspect(running.sidecar_container, "{{.Id}}")
    assert mode.removeprefix("container:") in (running.sidecar_container, gate_id)

    # The gate, by contrast, does publish one — that asymmetry is the design,
    # and without this line the test above would also pass on an app that never
    # started at all.
    assert "8080/tcp" in (
        await container.inspect(
            running.sidecar_container, "{{json .NetworkSettings.Ports}}"
        )
        or ""
    )


async def test_the_app_is_published_at_its_own_hostname(pipeline, live):
    app = await pipeline.app_of(live)
    deployment = await pipeline.deployment(live)

    assert deployment.endpoint == f"http://{app.subdomain}.localhost:55080"
    assert deployment.endpoint == proxy.public_url(pipeline.config, app.subdomain)


async def test_the_private_address_is_not_the_apps_address(pipeline, live):
    """What changed. The private address still exists; it is no longer the answer.

    The runtime knows it, because the proxy has to dial something, and nowhere
    a builder or a browser is ever shown it.
    """
    app = await pipeline.app_of(live)
    deployment = await pipeline.deployment(live)
    running = await serving.read(pipeline.config, app.app_id, deployment.generation)

    assert running is not None
    assert running.upstream not in deployment.endpoint
    assert running.endpoint not in deployment.endpoint


async def test_the_app_answers_through_the_proxy(pipeline, live):
    """The whole path: Caddy -> sidecar -> app -> Postgres, with RLS at the end.

    Two people, one unfiltered `SELECT`, through a real proxy. The enforcement
    is unchanged by what is in front of it, which is the point: adding a hop
    must not be the reason a test passes or fails.
    """
    app = await pipeline.app_of(live)
    endpoint = (await pipeline.deployment(live)).endpoint

    # Named explicitly, because otherwise this test would still pass against a
    # deployment that had handed out the sidecar's port: the request would
    # simply go straight there and never touch Caddy. The address being the
    # proxy's is what makes the rest of this a statement about the proxy.
    assert endpoint.endswith(f":{pipeline.config.app_port}")

    alice = await pipeline.add_person(live, *ALICE)
    await pipeline.give_todo(app.app_id, alice.principal_key, "alice buys milk")

    hers = await sign_in(endpoint, *ALICE)
    try:
        todos = (await hers.get("/todos")).json()["todos"]
        assert [one["title"] for one in todos] == ["alice buys milk"]
    finally:
        await hers.aclose()


async def test_a_hostname_nobody_deployed_is_not_somebody_elses_app(pipeline, live):
    """The catch-all is last, and it is a 404 rather than a default app.

    A proxy with no final route answers the first one that matches, and "the
    first one" is alphabetical rather than meaningful. Being served a stranger's
    app because its hostname sorted earlier is the failure this prevents.
    """
    async with visit("http://nothing-is-deployed-here.localhost:55080") as browser:
        response = await browser.get("/")

    assert response.status_code == 404
    assert response.text == proxy.NO_SUCH_APP


# ---------------------------------------------------------------------------
# The route came from the database, and says whose it is
# ---------------------------------------------------------------------------


async def test_the_route_carries_the_three_labels(pipeline, live):
    """Section 5 commitment 1, for the resource that matters most.

    A route is the one external resource that *is* the thing being fenced, so
    it carries the same labels as the containers and volumes: without them, a
    route pointing at a dead port could not be attributed to anything, and the
    only way to find out whose it was would be to ask the worker that is gone.
    """
    app = await pipeline.app_of(live)
    deployment = await pipeline.deployment(live)

    published = await proxy.published(pipeline.config)
    mine = [
        route
        for route in published
        if proxy.parse_id(route["@id"]) == (
            app.app_id,
            deployment.generation,
            str(live),
        )
    ]

    assert len(mine) == 1
    assert mine[0]["match"] == [{"host": [f"{app.subdomain}.localhost"]}]


async def test_the_upstream_is_the_one_the_database_says_is_live(pipeline, live):
    """Desired and actual, joined by the proxy and taken from two places.

    The database decides *which* generation has the traffic; the runtime says
    *where* that generation is listening. Reading the dial back out of Caddy and
    checking it against what the runtime reports is what makes the route a
    consequence of those two facts rather than of a variable a worker was
    holding.
    """
    app = await pipeline.app_of(live)
    deployment = await pipeline.deployment(live)
    running = await serving.read(pipeline.config, app.app_id, deployment.generation)

    route = next(
        one
        for one in await proxy.published(pipeline.config)
        if proxy.parse_id(one["@id"])[0] == app.app_id
    )
    dialled = route["handle"][0]["upstreams"][0]["dial"]

    assert dialled == container.reachable_from_container(running.upstream)
    # And not this laptop's loopback, which inside Caddy's container is Caddy.
    assert "127.0.0.1" not in dialled


async def test_only_the_generation_that_holds_the_traffic_is_published(pipeline, live):
    """One app, one route. A generation that did not win is not reachable.

    `desired` is built by joining `apps.live_deployment_id`, so a generation
    that lost the fence has nothing to join to and cannot appear. This is the
    checkable half of "a stale worker cannot route traffic".
    """
    app = await pipeline.app_of(live)

    mine = [
        proxy.parse_id(route["@id"])
        for route in await proxy.published(pipeline.config)
        if proxy.parse_id(route["@id"])[0] == app.app_id
    ]

    assert len(mine) == 1
    assert mine[0][2] == str(app.live_deployment_id)


async def test_reasserting_the_route_table_changes_nothing(pipeline, live):
    """Idempotent, which is what a reconciler is going to lean on in CP6.

    `apply` states the whole table rather than patching it, so running it twice
    has to be indistinguishable from running it once — including the ordering,
    which is sorted by hostname for exactly this reason.
    """
    before = await proxy.published(pipeline.config)
    await proxy.reassert(pipeline.config, pipeline.sessions)
    after = await proxy.published(pipeline.config)

    assert before == after
    # Still serving, not merely still configured. Anonymous, so the gate's own
    # refusal is the right answer; what matters is that the answer came from
    # the gate and not from the catch-all.
    async with visit((await pipeline.deployment(live)).endpoint) as browser:
        response = await browser.get("/todos")
    assert response.status_code != 404
    assert response.text != proxy.NO_SUCH_APP


@pytest.mark.negative
async def test_a_proxy_that_was_restarted_empty_can_still_be_published_into(
    pipeline, live
):
    """Protected. The first bug the demo found that the suite could not.

    `infra/local/Caddyfile` is a global options block and nothing else, so a
    proxy that has only just started has no `apps` key at all — and Caddy's
    admin API does not create intermediate objects. `apply` used to POST
    straight at `apps/http/servers/vd` on the strength of a docstring claiming
    it did, which answers 500 `invalid traversal path` against that config.

    Everything above this still passed, because the tests share a long-running
    Caddy that something had already written the path into. What failed was the
    thing no test did: start the stack and deploy. Every deploy blocked with
    "the local proxy is not running" while the proxy was running perfectly, and
    the reconciler silently could not converge — which is precisely the "a
    Caddy that was restarted empty" case `worker/reconcile.py` names as one of
    the three it exists to fix.

    `DELETE /config/apps` is that state without restarting the container.
    """
    admin = pipeline.config.proxy_admin_url
    async with httpx.AsyncClient(timeout=proxy.TIMEOUT) as client:
        emptied = await client.delete(f"{admin}/config/apps")
        assert emptied.status_code < 400
        # The precondition is the whole test: if something still holds the path
        # open, this passes without ever exercising the bug.
        assert (await client.get(f"{admin}/config/")).json().get("apps") is None

    await proxy.reassert(pipeline.config, pipeline.sessions)

    app = await pipeline.app_of(live)
    assert [
        one
        for one in await proxy.published(pipeline.config)
        if proxy.parse_id(one["@id"])[0] == app.app_id
    ]
    # Serving again, not merely configured again: the listener is part of what
    # `apply` had to put back, so a route in the config proves nothing on its
    # own here.
    async with visit((await pipeline.deployment(live)).endpoint) as browser:
        response = await browser.get("/todos")
    assert response.status_code != 404
    assert response.text != proxy.NO_SUCH_APP


async def test_a_live_app_with_nothing_running_is_not_published(
    pipeline, live, monkeypatch
):
    """Protected. A route to something nothing answers on is worse than no route.

    `desired` leaves out an app whose live generation the runtime has never
    heard of, which is the state a machine is in after a reboot: the database
    still says what *should* be live, and nothing is. Publishing it anyway
    would turn "not started yet" into a 502, and a 502 is indistinguishable
    from the app being broken.

    The absence is arranged through `serving.read` rather than by deleting a
    file, because *what* records a running generation is the runtime's
    business: it was a JSON file under the state root and it is now a pair of
    labelled containers. The decision under test — `None` from the runtime
    means no route — is the same either way, and it is the half that was wrong.
    """
    app = await pipeline.app_of(live)

    real = serving.read

    async def forgotten(config, app_id, generation):
        if app_id == app.app_id:
            return None
        return await real(config, app_id, generation)

    monkeypatch.setattr(serving, "read", forgotten)
    routes = await proxy.desired(pipeline.config, pipeline.sessions)
    assert not [one for one in routes if one.app_id == app.app_id]
    monkeypatch.undo()

    # Put the table back the way the rest of the session expects it.
    await proxy.reassert(pipeline.config, pipeline.sessions)
    assert [
        one
        for one in await proxy.published(pipeline.config)
        if proxy.parse_id(one["@id"])[0] == app.app_id
    ]


# ---------------------------------------------------------------------------
# No proxy is no deploy
# ---------------------------------------------------------------------------


@pytest.mark.negative
async def test_no_proxy_means_no_deploy(pipeline, monkeypatch):
    """Protected. There is no raw-port fallback, so there is nothing to fall to.

    A deploy that handed out `http://127.0.0.1:54321` when the proxy was down
    would pass every other test in this file and would have quietly replaced
    the thing section 16 describes with something else under the same name. So
    this one points the worker at an admin API nothing is listening on, and
    insists the deploy stops — with a sentence that names the program to start,
    and with no address recorded.
    """
    real = pipeline.config
    deployment_id = await pipeline.deploy("clean", name="unproxied")
    assert (await pipeline.deployment(deployment_id)).status == (
        DeploymentStatus.awaiting_confirmation.value
    )

    # Port 1 refuses immediately. Patched after the plan phase because that is
    # where the proxy first matters: nothing has been published yet, and the
    # deploy has not been told it cannot be.
    monkeypatch.setattr(
        pipeline,
        "config",
        replace(pipeline.config, proxy_admin_url="http://127.0.0.1:1"),
    )
    await pipeline.confirm(deployment_id)

    deployment = await pipeline.deployment(deployment_id)
    assert deployment.status == DeploymentStatus.blocked.value
    assert deployment.blocked_reason.startswith(proxy.NO_PROXY[:40])
    assert deployment.endpoint is None

    # Nothing points at this app, read off the proxy that is actually running
    # rather than off the one the worker could not reach.
    app = await pipeline.app_of(deployment_id)
    assert app.live_deployment_id is None
    assert not [
        one
        for one in await proxy.published(real)
        if proxy.parse_id(one["@id"])[0] == app.app_id
    ]

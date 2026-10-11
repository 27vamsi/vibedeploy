"""Measuring a cutover instead of asserting one. CP5.

CP4 proves an *order*: the new version is watched before it is given traffic,
and the old one is drained before it is stopped. That is a claim about code.
"zero downtime" is a claim about requests, and the only way to make it one is to
have requests in flight while the thing happens and then count what came back.

So this file keeps a steady stream of real, authenticated requests going through
Caddy, into the sidecar, into the app, into Postgres — and redeploys the app
underneath it. Three things are counted, because they fail differently and two
of them are usually left out:

  - **non-2xx**: the app answered, badly. A 502 means the proxy had an upstream
    and the upstream was gone.
  - **connection errors**: nothing answered at all. These never appear in a
    status histogram, which is how a cutover that drops connections gets
    reported as 100% success.
  - **wrong rows**: the request succeeded and returned somebody else's data, or
    none. A cutover that silently logged people out, or that lost the session
    key, would look perfect to the first two counts.

The numbers are **printed**, with `-s`, not only asserted. A measurement nobody
can read is an assertion with extra steps, and the point of this checkpoint is
that there is a number to put in `EVIDENCE.md`.

`/slow` in the fixture app is what makes this a measurement rather than a
coincidence: a request that takes no time is never in flight when you look, and
a harness that cannot catch a request mid-cutover cannot catch a dropped one
either. The last test here is about the instrument, not the product — it kills
the old version out from under the same harness and insists the harness notices.
"""

from __future__ import annotations

import asyncio
import statistics
import time
from collections import Counter
from dataclasses import dataclass, field, replace

import httpx
import pytest
import pytest_asyncio

from control_plane.models import DeploymentStatus
from tests.pipeline.conftest import settle, sign_in
from worker.runtimes import serving

ALICE = ("alice@example.com", "correct-horse-1")
HERS = "alice's only todo"

# Each request takes this long inside the app, so at `RPS` there are always
# several of them part-way through. Long enough to be caught by a cutover,
# short enough that a few hundred of them do not dominate the run.
SLOW = 0.4
RPS = 10
PATH = f"/slow?seconds={SLOW}"

# Generous. A request that takes four seconds has not been dropped, and calling
# it dropped would turn every `ALTER TABLE` lock wait into a false failure. What
# the latency did is printed either way.
PATIENCE = 15.0


@dataclass
class Measured:
    """What a stream of requests across a cutover came back as."""

    requests: int = 0
    ok: int = 0
    statuses: Counter = field(default_factory=Counter)
    # Keyed by exception name, because "it did not answer" has several shapes
    # and which one it was is the first thing anybody debugging would ask.
    errors: Counter = field(default_factory=Counter)
    wrong_rows: int = 0
    latencies: list[float] = field(default_factory=list)
    seconds: float = 0.0

    @property
    def bad(self) -> int:
        return sum(self.errors.values()) + sum(
            count for status, count in self.statuses.items() if status // 100 != 2
        )

    def report(self, title: str) -> str:
        order = sorted(self.latencies)
        worst = order[-1] if order else 0.0
        p50 = statistics.median(order) if order else 0.0
        p99 = order[int(len(order) * 0.99)] if len(order) > 1 else worst
        return "\n".join(
            [
                "",
                f"  {title}",
                f"    {self.requests} requests over {self.seconds:.1f}s"
                f" (~{self.requests / max(self.seconds, 0.001):.1f}/s,"
                f" {SLOW:.1f}s each)",
                f"    statuses          {dict(sorted(self.statuses.items()))}",
                f"    connection errors {dict(self.errors) or '{}'}",
                f"    wrong rows        {self.wrong_rows}",
                f"    latency           p50 {p50:.2f}s  p99 {p99:.2f}s"
                f"  max {worst:.2f}s",
            ]
        )


async def hammer(
    client: httpx.AsyncClient, stop: asyncio.Event, expect: list[str]
) -> Measured:
    """Keep roughly `RPS` requests a second going until `stop`, and count them.

    Requests are launched on a timer rather than one-after-another on purpose:
    back-to-back requests would slow down when the app does, which is exactly
    when the interesting thing is happening. A fixed rate keeps the pressure on
    through the cutover and lets the latency say what the cutover cost.
    """
    measured = Measured()
    started = time.monotonic()
    flight: set[asyncio.Task] = set()

    async def once() -> None:
        at = time.monotonic()
        try:
            response = await client.get(PATH)
        except httpx.HTTPError as exc:
            measured.errors[type(exc).__name__] += 1
            return
        finally:
            measured.requests += 1
            measured.latencies.append(time.monotonic() - at)
        measured.statuses[response.status_code] += 1
        if response.status_code != 200:
            return
        measured.ok += 1
        try:
            titles = [one["title"] for one in response.json()["todos"]]
        except (ValueError, KeyError, TypeError):
            measured.wrong_rows += 1
            return
        if titles != expect:
            measured.wrong_rows += 1

    while not stop.is_set():
        task = asyncio.create_task(once())
        flight.add(task)
        task.add_done_callback(flight.discard)
        await asyncio.sleep(1 / RPS)

    if flight:
        await asyncio.gather(*list(flight), return_exceptions=True)
    measured.seconds = time.monotonic() - started
    return measured


@pytest_asyncio.fixture(scope="session")
async def served(pipeline):
    """A live app with one row in it, and Alice signed in. The caller hammers it."""
    deployment_id = await pipeline.deploy("clean", name="downtime")
    await pipeline.confirm(deployment_id)
    deployment = await pipeline.deployment(deployment_id)
    assert deployment.status == DeploymentStatus.live.value, deployment.blocked_reason

    app = await pipeline.app_of(deployment_id)
    alice = await pipeline.add_person(deployment_id, *ALICE)
    await pipeline.give_todo(app.app_id, alice.principal_key, HERS)
    await settle()
    return deployment_id


async def measure_across(pipeline, deployment_id, run) -> Measured:
    """Hammer the app, do `run()` to it, keep hammering, then stop and count."""
    endpoint = (await pipeline.deployment(deployment_id)).endpoint
    client = await sign_in(endpoint, *ALICE)
    client.timeout = httpx.Timeout(PATIENCE)
    stop = asyncio.Event()
    try:
        hammering = asyncio.create_task(hammer(client, stop, [HERS]))
        # Steady state first, so the numbers include a before and an after and
        # not just the event.
        await asyncio.sleep(1.0)
        await run()
        await asyncio.sleep(1.0)
    finally:
        stop.set()
        measured = await hammering
        await client.aclose()
    return measured


async def test_a_real_cutover_drops_no_requests(pipeline, served, capsys):
    """The measurement CP5 exists for, printed so it can be quoted.

    A whole deployment happens during this: the code is cloned, the rules are
    re-proved in the throwaway database, the migration task runs against the
    live schema, RLS is re-applied, a new version is started and watched, the
    route moves, the old version is drained and stopped. Every request in the
    stream has to come back 200 with Alice's row in it.

    The session surviving matters as much as the status codes: the cookie was
    issued by the sidecar that is killed partway through this, and the new one
    accepts it because the key belongs to the app rather than to the process.

    What is *not* claimed, and is visible in the printed latency: a redeploy is
    free of dropped requests and is not free. p50 stays at the app's own 0.47s
    and the worst request of ~300 takes about 1.3s, which is `enforce`
    re-applying RLS with an `ALTER TABLE` on tables the live version is reading
    from. Slower, briefly, is a different thing from unavailable, and the only
    reason that is sayable here is that both numbers were measured.
    """
    before = (await pipeline.deployment(served)).generation

    async def cutover() -> None:
        second = await pipeline.redeploy(served)
        await pipeline.confirm(second)
        deployment = await pipeline.deployment(second)
        assert deployment.status == DeploymentStatus.live.value, (
            deployment.blocked_reason
        )
        assert deployment.generation > before

    measured = await measure_across(pipeline, served, cutover)

    with capsys.disabled():
        print(measured.report("a redeploy, with traffic on it"))

    assert measured.requests > 50, "not enough traffic for this to mean anything"
    assert dict(measured.errors) == {}
    assert [status for status in measured.statuses if status // 100 != 2] == []
    assert measured.wrong_rows == 0


async def test_what_the_drain_is_actually_worth(pipeline, served, capsys, monkeypatch):
    """The same cutover with `drain_seconds = 0`, measured rather than assumed.

    Reports whatever happens and asserts nothing about the count, because the
    honest version of "the drain is what stops requests being dropped" is
    smaller than it sounds: the runtime's own stop asks politely, and uvicorn
    answers a polite stop by finishing the requests it is already holding.
    The drain is therefore the second of two things covering an in-flight
    request, not the only one.

    Observed with no drain: **1 request in 257 came back 502**, against 0 in 307
    with the drain. So it is doing real work and not much of it, and a test that
    asserted "no drain means failures" would be flaky at that rate while
    sounding certain. The claim that is safe to make is the one the next test
    makes: the harness can see a dropped request, conclusively.
    """
    monkeypatch.setattr(
        pipeline, "config", replace(pipeline.config, drain_seconds=0.0)
    )

    async def cutover() -> None:
        third = await pipeline.redeploy(served)
        await pipeline.confirm(third)
        deployment = await pipeline.deployment(third)
        assert deployment.status == DeploymentStatus.live.value, (
            deployment.blocked_reason
        )

    measured = await measure_across(pipeline, served, cutover)

    with capsys.disabled():
        print(measured.report("the same redeploy with no drain at all"))
        print(f"    -> {measured.bad} request(s) came back wrong\n")

    assert measured.requests > 50, "not enough traffic for this to mean anything"
    # Whatever the drain is worth, it is not worth anybody's rows: a dropped
    # request is a 502 and never a 200 holding the wrong list.
    assert measured.wrong_rows == 0


@pytest.mark.negative
async def test_the_harness_can_see_a_dropped_request(pipeline, served, capsys):
    """Protected. An instrument that cannot report a failure has not reported a
    success either.

    The control case. Same harness, same app, same requests — and the version
    serving them is killed uncounted, out of band, with nothing put in its
    place: no health gate, no route move, no drain. These requests *are*
    dropped, and this test fails if the numbers come back clean.

    `serving.kill` rather than the ordinary stop because being asked politely
    is the whole point of the drain: uvicorn finishes what it is holding, which
    is why the cutover above can be lossless. The question here is only whether
    the counters move when something really does go wrong.
    """
    app = await pipeline.app_of(served)

    async def execute() -> None:
        # Whatever is live *now*, which after the cutover above is no longer the
        # generation `served` deployed. Taken from `live_deployment_id` for the
        # same reason the worker takes it from there: a test holding its own
        # idea of which version is serving would kill the wrong one and then
        # measure nothing.
        live = (await pipeline.app_of(served)).live_deployment_id
        assert live is not None
        generation = (await pipeline.deployment(live)).generation
        assert await serving.read(pipeline.config, app.app_id, generation) is not None
        await serving.kill(pipeline.config, app.app_id, generation)
        await asyncio.sleep(0.5)

    measured = await measure_across(pipeline, served, execute)

    with capsys.disabled():
        print(measured.report("the same traffic, with the app killed under it"))

    assert measured.bad > 0, (
        "the app was killed mid-request and the harness saw nothing wrong,"
        " so it cannot be trusted to have seen the cutover either"
    )

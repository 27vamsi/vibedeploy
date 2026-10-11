"""Replacing a running version without dropping a request. CP4.

What this file is for is the gap between "a new version started" and "a new
version works". Until now those were the same event: the runtime's `start`
waited for a port to answer once and the old version was killed in the next
statement. An app
that boots, answers one probe and then falls over was therefore promoted, and
the version that had been serving perfectly well was already gone.

So the tests below are about an order of operations, and each one names the thing
that goes wrong if that step moves:

  start new -> health gate (N consecutive) -> point the proxy at new
            -> drain -> SIGTERM old -> verify it exited

The two halves are tested differently on purpose. The gate's logic is tested
against a scripted socket, because the interesting input is a *sequence* of
answers and a real app cannot be asked to fail on its second request only. The
order is tested against a real deployment, because an order is only true of the
code that actually runs it — a unit test of a cutover is a test of the test.

`worker/pipeline/cutover.py` argues for each step; this file tries to catch it
not doing them.
"""

from __future__ import annotations

import asyncio
import socket
import time
from dataclasses import dataclass, field, replace
from typing import Any

import pytest
import pytest_asyncio

from control_plane.models import DeploymentStatus
from tests.pipeline.conftest import settle, sign_in, visit
from worker.pipeline import cutover
from worker.runtimes import proxy, serving

ALICE = ("alice@example.com", "correct-horse-1")

# Fast, because nothing here is waiting for a real app to boot. The gate's
# behaviour is about the order of its answers, not about the clock.
BRISK = cutover.Gate(passes=3, interval=0.02, timeout=5.0)


# ---------------------------------------------------------------------------
# A socket that answers a script
# ---------------------------------------------------------------------------


class Scripted:
    """An HTTP server that answers a fixed list of statuses, then repeats.

    Deliberately not a real app and not the sidecar. The one input that matters
    to a consecutive gate is the *sequence* — 200, then 500, then 200 — and
    there is no way to ask a real process for that. `served` is how many times
    it was actually asked, which is what tells a run of three from three
    scattered passes.
    """

    def __init__(self, statuses: list[int]) -> None:
        self.statuses = list(statuses)
        self.served = 0

    async def __aenter__(self) -> "Scripted":
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        self.port = int(self._server.sockets[0].getsockname()[1])
        return self

    async def __aexit__(self, *exc: object) -> None:
        self._server.close()
        await self._server.wait_closed()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}{cutover.HEALTH_PATH}"

    async def _handle(self, reader: Any, writer: Any) -> None:
        try:
            await reader.readuntil(b"\r\n\r\n")
        except (asyncio.IncompleteReadError, ConnectionError, asyncio.LimitOverrunError):
            writer.close()
            return
        status = self.statuses[min(self.served, len(self.statuses) - 1)]
        self.served += 1
        body = b'{"status":"ok"}'
        writer.write(
            f"HTTP/1.1 {status} Scripted\r\nContent-Length: {len(body)}\r\n"
            "Connection: close\r\n\r\n".encode()
            + body
        )
        try:
            await writer.drain()
        except ConnectionError:
            pass
        writer.close()


def _closed_port() -> int:
    """A port nothing is listening on, which is what a dead process leaves."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _answering(port: int) -> cutover.Watcher:
    """The app half of a probe, as the gate now takes it: a question, not a port.

    The gate asks "is the app still there?" and the two runtimes answer it with
    different evidence — a port accepting connections locally, `.State.Running`
    on a container. A port is what this file can script, so the port goes in
    here and nowhere near `cutover`.

    Its own socket rather than the runtime's, deliberately: there is no
    deployed app in these tests, only a scripted server, so borrowing a
    runtime's liveness check would tie a unit test of the gate's *counting* to
    whichever runtime happens to be selected.
    """

    def connects() -> bool:
        with socket.socket() as sock:
            sock.settimeout(2.0)
            return sock.connect_ex(("127.0.0.1", port)) == 0

    async def alive() -> bool:
        return await asyncio.to_thread(connects)

    return alive


# ---------------------------------------------------------------------------
# The gate: N in a row, and the run resets
# ---------------------------------------------------------------------------


async def test_a_version_that_stays_up_passes_in_exactly_the_passes_asked_for():
    async with Scripted([200]) as gate_server, Scripted([200]) as app_server:
        result = await cutover.health_gate(
            health_url=gate_server.url, app_alive=_answering(app_server.port), gate=BRISK
        )

    assert result.passed
    assert (result.consecutive, result.probes, result.failures) == (3, 3, 0)
    assert result.needed == 3
    # Returned on the pass that completed the run, without sleeping again.
    assert gate_server.served == 3


async def test_a_flapping_version_has_its_run_reset_rather_than_continued():
    """Protected. This is the whole reason the gate counts consecutively.

    Five answers, one of them bad in the middle. A gate that counted total
    passes would have promoted this version on its fourth probe. A gate that
    counts a *run* needs a fourth and fifth, and the difference between those
    two gates is exactly an app that crashes on its second request.
    """
    async with Scripted([200, 500, 200, 200, 200]) as gate_server, Scripted(
        [200]
    ) as app_server:
        result = await cutover.health_gate(
            health_url=gate_server.url, app_alive=_answering(app_server.port), gate=BRISK
        )

    assert result.passed
    assert result.probes == 5
    assert result.failures == 1
    assert result.consecutive == 3


async def test_a_version_that_never_answers_fails_with_what_was_tried():
    port = _closed_port()
    result = await cutover.health_gate(
        health_url=f"http://127.0.0.1:{port}{cutover.HEALTH_PATH}",
        app_alive=_answering(port),
        gate=replace(BRISK, timeout=0.3),
    )

    assert not result.passed
    assert result.consecutive == 0
    assert result.probes >= 1
    assert "login gate could not be reached" in result.detail


async def test_a_gate_that_is_up_with_a_dead_app_behind_it_fails():
    """The half that would otherwise read as healthy.

    The sidecar is our process and answers its own health route whether or not
    the app behind it is alive. Probing only that would promote a version whose
    every request is a 502 — and a 502 looks like the platform's fault, which
    is the worst possible way for this to be wrong.
    """
    async with Scripted([200]) as gate_server:
        result = await cutover.health_gate(
            health_url=gate_server.url,
            app_alive=_answering(_closed_port()),
            gate=replace(BRISK, timeout=0.3),
        )

    assert not result.passed
    assert result.detail == "the app itself had stopped accepting connections."


async def test_a_gate_that_answers_the_wrong_status_fails():
    async with Scripted([503]) as gate_server, Scripted([200]) as app_server:
        result = await cutover.health_gate(
            health_url=gate_server.url,
            app_alive=_answering(app_server.port),
            gate=replace(BRISK, timeout=0.3),
        )

    assert not result.passed
    assert result.detail == "its login gate answered 503."


def test_the_sentence_a_builder_reads_says_what_was_tried():
    """It has to render from any result, including a partially successful run.

    Not decoration: this is the only thing a person sees when a deploy is
    refused for being unhealthy, and the branch where `consecutive` is non-zero
    is the one a flapping app takes.
    """
    sentence = cutover.gate_failed(
        cutover.GateResult(
            passed=False,
            consecutive=2,
            needed=3,
            probes=14,
            failures=5,
            seconds=7.0,
            detail="its login gate answered 500.",
        )
    )

    assert "14 times" in sentence
    assert "3 passes in a row" in sentence
    assert "never got past 2" in sentence
    # And it says what did not happen, which is the part that stops a builder
    # from going to look for the damage.
    assert "already live is still serving" in sentence


# ---------------------------------------------------------------------------
# The order, against a real deployment
# ---------------------------------------------------------------------------


@dataclass
class Witness:
    """What was true at the instant the old version was about to be stopped."""

    generations: list[int] = field(default_factory=list)
    published: list[dict] = field(default_factory=list)
    # Taken before the flip, because the stop destroys the only description of
    # what was stopped and there is nothing to check against afterwards.
    old_alive: cutover.Watcher | None = None
    retired: cutover.Retired | None = None


@pytest_asyncio.fixture(scope="session")
async def first(pipeline):
    """One real deployment, live and published. The version to be replaced."""
    deployment_id = await pipeline.deploy("clean", name="cutover")
    await pipeline.confirm(deployment_id)
    deployment = await pipeline.deployment(deployment_id)
    assert deployment.status == DeploymentStatus.live.value, deployment.blocked_reason
    await settle()
    return deployment_id


@pytest_asyncio.fixture(scope="session")
async def flipped(pipeline, first):
    """A second real deployment of the same app, with a witness at the flip.

    The spy wraps `retire` rather than replacing it, so what is measured is the
    real cutover: the gate ran, the fenced publish happened, the proxy was
    re-asserted, and only then was this called. Everything the spy records is
    read *before* the old version is touched, which is the only moment the
    claim "both were live" can be checked at all — a moment later it is false
    on purpose.
    """
    witness = Witness()
    real = cutover.retire

    app = await pipeline.app_of(first)
    witness.old_alive = await serving.watcher(
        pipeline.config,
        app.app_id,
        (await pipeline.deployment(first)).generation,
    )

    async def spy(config, *, app_id, keep, drain, **kwargs):
        witness.generations = await serving.generations(config, app_id)
        witness.published = await proxy.published(config)
        witness.retired = await real(
            config, app_id=app_id, keep=keep, drain=drain, **kwargs
        )
        return witness.retired

    cutover.retire = spy
    try:
        second = await pipeline.redeploy(first)
        await pipeline.confirm(second)
    finally:
        cutover.retire = real

    deployment = await pipeline.deployment(second)
    assert deployment.status == DeploymentStatus.live.value, deployment.blocked_reason
    await settle()
    return second, witness


async def test_both_versions_were_running_when_the_traffic_moved(
    pipeline, first, flipped
):
    """Blue-green, stated as the one fact that distinguishes it.

    If the old version had been stopped to make room for the new one, this list
    would have one entry in it, and every "zero dropped requests" claim after
    this would be a claim about a restart.
    """
    _, witness = flipped
    old = (await pipeline.deployment(first)).generation
    new = (await pipeline.deployment(flipped[0])).generation

    assert new > old
    assert sorted(witness.generations) == sorted([old, new])


async def test_the_traffic_had_already_moved_before_the_old_one_was_stopped(
    pipeline, flipped
):
    """The drain is only worth anything in this order.

    Stopping the old version first, or draining it first, would leave a window
    where the old one refuses and the new one has no traffic. So at the instant
    `retire` is entered the published route must already name the new
    generation: from then on everything arriving is going to the new version
    and the drain covers only what was already in flight.
    """
    second, witness = flipped
    app = await pipeline.app_of(second)
    new = (await pipeline.deployment(second)).generation

    mine = [
        proxy.parse_id(route["@id"])
        for route in witness.published
        if proxy.parse_id(route["@id"])[0] == app.app_id
    ]

    assert len(mine) == 1
    assert mine[0] == (app.app_id, new, str(second))


async def test_the_old_version_is_verified_gone_and_not_merely_signalled(
    pipeline, first, flipped
):
    """"We sent SIGTERM" and "it stopped" are different claims.

    A process that ignores the signal is indistinguishable from one that obeys
    it unless somebody looks afterwards, and the leftover would go on holding
    the app's database connections. The evidence is the runtime's own liveness
    check coming back false, not an exit code: once a worker restarts, a pid is
    all that is left, and on Windows `os.kill(pid, 0)` terminates the process
    it is asked about.
    """
    second, witness = flipped
    app = await pipeline.app_of(second)
    old = (await pipeline.deployment(first)).generation
    new = (await pipeline.deployment(second)).generation

    assert witness.retired is not None
    assert witness.retired.generations == [old]
    assert witness.retired.exited is True

    assert await serving.generations(pipeline.config, app.app_id) == [new]
    assert await serving.read(pipeline.config, app.app_id, old) is None

    # And the evidence itself, independently of what `retire` reported: the old
    # version has stopped answering.
    assert witness.old_alive is not None
    assert not await witness.old_alive()


async def test_the_drain_is_waited_out_before_anything_is_stopped(
    pipeline, tmp_path, monkeypatch
):
    """The sleep is between the route moving and the signal, or it is pointless.

    Measured against a state root with nothing in it, because what is being
    checked is the ordering inside `retire` and not which processes it found.
    """
    config = replace(pipeline.config, state_root=tmp_path)
    reaped: list[float] = []

    async def spy(*args: object, **kwargs: object) -> list[int]:
        reaped.append(time.monotonic())
        return []

    monkeypatch.setattr(serving, "reap", spy)
    started = time.monotonic()
    result = await cutover.retire(config, app_id="nobody", keep=1, drain=0.4)

    assert reaped and reaped[0] - started >= 0.4
    assert result.drained == 0.4
    # Nothing was running, so there is nothing left to be still answering.
    assert result.exited is True


async def test_a_version_that_did_not_stop_is_reported_rather_than_hidden(
    pipeline, tmp_path, monkeypatch
):
    """Protected. "We asked it to stop" has to be able to come back as "and it
    did not", or `exited` is decoration.

    So: a generation that exists, a real stop that reaches nothing, and
    something that is genuinely still accepting connections afterwards. That
    last part is a real socket, not a flag — the question `retire` has to get
    right is whether it believes the evidence or its own instruction, and a
    fake answer would not tell the two apart.

    `exited` has to come back False, and `retire` has to *return* it rather
    than raise: the new version already holds the traffic by this point, so a
    leftover is the reconciler's problem and not a failed deployment.

    Honest about what this no longer demonstrates. It used to arrange a run
    record naming a dead pid and a live port, which was the exact shape of a
    uvicorn that outlived its SIGTERM. A container cannot be in that shape —
    `docker stop` escalates to SIGKILL, which nothing can ignore — so the
    scenario is now arranged rather than found: a weaker demonstration of a
    stronger system, and the property it protects is unchanged.
    """
    config = replace(pipeline.config, state_root=tmp_path)
    async with Scripted([200]) as stubborn:
        still_up = _answering(stubborn.port)

        async def one_generation(*args: object, **kwargs: object) -> list[int]:
            return [1]

        async def watching(*args: object, **kwargs: object) -> cutover.Watcher:
            return still_up

        # `reap` is left real. It looks for generation 1, finds nothing of it on
        # this empty state root, and stops nothing — which is precisely the
        # position a worker is in when its signal did not land.
        monkeypatch.setattr(serving, "generations", one_generation)
        monkeypatch.setattr(serving, "watcher", watching)

        result = await cutover.retire(
            config, app_id="app_stubborn", keep=2, drain=0.0, exit_timeout=0.4
        )

        assert result.generations == [1]
        assert result.exited is False
        # And the evidence was real: it is still answering now, after `retire`
        # spent its whole exit timeout waiting for it to stop. Inside the
        # block, because outside it the server is shut and "not answering"
        # would be this test's own doing.
        assert await still_up() is True


async def test_the_new_version_serves_at_the_same_address(pipeline, flipped):
    """The address is the app's, not the deployment's, and it did not move."""
    second, _ = flipped
    app = await pipeline.app_of(second)
    endpoint = (await pipeline.deployment(second)).endpoint

    assert endpoint == proxy.public_url(pipeline.config, app.subdomain)

    alice = await pipeline.add_person(second, *ALICE)
    await pipeline.give_todo(app.app_id, alice.principal_key, "alice after the flip")

    hers = await sign_in(endpoint, *ALICE)
    try:
        todos = (await hers.get("/todos")).json()["todos"]
        assert [one["title"] for one in todos] == ["alice after the flip"]
    finally:
        await hers.aclose()


# ---------------------------------------------------------------------------
# A failed gate is not a failed deployment of the old version
# ---------------------------------------------------------------------------


@pytest.mark.negative
async def test_a_version_that_does_not_stay_healthy_never_gets_the_traffic(
    pipeline, flipped, monkeypatch
):
    """Protected. The failure mode the gate exists for, end to end.

    `probe` is forced to fail; everything else is real, including the gate's
    retrying and its clock. What must be true afterwards is a list of things
    that did *not* happen: no route moved, nothing was revoked, the version
    people are using was not interrupted, and the only thing left behind is a
    sentence. A gate that blocked by taking the app down would pass a test that
    only checked the deployment's status.
    """
    live_before, _ = flipped
    app = await pipeline.app_of(live_before)
    unmoved = (await pipeline.deployment(live_before)).generation

    async def never(health_url: str, app_alive: cutover.Watcher) -> str:
        return "its login gate answered 500."

    monkeypatch.setattr(cutover, "probe", never)
    # A short clock, so the real loop is exercised without the test waiting out
    # the production timeout. The logic under test is the retrying, not 30s.
    monkeypatch.setattr(
        pipeline,
        "config",
        replace(pipeline.config, health_timeout=1.0, health_interval=0.05),
    )

    third = await pipeline.redeploy(live_before)
    await pipeline.confirm(third)

    deployment = await pipeline.deployment(third)
    assert deployment.status == DeploymentStatus.blocked.value
    assert "did not stay healthy" in deployment.blocked_reason
    assert "already live is still serving" in deployment.blocked_reason
    assert deployment.endpoint is None

    # The old version still holds the traffic, in the database...
    app = await pipeline.app_of(third)
    assert app.live_deployment_id == live_before
    # ...and in the proxy, read back off Caddy rather than off what we intended.
    mine = [
        proxy.parse_id(route["@id"])
        for route in await proxy.published(pipeline.config)
        if proxy.parse_id(route["@id"])[0] == app.app_id
    ]
    assert mine == [(app.app_id, unmoved, str(live_before))]

    # The candidate's half was taken back, and the serving one was not.
    assert await serving.generations(pipeline.config, app.app_id) == [unmoved]

    # Still answering. Anonymous, so the gate's own refusal is the right answer;
    # what matters is that it came from the gate and not from the catch-all.
    async with visit(proxy.public_url(pipeline.config, app.subdomain)) as browser:
        response = await browser.get("/todos")
    assert response.status_code != 404
    assert response.text != proxy.NO_SUCH_APP

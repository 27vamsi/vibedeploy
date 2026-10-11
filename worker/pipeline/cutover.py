"""Replacing a running version without dropping a request.

Starting the new version and stopping the old one used to be one step, and it
had three problems hiding in it:

  - nothing checked that the new version *worked* before it was given the
    traffic. `serving.start` waits for the gate to answer once, which an app
    that falls over on its second request also does;
  - the old version was stopped the instant the new one was published, so any
    request already running on it died mid-sentence;
  - nothing checked that it had actually stopped. A process that ignored the
    signal was indistinguishable from one that obeyed it.

So the cutover is five steps, in this order, and the order is the whole thing:

    start new  ->  health gate (N consecutive)  ->  point the proxy at new
               ->  drain  ->  SIGTERM old  ->  verify it exited

**Why the gate is consecutive.** A single probe answers "is it up?", and the
interesting failures are not up-or-down: an app that boots, answers, and then
dies on its first real query passes one probe. N passes spaced by an interval
is the cheapest thing that distinguishes "listening" from "staying up".

**What the gate can and cannot check.** It probes the login gate's own health
endpoint, which is ours, and it checks that the app behind it is still
accepting connections. It does **not** ask the app for a 200 on any path,
because we did not write the app and have no right to assume it has a health
route. That is a real limit and it is the same one the runtime's own wait has:
listening is the only readiness signal we are entitled to.

**Why the drain comes after the proxy moves, not before.** Draining first would
mean a window where the old version is refusing and the new one has no traffic.
The proxy moves first, so new requests go to the new version immediately; the
drain is only ever about requests that were already in flight.

**Why a failed gate is not a failed deployment of the old version.** Nothing is
revoked by blocking. The old version's route is still published, because
`generations.publish` was never called, and its process is still running,
because `retire` was never called. A builder gets a sentence and the people
using the app get no interruption at all.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass

import httpx

from control_plane.config import ControlPlaneConfig
from worker.runtimes import Watcher, serving

# The sidecar's own health route, which is the only endpoint on a deployed app
# that we wrote and can therefore demand a 200 from. Kept here rather than
# imported from `sidecar.app` so that watching a process does not drag the whole
# gate's ASGI app into the worker.
HEALTH_PATH = "/__vd/health"

# How long one look at the login gate is given. The other half of a probe is the
# runtime's own liveness check, which has its own.
PROBE_TIMEOUT = 2.0

# How long to wait for a process to be gone after it was asked to go. Generous
# against a laptop under load; a worker that waits a moment longer is cheaper
# than one that reports a stop it did not verify.
EXIT_TIMEOUT = 15.0
EXIT_INTERVAL = 0.2


@dataclass(frozen=True)
class Gate:
    passes: int
    interval: float
    timeout: float


@dataclass(frozen=True)
class GateResult:
    passed: bool
    # The run of passes it finished on. Lower than `probes` whenever something
    # failed in the middle, which is the case the whole design is about.
    consecutive: int
    # What the run had to reach. Carried on the result so the sentence a builder
    # reads does not have to be handed the `Gate` as well.
    needed: int
    probes: int
    failures: int
    seconds: float
    detail: str = ""


@dataclass(frozen=True)
class Retired:
    """What was stopped, and whether it is really gone."""

    generations: list[int]
    drained: float
    # False means something was signalled and was still answering afterwards.
    # Reported rather than raised: the new version already has the traffic, so
    # this is a leftover for the reconciler, not a failed deployment.
    exited: bool


def gate_for(config: ControlPlaneConfig) -> Gate:
    return Gate(
        passes=config.health_passes,
        interval=config.health_interval,
        timeout=config.health_timeout,
    )


def gate_failed(result: GateResult) -> str:
    """The sentence a builder reads. Says what was tried, not what broke."""
    return (
        "This version started but did not stay healthy, so it was never given"
        f" any traffic: {result.detail} We checked {result.probes} times over"
        f" {result.seconds:.0f} seconds and needed {result.needed} passes in a"
        f" row, but never got past {result.consecutive}. The version that was"
        " already live is still serving, and nothing about your database was"
        " changed by this."
    )


async def probe(health_url: str, app_alive: Watcher) -> str | None:
    """One look at the candidate. `None` means healthy, a sentence means not.

    Both halves, because they fail differently: the gate can be up while the
    app behind it is gone, and then every request is a 502 that looks like the
    platform's fault.

    The app half arrives as a callable rather than as an address, because the
    two runtimes have different evidence for it and neither is the other's: a
    port accepting connections locally, and `.State.Running` on the app's own
    container. A `probe` that took a port could only ever have worked for one
    of them.
    """
    try:
        async with httpx.AsyncClient(timeout=PROBE_TIMEOUT) as client:
            response = await client.get(health_url)
    except httpx.HTTPError as exc:
        return f"its login gate could not be reached ({type(exc).__name__})."
    if response.status_code != 200:
        return f"its login gate answered {response.status_code}."
    if not await app_alive():
        return "the app itself had stopped accepting connections."
    return None


async def health_gate(
    *, health_url: str, app_alive: Watcher, gate: Gate
) -> GateResult:
    """Watch the candidate until it has passed `gate.passes` times in a row.

    A failure resets the run rather than ending it: an app that is slow to
    settle is a normal thing, and the gate's job is to tell slow-to-settle from
    never-settles. Only the clock ends it.
    """
    started = time.monotonic()
    consecutive = probes = failures = 0
    detail = "it never answered at all."

    while time.monotonic() - started < gate.timeout:
        probes += 1
        reason = await probe(health_url, app_alive)
        if reason is None:
            consecutive += 1
            if consecutive >= gate.passes:
                return GateResult(
                    passed=True,
                    consecutive=consecutive,
                    needed=gate.passes,
                    probes=probes,
                    failures=failures,
                    seconds=time.monotonic() - started,
                )
        else:
            failures += 1
            # Back to zero. Two passes either side of a failure are not a run
            # of two, and treating them as one is how a flapping version gets
            # promoted.
            consecutive = 0
            detail = reason
        await asyncio.sleep(gate.interval)

    return GateResult(
        passed=False,
        consecutive=consecutive,
        needed=gate.passes,
        probes=probes,
        failures=failures,
        seconds=time.monotonic() - started,
        detail=detail,
    )


async def retire(
    config: ControlPlaneConfig,
    *,
    app_id: str,
    keep: int,
    drain: float,
    exit_timeout: float = EXIT_TIMEOUT,
) -> Retired:
    """Drain, stop, and then check that every other generation is really gone.

    Called only after the proxy is pointing at `keep`. By then no new request
    can arrive at anything else, so `drain` is exactly the time the requests
    already running are being given to finish — which is why it has to exceed
    the slowest thing the app serves, plus however long a kept-alive connection
    sits idle.

    `keep` comes from the database rather than from the worker's own idea of
    which generation it deployed, so this also stops a generation that some
    other worker started and lost the right to publish.
    """
    doomed = [one for one in await serving.generations(config, app_id) if one != keep]
    # Taken before anything is stopped. Stopping destroys the only description
    # of what was stopped — `reap` unlinks the run record, `docker rm` takes
    # the container's own answer with it — so the question has to be made
    # askable first or there is nothing left to check against.
    watchers = [await serving.watcher(config, app_id, one) for one in doomed]

    if drain > 0:
        await asyncio.sleep(drain)

    await serving.reap(config, app_id, keep={keep})
    return Retired(
        generations=doomed,
        drained=drain,
        exited=await _gone(watchers, exit_timeout),
    )


async def _gone(watchers: list[Watcher], timeout: float) -> bool:
    """Wait until not one of them answers any more.

    What counts as an answer is the runtime's business, and both of its answers
    are stronger than an exit code: a process whose port refuses connections
    has stopped serving whatever its pid says, and a container that reports
    `.State.Running` false has stopped whatever its port was reassigned to.
    """
    deadline = time.monotonic() + timeout
    while watchers and time.monotonic() < deadline:
        watchers = [one for one in watchers if await one()]
        if not watchers:
            return True
        await asyncio.sleep(EXIT_INTERVAL)
    return not watchers

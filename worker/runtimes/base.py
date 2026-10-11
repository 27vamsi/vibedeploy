"""What every way of serving an app has to agree about.

Two implementations share this: `local`, which is bare processes on this
machine, and `apprt`, which is two containers per generation. The pipeline
above them must not be able to tell which it is talking to, which is only true
if the type it gets back says the same thing either way.

So `Running` carries addresses and names, and deliberately not pids or ports.
It used to carry both, and that made the record a description of *how* an app
was started rather than of where it is — which is the one question the proxy,
the health gate and the reconciler all actually ask.

Two addresses, because they are two different answers:

  - `endpoint` is where the worker reaches the login gate. The worker runs on
    the host, so this is a host address.
  - `upstream` is what the proxy dials. Caddy runs in a container of its own,
    so `127.0.0.1` there is Caddy, and an upstream that happened to be right
    for the worker would be a 502 for everybody else.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Awaitable, Callable

# "Is any of this still answering?", asked repeatedly, about something that has
# already been told to stop. A callable rather than a predicate over an app id
# because by the time the answer matters the thing that could be looked up is
# gone: `reap` unlinks the run record, and `docker rm` takes the container's own
# answer with it. So whatever identifies the generation is captured first, and
# this is what is left holding it.
Watcher = Callable[[], Awaitable[bool]]

NO_ENTRYPOINT = (
    "We could not find the file that starts this app. V0.5 looks for `app.py`,"
    " `main.py` or `application.py` in the root of the repository, exposing an"
    " application called `app`."
)


class StartupFailed(RuntimeError):
    """Starting the app failed, in words a builder can act on."""


@dataclass(frozen=True)
class Running:
    app_id: str
    endpoint: str
    upstream: str
    # The two halves of a generation, by name. Empty for the `local` runtime,
    # which has processes and nothing to hang a name off — that absence is the
    # reason the containerised runtime exists at all, and the fields are not
    # filled in with pids, because a pid in a field called `container` would be
    # a lie that every caller would then have to know about.
    app_container: str = ""
    sidecar_container: str = ""
    # The three labels of `control_plane/generations.py`.
    generation: int = 0
    deployment_id: str = ""

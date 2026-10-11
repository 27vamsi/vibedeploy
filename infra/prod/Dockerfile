# The image our own three processes run in: control plane, worker, gateway.
#
# One image, three commands. They are the same codebase differing only in the
# entrypoint, and a second Dockerfile would exist only to leave the `docker`
# client out of two of them — which buys nothing, because reaching a daemon
# needs a socket and only the worker is given one.
#
# Our code is **mounted, not copied**, as it is in `infra/local/buildjob` and
# `infra/local/apprt`. That is not only to keep edits cheap here; it is
# load-bearing. The worker starts an app's containers by asking the host's
# daemon for them, and a `--volume` in that request is resolved by the daemon
# on the *host*, not inside the worker. So every path the worker hands over has
# to mean the same thing on both sides of its own filesystem, which it only can
# if the repository and the state root are mounted at the paths they already
# have. `infra/prod/docker-compose.yml` mounts `${VD_ROOT}:${VD_ROOT}` for
# exactly this reason, and a copied-in `/app` would break app deploys in a way
# that looks like a missing file.

FROM python:3.12-slim

# `git` because the worker clones a builder's repository itself (section 15),
# `docker` because it starts the build job and every deployed app, and
# `ca-certificates` because both of those reach the internet over TLS.
#
# `docker.io` is the client *and* the daemon package on Debian; only the client
# binary is used here and no daemon is started in this image. It talks to the
# host's daemon over the socket the compose file mounts into the worker alone.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        git \
        ca-certificates \
        docker.io \
    && rm -rf /var/lib/apt/lists/*

# Mirrors `[project].dependencies` in pyproject.toml, with exact versions
# rather than floors — the same split the other two Dockerfiles make. A floor
# is the right thing for a developer's venv, which should be free to pick up a
# fix, and the wrong thing for an image, where "the same image gives the same
# behaviour" is the only property worth having. These are the versions the
# suite is green on.
RUN pip install --no-cache-dir \
        fastapi==0.141.1 \
        uvicorn==0.53.0 \
        starlette==1.6.0 \
        httpx==0.28.1 \
        python-multipart==0.0.32 \
        SQLAlchemy==2.0.54 \
        asyncpg==0.31.0 \
        alembic==1.20.0 \
        argon2-cffi==25.1.0 \
        Jinja2==3.1.6 \
        cryptography==50.0.2 \
        PyYAML==6.0.3 \
        mcp==2.3.0

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# Not root by default. The control plane and the gateway have no reason to be,
# and the worker overrides it in the compose file with the reason stated there.
#
# The uid is fixed and above the apprt and buildjob ones so that files left in
# the state root by one release are still readable by the next.
RUN useradd --create-home --uid 10000 vd

USER vd

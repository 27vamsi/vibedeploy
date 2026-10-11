# The image a deployed app and its login gate run in. Readme.md section 16.
#
# One generic image for both halves of a generation, not one image per app.
# Everything that differs between two apps is mounted or passed in: our code
# read-only, the app's checkout read-only, the app's own packages in a volume
# the build job already filled, and the app's settings in the environment.
#
# That makes a deploy's cost one `pip install` into a volume rather than a
# `docker build`, which matters because the build job has to do that install
# anyway in order to run the app's migrations — this is the same packages,
# reused, instead of a second copy of them in an image layer.
#
# Per-app images are the better production answer and are deliberately one
# module away: `container.build_image` already exists and `worker/runtimes/`
# chooses the image by name.

FROM python:3.12-slim

# Three groups, and the reason each is baked in rather than taken from the
# app's `requirements.txt`:
#
#   - **uvicorn** is the launcher. Running an app must not depend on the app
#     having remembered to pin its own server, exactly as running an app's
#     migrations must not depend on it having pinned Alembic.
#   - **starlette, httpx and python-multipart** are the login gate's, not the
#     app's. The gate is ours and runs beside app code that may have pinned
#     anything. `python-multipart` is not optional despite the sign-in form
#     being urlencoded rather than multipart: Starlette's `request.form()`
#     asserts on it before it looks at the content type, so without it the gate
#     comes up healthy and then answers every sign-in with a 500.
#   - **SQLAlchemy and both synchronous drivers plus asyncpg** are the identity
#     shim's floor. The shim hooks `sqlalchemy.engine.Engine`, so it needs a
#     SQLAlchemy to hook; an app that ships its own gets its own, because the
#     deps volume goes *ahead* of site-packages on `PYTHONPATH`. Which driver a
#     plain `postgresql://` URL means is SQLAlchemy's default and that changed
#     between major versions, so an app that never thought about it works
#     either way.
RUN pip install --no-cache-dir \
        uvicorn==0.53.0 \
        starlette==1.6.0 \
        httpx==0.28.1 \
        python-multipart==0.0.32 \
        SQLAlchemy==2.0.54 \
        asyncpg==0.31.0 \
        psycopg2-binary==2.9.10 \
        "psycopg[binary]==3.2.12"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# Not root, and not the worker's user either — which was the whole problem with
# running a deployed app as a subprocess: it shared the worker's filesystem,
# its environment and therefore its credentials.
RUN useradd --create-home --uid 10002 vdapp

# The app's own packages arrive here from the deps volume the build job filled.
# It is mounted read-only here — the app has no business editing the packages
# it was proved with — so this only has to be traversable, not owned. The
# directory still has to exist: a volume mounted onto a path the image does not
# have gets created root-owned, and this container is not root.
RUN mkdir -p /vd/deps && chown vdapp:vdapp /vd/deps

# Where the app's checkout is mounted. Read-only: an app must not be able to
# edit the tree it was proved against.
RUN mkdir -p /vd/app /vd/src && chown vdapp:vdapp /vd/app /vd/src

USER vdapp
WORKDIR /vd/app
ENTRYPOINT []

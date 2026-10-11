# The image an app's own migrations run in. Readme.md section 3 rule 11.
#
# Our build job's code is mounted read-only rather than copied in, so editing
# `buildjob/` does not mean rebuilding this. What is baked in is only the third
# party packages it imports, which is why this file changes about once a year.
#
# Stands in for CodeBuild. The arrangement is the same: a container with the
# app's checkout, a throwaway database to point at, and one token to report
# home with.

FROM python:3.12-slim

# Nothing here needs a compiler: every wheel is published for this platform.
#
# Alembic and a *synchronous* driver are ours, not the app's, and they are baked
# in rather than taken from the app's `requirements.txt`: running an app's
# migrations must not depend on the app having remembered to pin the tool that
# runs them. The app's own packages are installed separately, at deploy time,
# into a writable mount (`buildjob/deps.py`) and go **ahead** of these on the
# path, so an app that pins its own SQLAlchemy gets its own.
#
# *Both* synchronous drivers, and that is not belt and braces. We hand Alembic
# a plain `postgresql://` URL, and which driver that means is SQLAlchemy's
# default, which changed from psycopg2 to psycopg 3 between major versions. The
# app chooses the SQLAlchemy on the path, so the app chooses the default — and
# an app that did not think about it at all would otherwise fail on whichever
# one we had not installed. Naming the driver in the URL instead would be worse:
# `DATABASE_URL` would stop being a URL anything but SQLAlchemy can read.
RUN pip install --no-cache-dir \
        asyncpg==0.30.0 \
        httpx==0.28.1 \
        alembic==1.20.0 \
        SQLAlchemy==2.0.54 \
        psycopg2-binary==2.9.10 \
        "psycopg[binary]==3.2.12"

# Our code and the app's checkout are both mounted. `/vd/src` is on the path so
# that `python -m buildjob.run` finds it without an install step.
ENV PYTHONPATH=/vd/src \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# Not root. App migrations get no reason to care who they run as, and every
# reason not to be able to write to anything of ours.
RUN useradd --create-home --uid 10001 vdbuild

# The app's own packages are mounted here from a docker volume, and a volume
# being created for the first time copies this directory's ownership. Without
# it the volume would belong to root and the container, correctly, is not root.
RUN mkdir -p /vd/deps && chown vdbuild:vdbuild /vd/deps

USER vdbuild

WORKDIR /vd
ENTRYPOINT []
CMD ["python", "-m", "buildjob.run", "--spec", "/vd/spec.json"]

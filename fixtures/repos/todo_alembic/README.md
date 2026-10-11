# The same todo app, with Alembic

`fixtures/repos/todo` is the same application with a folder of plain `.sql`
migrations. This one exists because almost nothing an AI writes looks like that:
it writes SQLAlchemy models and `alembic revision --autogenerate`, and an
Alembic project is not a folder of SQL we can read and run. It is the app's own
Python, with an `env.py` that imports the app and opens its own connection.

So this fixture is deliberately the awkward shape rather than the convenient
one:

  - `alembic/env.py` does `from models import Base`, so running the migrations
    means **importing the app**, which means having what the app imports. That
    is what `buildjob/deps.py` is for.
  - it reads `DATABASE_URL` and falls back to `sqlalchemy.url`, because real
    `env.py` files do one or the other and there is no telling which.
    `buildjob/migrate_alembic.py` sets both to the same thing.
  - `alembic.ini` has `prepend_sys_path = .`, which only works if the working
    directory is the checkout.

The schema it builds is the same two tables as `../todo`, on purpose: the access
model derived from it should be the same model, so a difference is a bug in the
Alembic path and not a different app.

`base/` plus `branches/<name>/` is the overlay layout `worker/pipeline/clone.py`
uses for fixtures — see `../todo/README.md`.

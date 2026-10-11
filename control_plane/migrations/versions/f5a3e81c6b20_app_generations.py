"""the generation that fences everything outside this database

Revision ID: f5a3e81c6b20
Revises: e4b2c7d91f38
Create Date: 2026-10-10 00:00:00.000000

`jobs.fence` already stops a stale worker writing to its job row. It does not
stop that worker from starting an app process, because nothing can: the process
is on the other side of the database and the worker does not know it is stale.

So the fence is extended to cover the thing that actually matters, which is not
the process but the *route*. `apps.desired_generation` goes up once per
deployment queued; `deployments.generation` is the number it was queued under;
and the single write that points traffic at a deployment names that number in
its `WHERE`. A stale worker's process ends up running and unreachable, which is
a wasted process rather than a wrong answer.

Existing rows get 0 on both sides, which is consistent: whatever is live now was
queued under the generation the app is still on.
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = 'f5a3e81c6b20'
down_revision = 'e4b2c7d91f38'
branch_labels = None
depends_on = None

SCHEMA = 'vd_control'


def upgrade() -> None:
    op.add_column(
        'apps',
        sa.Column(
            'desired_generation', sa.Integer(), nullable=False, server_default='0'
        ),
        schema=SCHEMA,
    )
    op.add_column(
        'deployments',
        sa.Column('generation', sa.Integer(), nullable=False, server_default='0'),
        schema=SCHEMA,
    )


def downgrade() -> None:
    op.drop_column('deployments', 'generation', schema=SCHEMA)
    op.drop_column('apps', 'desired_generation', schema=SCHEMA)

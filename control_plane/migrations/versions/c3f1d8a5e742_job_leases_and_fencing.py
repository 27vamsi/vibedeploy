"""job leases and fencing

Revision ID: c3f1d8a5e742
Revises: b17c4e9f2a08
Create Date: 2026-10-10 00:00:00.000000

`FOR UPDATE SKIP LOCKED` ends at commit, and the worker commits the claim before
it starts work. Everything after that was protected by `locked_at` plus a fixed
thirty minute cutoff, which gets both halves wrong: a crash stalls the queue for
half an hour, and a worker that was merely paused can come back and write a
verdict about a job somebody else has since taken over.

`lease_expires_at` replaces the cutoff with a deadline the holder renews, so the
timeout can be short. `fence` is what makes the handover safe: it only ever goes
up, and every write a worker makes to its own job names the number it was given,
so a worker that has been replaced cannot write at all.

Existing `running` rows are given a lease in the past on purpose. They are rows
left behind by a worker from before this migration, which is exactly the case
"reclaim it" exists for.
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = 'c3f1d8a5e742'
down_revision = 'b17c4e9f2a08'
branch_labels = None
depends_on = None

SCHEMA = 'vd_control'


def upgrade() -> None:
    op.add_column(
        'jobs',
        sa.Column('lease_expires_at', sa.DateTime(timezone=True), nullable=True),
        schema=SCHEMA,
    )
    op.add_column(
        'jobs',
        sa.Column('fence', sa.Integer(), nullable=False, server_default='0'),
        schema=SCHEMA,
    )
    op.execute(
        f"UPDATE {SCHEMA}.jobs SET lease_expires_at = now() - interval '1 second'"
        " WHERE status = 'running'"
    )
    op.drop_index('ix_jobs_claim', table_name='jobs', schema=SCHEMA)
    op.create_index(
        'ix_jobs_claim',
        'jobs',
        ['status', 'run_after', 'lease_expires_at'],
        schema=SCHEMA,
    )


def downgrade() -> None:
    op.drop_index('ix_jobs_claim', table_name='jobs', schema=SCHEMA)
    op.create_index(
        'ix_jobs_claim', 'jobs', ['status', 'run_after'], schema=SCHEMA
    )
    op.drop_column('jobs', 'fence', schema=SCHEMA)
    op.drop_column('jobs', 'lease_expires_at', schema=SCHEMA)

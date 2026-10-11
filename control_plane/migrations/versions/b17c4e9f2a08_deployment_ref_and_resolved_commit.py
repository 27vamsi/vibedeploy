"""deployment ref and resolved commit

Revision ID: b17c4e9f2a08
Revises: d5a5c286a430
Create Date: 2026-10-09 00:00:00.000000

A deployment used to carry one string that was both "what somebody asked for"
and "what was built". Those are different things the moment a repository is a
real git repository: a branch moves, and `verify` has to be able to re-check out
the exact commit `plan` resolved.

`ref` is what was asked for; `commit_sha` is what git resolved, and is null
until the clone has happened.
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = 'b17c4e9f2a08'
down_revision = 'd5a5c286a430'
branch_labels = None
depends_on = None

SCHEMA = 'vd_control'


def upgrade() -> None:
    op.add_column(
        'deployments',
        sa.Column('ref', sa.Text(), nullable=True),
        schema=SCHEMA,
    )
    # Existing rows were created when the single column held the ref.
    op.execute(f'UPDATE {SCHEMA}.deployments SET ref = commit_sha WHERE ref IS NULL')
    op.alter_column('deployments', 'ref', nullable=False, schema=SCHEMA)
    op.alter_column('deployments', 'commit_sha', nullable=True, schema=SCHEMA)


def downgrade() -> None:
    op.execute(
        f'UPDATE {SCHEMA}.deployments SET commit_sha = ref WHERE commit_sha IS NULL'
    )
    op.alter_column('deployments', 'commit_sha', nullable=False, schema=SCHEMA)
    op.drop_column('deployments', 'ref', schema=SCHEMA)

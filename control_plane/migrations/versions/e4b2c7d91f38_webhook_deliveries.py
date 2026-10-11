"""webhook deliveries and the ref an app tracks

Revision ID: e4b2c7d91f38
Revises: c3f1d8a5e742
Create Date: 2026-10-10 00:00:00.000000

GitHub retries a delivery it did not get a 2xx for, and it retries with the same
delivery id. The unique index here is what makes the second attempt do nothing:
the handler inserts this row first, and a duplicate insert is the answer rather
than something it goes looking for.

`apps.tracked_ref` exists because "a push deploys" has to mean a push to one
named branch. Existing rows get `main`, which is the branch they would have been
deploying by hand.
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = 'e4b2c7d91f38'
down_revision = 'c3f1d8a5e742'
branch_labels = None
depends_on = None

SCHEMA = 'vd_control'


def upgrade() -> None:
    op.add_column(
        'apps',
        sa.Column(
            'tracked_ref', sa.Text(), nullable=False, server_default='main'
        ),
        schema=SCHEMA,
    )
    op.create_table(
        'webhook_deliveries',
        sa.Column(
            'id',
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            nullable=False,
        ),
        sa.Column('source', sa.Text(), nullable=False),
        sa.Column('delivery_id', sa.Text(), nullable=False),
        sa.Column('event', sa.Text(), nullable=False),
        sa.Column('ref', sa.Text(), nullable=True),
        sa.Column('head_sha', sa.Text(), nullable=True),
        sa.Column('outcome', sa.Text(), nullable=True),
        sa.Column('detail', sa.Text(), nullable=True),
        sa.Column('deployments', postgresql.JSONB(), nullable=True),
        sa.Column(
            'created_at',
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint(
            'source', 'delivery_id', name='uq_webhook_deliveries_id'
        ),
        schema=SCHEMA,
    )


def downgrade() -> None:
    op.drop_table('webhook_deliveries', schema=SCHEMA)
    op.drop_column('apps', 'tracked_ref', schema=SCHEMA)

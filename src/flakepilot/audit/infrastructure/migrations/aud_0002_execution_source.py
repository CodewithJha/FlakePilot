"""Accept execution envelopes in the audit log.

Revision ID: aud_0002
Revises: exec_0001
Create Date: 2026-10-21
"""

from alembic import op

revision = "aud_0002"
down_revision = "exec_0001"
branch_labels = None
depends_on = None

_WITH = "source IN ('iam', 'ti', 'diag', 'exec')"
_WITHOUT = "source IN ('iam', 'ti', 'diag')"


def upgrade() -> None:
    op.drop_constraint("ck_events_source", "events", schema="aud", type_="check")
    op.create_check_constraint(op.f("ck_events_source"), "events", _WITH, schema="aud")


def downgrade() -> None:
    op.drop_constraint("ck_events_source", "events", schema="aud", type_="check")
    op.create_check_constraint(
        op.f("ck_events_source"), "events", _WITHOUT, schema="aud"
    )

"""Accept repository outbox envelopes in the audit log.

Revision ID: aud_0003
Revises: repo_0002
Create Date: 2026-10-23
"""

from alembic import op

revision = "aud_0003"
down_revision = "repo_0002"
branch_labels = None
depends_on = None

_WITH = "source IN ('iam', 'ti', 'diag', 'exec', 'repo')"
_WITHOUT = "source IN ('iam', 'ti', 'diag', 'exec')"


def upgrade() -> None:
    op.drop_constraint("ck_events_source", "events", schema="aud", type_="check")
    op.create_check_constraint(op.f("ck_events_source"), "events", _WITH, schema="aud")


def downgrade() -> None:
    op.drop_constraint("ck_events_source", "events", schema="aud", type_="check")
    op.create_check_constraint(
        op.f("ck_events_source"), "events", _WITHOUT, schema="aud"
    )

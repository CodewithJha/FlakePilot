"""One unverified row per repository, selector and SHA.

Revision ID: exec_0003
Revises: exec_0002
Create Date: 2026-10-21
"""

import sqlalchemy as sa
from alembic import op

revision = "exec_0003"
down_revision = "exec_0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        "uq_exec_executions_unverified",
        "executions",
        ["org_id", "repo_id", "selector", "sha"],
        unique=True,
        schema="exec",
        postgresql_where=sa.text("reason = 'source_unverified'"),
    )


def downgrade() -> None:
    op.drop_index(
        "uq_exec_executions_unverified", table_name="executions", schema="exec"
    )

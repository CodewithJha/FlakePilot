"""One closed-mode row per repository, selector and SHA.

Revision ID: exec_0002
Revises: aud_0002
Create Date: 2026-10-21
"""

import sqlalchemy as sa
from alembic import op

revision = "exec_0002"
down_revision = "aud_0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        "uq_exec_executions_closed",
        "executions",
        ["org_id", "repo_id", "selector", "sha"],
        unique=True,
        schema="exec",
        postgresql_where=sa.text("reason = 'execution_closed'"),
    )


def downgrade() -> None:
    op.drop_index("uq_exec_executions_closed", table_name="executions", schema="exec")

"""Registered GitHub repositories.

Revision ID: repo_0001
Revises: aud_0001
Create Date: 2026-10-21
"""

from datetime import datetime

import sqlalchemy as sa
from alembic import op

revision = "repo_0001"
down_revision = "aud_0001"
branch_labels = None
depends_on = None


def _time(name: str) -> sa.Column[datetime]:
    return sa.Column(name, sa.DateTime(timezone=True), nullable=False)


def upgrade() -> None:
    op.execute("CREATE SCHEMA repo")
    op.create_table(
        "repositories",
        sa.Column("id", sa.Uuid(as_uuid=False), nullable=False),
        sa.Column("org_id", sa.Uuid(as_uuid=False), nullable=False),
        sa.Column("host", sa.Text(), nullable=False),
        sa.Column("owner", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        _time("created_at"),
        sa.CheckConstraint("host = 'github'", name=op.f("ck_repositories_host")),
        sa.CheckConstraint(
            "owner ~ '^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})$'",
            name=op.f("ck_repositories_owner"),
        ),
        sa.CheckConstraint(
            "name ~ '^[A-Za-z0-9._-]{1,100}$' AND strpos(name, '..') = 0 "
            "AND left(name, 1) <> '.'",
            name=op.f("ck_repositories_name"),
        ),
        sa.UniqueConstraint(
            "host", "owner", "name", name="uq_repositories_coordinates"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_repositories")),
        schema="repo",
    )


def downgrade() -> None:
    op.execute("DROP SCHEMA repo CASCADE")

"""IAM outbox (ADR-012) and case-insensitively unique org names (owner decision B).

The unique index fails on a database that already holds org names differing only in case;
resolve those first.

Revision ID: iam_0002
Revises: int_secrets_0001
Create Date: 2026-10-21
"""

from datetime import datetime

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "iam_0002"
down_revision = "int_secrets_0001"
branch_labels = None
depends_on = None


def _id(name: str, nullable: bool = False) -> sa.Column[str]:
    return sa.Column(name, sa.Uuid(as_uuid=False), nullable=nullable)


def _time(name: str, nullable: bool = False) -> sa.Column[datetime]:
    return sa.Column(name, sa.DateTime(timezone=True), nullable=nullable)


def upgrade() -> None:
    op.create_index(
        "uq_orgs_lower_name",
        "orgs",
        [sa.text("lower(name)")],
        unique=True,
        schema="iam",
    )
    op.create_table(
        "outbox",
        _id("event_id"),
        sa.Column("seq", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("schema_version", sa.SmallInteger(), nullable=False),
        _time("occurred_at"),
        _id("org_id", nullable=True),
        _id("repo_id", nullable=True),
        sa.Column("actor_type", sa.Text(), nullable=False),
        _id("actor_id", nullable=True),
        sa.Column("channel", sa.Text(), nullable=False),
        sa.Column("correlation_id", sa.Text(), nullable=False),
        sa.Column("subject_type", sa.Text(), nullable=False),
        _id("subject_id"),
        sa.Column("before", sa.Text(), nullable=True),
        sa.Column("after", sa.Text(), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        _time("published_at", nullable=True),
        sa.CheckConstraint(
            "actor_type IN ('user', 'ci_token', 'operator_cli', 'system')",
            name=op.f("ck_outbox_actor_type"),
        ),
        sa.CheckConstraint(
            "(actor_type IN ('user', 'ci_token')) = (actor_id IS NOT NULL)",
            name=op.f("ck_outbox_actor_id"),
        ),
        sa.CheckConstraint(
            "channel IN ('http', 'cli')", name=op.f("ck_outbox_channel")
        ),
        sa.PrimaryKeyConstraint("event_id", name=op.f("pk_outbox")),
        schema="iam",
    )
    op.create_index(
        "ix_iam_outbox_unpublished",
        "outbox",
        ["seq"],
        schema="iam",
        postgresql_where=sa.text("published_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_iam_outbox_unpublished", table_name="outbox", schema="iam")
    op.drop_table("outbox", schema="iam")
    op.drop_index("uq_orgs_lower_name", table_name="orgs", schema="iam")

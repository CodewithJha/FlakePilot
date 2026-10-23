"""The repository context's own outbox (ADR-012), born with the delivery columns.

Registered bindings become auditable state changes; the worker relay publishes these
envelopes to AUD. The table mirrors the self-contained `exec` outbox DDL.

Revision ID: repo_0002
Revises: exec_0003Create Date: 2026-10-23
"""

from datetime import datetime

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "repo_0002"
down_revision = "exec_0003"
branch_labels = None
depends_on = None


def _time(name: str, nullable: bool = False) -> sa.Column[datetime]:
    return sa.Column(name, sa.DateTime(timezone=True), nullable=nullable)


def upgrade() -> None:
    op.create_table(
        "outbox",
        sa.Column("event_id", sa.Uuid(as_uuid=False), nullable=False),
        sa.Column("seq", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("schema_version", sa.SmallInteger(), nullable=False),
        _time("occurred_at"),
        sa.Column("org_id", sa.Uuid(as_uuid=False)),
        sa.Column("repo_id", sa.Uuid(as_uuid=False)),
        sa.Column("actor_type", sa.Text(), nullable=False),
        sa.Column("actor_id", sa.Uuid(as_uuid=False)),
        sa.Column("channel", sa.Text(), nullable=False),
        sa.Column("correlation_id", sa.Text(), nullable=False),
        sa.Column("subject_type", sa.Text(), nullable=False),
        sa.Column("subject_id", sa.Uuid(as_uuid=False), nullable=False),
        sa.Column("before", sa.Text()),
        sa.Column("after", sa.Text()),
        sa.Column("reason", sa.Text()),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        _time("published_at", nullable=True),
        sa.Column(
            "attempts", sa.SmallInteger(), nullable=False, server_default=sa.text("0")
        ),
        sa.Column("last_error", sa.Text()),
        _time("dead_at", nullable=True),
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
        sa.CheckConstraint(
            "attempts BETWEEN 0 AND 20", name=op.f("ck_outbox_attempts")
        ),
        sa.CheckConstraint(
            "last_error IS NULL OR last_error ~ '^[a-z][a-z0-9_]{0,39}$'",
            name=op.f("ck_outbox_last_error"),
        ),
        sa.CheckConstraint(
            "published_at IS NULL OR dead_at IS NULL",
            name=op.f("ck_outbox_terminal"),
        ),
        sa.PrimaryKeyConstraint("event_id", name=op.f("pk_outbox")),
        schema="repo",
    )
    op.create_index(
        "ix_repo_outbox_unpublished",
        "outbox",
        ["seq"],
        schema="repo",
        postgresql_where=sa.text("published_at IS NULL AND dead_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_repo_outbox_unpublished", table_name="outbox", schema="repo")
    op.drop_table("outbox", schema="repo")

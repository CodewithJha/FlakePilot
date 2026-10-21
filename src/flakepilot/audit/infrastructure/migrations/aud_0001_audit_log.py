"""Audit log, and outbox delivery columns for iam, ti, and diag.

Revision ID: aud_0001
Revises: orch_0001
Create Date: 2026-10-21
"""

from datetime import datetime

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "aud_0001"
down_revision = "orch_0001"
branch_labels = None
depends_on = None

_OUTBOXES = (
    ("iam", "ix_iam_outbox_unpublished"),
    ("ti", "ix_ti_outbox_unpublished"),
    ("diag", "ix_diag_outbox_unpublished"),
)


def _id(name: str, nullable: bool = False) -> sa.Column[str]:
    return sa.Column(name, sa.Uuid(as_uuid=False), nullable=nullable)


def _time(name: str) -> sa.Column[datetime]:
    return sa.Column(name, sa.DateTime(timezone=True), nullable=False)


def upgrade() -> None:
    op.execute("CREATE SCHEMA aud")
    op.create_table(
        "events",
        sa.Column("event_id", sa.Uuid(as_uuid=False), nullable=False),
        sa.Column("source", sa.Text(), nullable=False),
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
        _time("recorded_at"),
        sa.CheckConstraint(
            "source IN ('iam', 'ti', 'diag')", name=op.f("ck_events_source")
        ),
        sa.CheckConstraint("schema_version = 1", name=op.f("ck_events_schema_version")),
        sa.CheckConstraint(
            "actor_type IN ('user', 'ci_token', 'operator_cli', 'system')",
            name=op.f("ck_events_actor_type"),
        ),
        sa.CheckConstraint(
            "(actor_type IN ('user', 'ci_token')) = (actor_id IS NOT NULL)",
            name=op.f("ck_events_actor_id"),
        ),
        sa.CheckConstraint(
            "channel IN ('http', 'cli')", name=op.f("ck_events_channel")
        ),
        sa.PrimaryKeyConstraint("event_id", name=op.f("pk_events")),
        schema="aud",
    )
    for schema, index in _OUTBOXES:
        _delivery_columns(schema, index)


def downgrade() -> None:
    for schema, index in reversed(_OUTBOXES):
        _drop_delivery_columns(schema, index)
    op.execute("DROP SCHEMA aud CASCADE")


def _delivery_columns(schema: str, index: str) -> None:
    op.add_column(
        "outbox",
        sa.Column(
            "attempts", sa.SmallInteger(), nullable=False, server_default=sa.text("0")
        ),
        schema=schema,
    )
    op.add_column(
        "outbox", sa.Column("last_error", sa.Text(), nullable=True), schema=schema
    )
    op.add_column(
        "outbox",
        sa.Column("dead_at", sa.DateTime(timezone=True), nullable=True),
        schema=schema,
    )
    op.create_check_constraint(
        op.f("ck_outbox_attempts"),
        "outbox",
        "attempts BETWEEN 0 AND 20",
        schema=schema,
    )
    op.create_check_constraint(
        op.f("ck_outbox_last_error"),
        "outbox",
        "last_error IS NULL OR last_error ~ '^[a-z][a-z0-9_]{0,39}$'",
        schema=schema,
    )
    op.create_check_constraint(
        op.f("ck_outbox_terminal"),
        "outbox",
        "published_at IS NULL OR dead_at IS NULL",
        schema=schema,
    )
    op.drop_index(index, table_name="outbox", schema=schema)
    op.create_index(
        index,
        "outbox",
        ["seq"],
        schema=schema,
        postgresql_where=sa.text("published_at IS NULL AND dead_at IS NULL"),
    )


def _drop_delivery_columns(schema: str, index: str) -> None:
    op.drop_index(index, table_name="outbox", schema=schema)
    op.create_index(
        index,
        "outbox",
        ["seq"],
        schema=schema,
        postgresql_where=sa.text("published_at IS NULL"),
    )
    for name in ("ck_outbox_terminal", "ck_outbox_last_error", "ck_outbox_attempts"):
        op.drop_constraint(name, "outbox", schema=schema, type_="check")
    for column in ("dead_at", "last_error", "attempts"):
        op.drop_column("outbox", column, schema=schema)

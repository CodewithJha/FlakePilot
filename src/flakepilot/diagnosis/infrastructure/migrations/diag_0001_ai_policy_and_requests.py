"""Diagnosis AI policy, request metadata, and outbox.

Revision ID: diag_0001
Revises: ti_0001
Create Date: 2026-10-21
"""

from datetime import datetime

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "diag_0001"
down_revision = "ti_0001"
branch_labels = None
depends_on = None

_LEVELS = "('disabled', 'evidence_only', 'evidence_and_source')"
_OUTCOMES = "('produced', 'no_hypothesis', 'rejected', 'unavailable')"
_CODE = "'^[a-z][a-z0-9_]{0,39}$'"


def _id(name: str, nullable: bool = False) -> sa.Column[str]:
    return sa.Column(name, sa.Uuid(as_uuid=False), nullable=nullable)


def _time(name: str, nullable: bool = False) -> sa.Column[datetime]:
    return sa.Column(name, sa.DateTime(timezone=True), nullable=nullable)


def upgrade() -> None:
    op.execute("CREATE SCHEMA diag")
    op.create_table(
        "ai_policies",
        _id("org_id"),
        sa.Column("level", sa.Text(), nullable=False),
        sa.Column(
            "retain_raw", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
        _time("updated_at"),
        sa.CheckConstraint(f"level IN {_LEVELS}", name=op.f("ck_ai_policies_level")),
        sa.PrimaryKeyConstraint("org_id", name=op.f("pk_ai_policies")),
        schema="diag",
    )
    op.create_table(
        "ai_requests",
        _id("id"),
        _id("org_id"),
        _id("repo_id"),
        _id("actor_id"),
        sa.Column("policy_level", sa.Text(), nullable=False),
        sa.Column("retain_raw", sa.Boolean(), nullable=False),
        sa.Column("provider", sa.Text(), nullable=False),
        sa.Column("model", sa.Text(), nullable=True),
        sa.Column("payload_sha256", sa.LargeBinary(), nullable=False),
        sa.Column("payload_size", sa.Integer(), nullable=False),
        sa.Column("outcome", sa.Text(), nullable=False),
        sa.Column("validation_reason", sa.Text(), nullable=True),
        sa.Column("linked_count", sa.Integer(), nullable=False),
        sa.Column("unverified_count", sa.Integer(), nullable=False),
        sa.Column("contradicted_count", sa.Integer(), nullable=False),
        sa.Column("raw_output", sa.Text(), nullable=True),
        _time("created_at"),
        sa.CheckConstraint(
            f"policy_level IN {_LEVELS}", name=op.f("ck_ai_requests_policy_level")
        ),
        sa.CheckConstraint(
            f"outcome IN {_OUTCOMES}", name=op.f("ck_ai_requests_outcome")
        ),
        sa.CheckConstraint(
            "octet_length(payload_sha256) = 32",
            name=op.f("ck_ai_requests_payload_sha256_length"),
        ),
        sa.CheckConstraint(
            "payload_size >= 0", name=op.f("ck_ai_requests_payload_size")
        ),
        sa.CheckConstraint(
            "linked_count >= 0", name=op.f("ck_ai_requests_linked_count")
        ),
        sa.CheckConstraint(
            "unverified_count >= 0", name=op.f("ck_ai_requests_unverified_count")
        ),
        sa.CheckConstraint(
            "contradicted_count >= 0", name=op.f("ck_ai_requests_contradicted_count")
        ),
        sa.CheckConstraint(
            "raw_output IS NULL OR retain_raw",
            name=op.f("ck_ai_requests_raw_output_opt_in"),
        ),
        sa.CheckConstraint(
            "outcome <> 'rejected' OR validation_reason IS NOT NULL",
            name=op.f("ck_ai_requests_rejected_reason"),
        ),
        sa.CheckConstraint(
            "raw_output IS NULL OR octet_length(raw_output) <= 65536",
            name=op.f("ck_ai_requests_raw_output_length"),
        ),
        sa.CheckConstraint(f"provider ~ {_CODE}", name=op.f("ck_ai_requests_provider")),
        sa.CheckConstraint(
            f"model IS NULL OR model ~ {_CODE}", name=op.f("ck_ai_requests_model")
        ),
        sa.CheckConstraint(
            f"validation_reason IS NULL OR validation_reason ~ {_CODE}",
            name=op.f("ck_ai_requests_validation_reason"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_ai_requests")),
        schema="diag",
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
        schema="diag",
    )
    op.create_index(
        "ix_diag_outbox_unpublished",
        "outbox",
        ["seq"],
        schema="diag",
        postgresql_where=sa.text("published_at IS NULL"),
    )


def downgrade() -> None:
    op.execute("DROP SCHEMA diag CASCADE")

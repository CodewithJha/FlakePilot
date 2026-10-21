"""Browser reproduction records and the execution outbox.

Revision ID: exec_0001
Revises: repo_0001
Create Date: 2026-10-21
"""

from datetime import datetime

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "exec_0001"
down_revision = "repo_0001"
branch_labels = None
depends_on = None

_CODE = "'^[a-z][a-z0-9_]{0,39}$'"
_OUTCOME = (
    "(state IN ('scheduled', 'running') AND outcome IS NULL AND reason IS NULL) "
    "OR (state = 'completed' AND ("
    "(outcome = 'success' AND reason = 'passed') "
    "OR (outcome = 'test_failure' AND reason = 'failed') "
    "OR (outcome = 'timeout' AND reason = 'timeout') "
    "OR (outcome = 'invalid_result' AND reason = 'invalid_result') "
    "OR (outcome = 'infra_failure' AND reason ~ " + _CODE + ") "
    "OR (outcome = 'inconclusive' AND reason IN "
    "('execution_closed', 'source_unverified'))"
    ")) OR (state = 'cancelled' AND outcome = 'cancelled' AND reason = 'cancelled')"
)


def _time(name: str, nullable: bool = False) -> sa.Column[datetime]:
    return sa.Column(name, sa.DateTime(timezone=True), nullable=nullable)


def upgrade() -> None:
    op.execute("CREATE SCHEMA exec")
    op.create_table(
        "executions",
        sa.Column("id", sa.Uuid(as_uuid=False), nullable=False),
        sa.Column("org_id", sa.Uuid(as_uuid=False), nullable=False),
        sa.Column("repo_id", sa.Uuid(as_uuid=False), nullable=False),
        sa.Column("selector", sa.Text(), nullable=False),
        sa.Column("sha", sa.Text(), nullable=False),
        sa.Column("state", sa.Text(), nullable=False),
        sa.Column("outcome", sa.Text()),
        sa.Column("reason", sa.Text()),
        sa.Column("generation", sa.SmallInteger(), nullable=False),
        sa.Column("lease_owner", sa.Text()),
        _time("lease_expires_at", nullable=True),
        sa.Column("job_id", sa.Uuid(as_uuid=False)),
        sa.Column(
            "evidence_partial",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
        sa.Column(
            "cancel_requested",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
        sa.Column("actor_type", sa.Text(), nullable=False),
        sa.Column("actor_id", sa.Uuid(as_uuid=False)),
        sa.Column("channel", sa.Text(), nullable=False),
        sa.Column("correlation_id", sa.Text(), nullable=False),
        _time("created_at"),
        _time("updated_at"),
        sa.CheckConstraint(
            "selector ~ '^[A-Za-z0-9_.:-]{1,120}$' AND strpos(selector, '..') = 0",
            name=op.f("ck_executions_selector"),
        ),
        sa.CheckConstraint("sha ~ '^[0-9a-f]{40}$'", name=op.f("ck_executions_sha")),
        sa.CheckConstraint(
            "state IN ('scheduled', 'running', 'completed', 'cancelled')",
            name=op.f("ck_executions_state"),
        ),
        sa.CheckConstraint(_OUTCOME, name=op.f("ck_executions_outcome")),
        sa.CheckConstraint(
            "generation BETWEEN 0 AND 20", name=op.f("ck_executions_generation")
        ),
        sa.CheckConstraint(
            "(state = 'running') = "
            "(lease_owner IS NOT NULL AND lease_expires_at IS NOT NULL)",
            name=op.f("ck_executions_lease"),
        ),
        sa.CheckConstraint(
            "lease_owner IS NULL OR lease_owner ~ '^[a-z0-9][a-z0-9-]{0,63}$'",
            name=op.f("ck_executions_lease_owner"),
        ),
        sa.CheckConstraint(
            "actor_type IN ('user', 'ci_token', 'operator_cli', 'system')",
            name=op.f("ck_executions_actor_type"),
        ),
        sa.CheckConstraint(
            "(actor_type IN ('user', 'ci_token')) = (actor_id IS NOT NULL)",
            name=op.f("ck_executions_actor_id"),
        ),
        sa.CheckConstraint(
            "channel IN ('http', 'cli')", name=op.f("ck_executions_channel")
        ),
        sa.CheckConstraint(
            "correlation_id ~ '^[A-Za-z0-9._:-]{1,100}$'",
            name=op.f("ck_executions_correlation_id"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_executions")),
        schema="exec",
    )
    op.create_index(
        "uq_exec_executions_active",
        "executions",
        ["org_id", "repo_id", "selector", "sha"],
        unique=True,
        schema="exec",
        postgresql_where=sa.text("state = 'scheduled' OR state = 'running'"),
    )
    op.create_index(
        "ix_exec_executions_expired",
        "executions",
        ["lease_expires_at"],
        schema="exec",
        postgresql_where=sa.text("state = 'running'"),
    )
    op.create_table(
        "execution_artifacts",
        sa.Column("id", sa.Uuid(as_uuid=False), nullable=False),
        sa.Column("execution_id", sa.Uuid(as_uuid=False), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("sha256", sa.LargeBinary(), nullable=False),
        sa.Column("size", sa.Integer(), nullable=False),
        sa.Column("artifact_key", sa.Text(), nullable=False),
        sa.CheckConstraint(
            "kind IN ('result', 'screenshot')",
            name=op.f("ck_execution_artifacts_kind"),
        ),
        sa.CheckConstraint(
            "octet_length(sha256) = 32",
            name=op.f("ck_execution_artifacts_sha256"),
        ),
        sa.CheckConstraint(
            "size BETWEEN 1 AND 1048576",
            name=op.f("ck_execution_artifacts_size"),
        ),
        sa.CheckConstraint(
            "artifact_key ~ '^exec/[0-9a-f-]{36}/[0-9a-f-]{36}/sha256/[0-9a-f]{64}$'",
            name=op.f("ck_execution_artifacts_artifact_key"),
        ),
        sa.ForeignKeyConstraint(
            ["execution_id"],
            ["exec.executions.id"],
            name=op.f("fk_execution_artifacts_execution_id_executions"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_execution_artifacts")),
        schema="exec",
    )
    op.create_index(
        "ix_exec_execution_artifacts_execution",
        "execution_artifacts",
        ["execution_id"],
        schema="exec",
    )
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
        schema="exec",
    )
    op.create_index(
        "ix_exec_outbox_unpublished",
        "outbox",
        ["seq"],
        schema="exec",
        postgresql_where=sa.text("published_at IS NULL AND dead_at IS NULL"),
    )


def downgrade() -> None:
    op.execute("DROP SCHEMA exec CASCADE")

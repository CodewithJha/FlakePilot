"""Durable job table (ADR-006).

Revision ID: orch_0001
Revises: diag_0001
Create Date: 2026-10-21
"""

from datetime import datetime

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "orch_0001"
down_revision = "diag_0001"
branch_labels = None
depends_on = None

_TOKEN = "'^[A-Za-z0-9._:-]{1,100}$'"
_CODE = "'^[a-z][a-z0-9_]{0,39}$'"


def _time(name: str, nullable: bool = False) -> sa.Column[datetime]:
    return sa.Column(name, sa.DateTime(timezone=True), nullable=nullable)


def upgrade() -> None:
    op.execute("CREATE SCHEMA orch")
    op.create_table(
        "jobs",
        sa.Column("id", sa.Uuid(as_uuid=False), nullable=False),
        sa.Column("job_type", sa.Text(), nullable=False),
        sa.Column("payload_version", sa.SmallInteger(), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column("state", sa.Text(), nullable=False),
        _time("run_at"),
        sa.Column("attempts", sa.SmallInteger(), nullable=False),
        sa.Column("max_attempts", sa.SmallInteger(), nullable=False),
        sa.Column("lease_owner", sa.Text(), nullable=True),
        _time("lease_expires_at", nullable=True),
        _time("created_at"),
        _time("updated_at"),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("correlation_id", sa.Text(), nullable=True),
        sa.Column("causation_id", sa.Text(), nullable=True),
        sa.Column(
            "cancel_requested",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
        sa.CheckConstraint(
            "state IN ('scheduled', 'running', 'completed', 'failed', 'cancelled')",
            name=op.f("ck_jobs_state"),
        ),
        sa.CheckConstraint(
            "job_type ~ '^[a-z][a-z0-9_]{0,63}$'",
            name=op.f("ck_jobs_job_type"),
        ),
        sa.CheckConstraint("payload_version = 1", name=op.f("ck_jobs_payload_version")),
        sa.CheckConstraint(
            "octet_length(payload::text) <= 8192",
            name=op.f("ck_jobs_payload_size"),
        ),
        sa.CheckConstraint(
            "attempts >= 0 AND attempts <= max_attempts",
            name=op.f("ck_jobs_attempts"),
        ),
        sa.CheckConstraint(
            "max_attempts BETWEEN 1 AND 20",
            name=op.f("ck_jobs_max_attempts"),
        ),
        sa.CheckConstraint(
            "(state = 'running') = "
            "(lease_owner IS NOT NULL AND lease_expires_at IS NOT NULL)",
            name=op.f("ck_jobs_lease"),
        ),
        sa.CheckConstraint(
            "lease_owner IS NULL OR lease_owner ~ '^[a-z0-9][a-z0-9-]{0,63}$'",
            name=op.f("ck_jobs_lease_owner"),
        ),
        sa.CheckConstraint(
            f"last_error IS NULL OR last_error ~ {_CODE}",
            name=op.f("ck_jobs_last_error"),
        ),
        sa.CheckConstraint(
            f"correlation_id IS NULL OR correlation_id ~ {_TOKEN}",
            name=op.f("ck_jobs_correlation_id"),
        ),
        sa.CheckConstraint(
            f"causation_id IS NULL OR causation_id ~ {_TOKEN}",
            name=op.f("ck_jobs_causation_id"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_jobs")),
        schema="orch",
    )
    op.create_index(
        "ix_orch_jobs_due",
        "jobs",
        ["run_at", "id"],
        schema="orch",
        postgresql_where=sa.text("state = 'scheduled'"),
    )
    op.create_index(
        "ix_orch_jobs_expired",
        "jobs",
        ["lease_expires_at"],
        schema="orch",
        postgresql_where=sa.text("state = 'running'"),
    )


def downgrade() -> None:
    op.execute("DROP SCHEMA orch CASCADE")

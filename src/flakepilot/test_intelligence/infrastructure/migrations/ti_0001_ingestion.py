"""TI tables: uploads, runs, test identities, test results, outbox (ADR-013, ADR-012).

Revision ID: ti_0001
Revises: iam_0002
Create Date: 2026-10-21
"""

from datetime import datetime

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "ti_0001"
down_revision = "iam_0002"
branch_labels = None
depends_on = None

SCOPE = ["org_id", "repo_id"]


def _id(name: str, nullable: bool = False) -> sa.Column[str]:
    return sa.Column(name, sa.Uuid(as_uuid=False), nullable=nullable)


def _time(name: str, nullable: bool = False) -> sa.Column[datetime]:
    return sa.Column(name, sa.DateTime(timezone=True), nullable=nullable)


def upgrade() -> None:
    op.execute("CREATE SCHEMA ti")
    op.create_table(
        "uploads",
        _id("id"),
        _id("org_id"),
        _id("repo_id"),
        _id("token_id"),
        sa.Column("state", sa.Text(), nullable=False),
        sa.Column("reject_reason", sa.Text(), nullable=True),
        sa.Column("ci_source", sa.Text(), nullable=True),
        sa.Column("ci_run_id", sa.Text(), nullable=True),
        sa.Column("ci_attempt", sa.Integer(), nullable=True),
        sa.Column("job_name", sa.Text(), nullable=True),
        sa.Column("commit_sha", sa.Text(), nullable=True),
        sa.Column("ref", sa.Text(), nullable=True),
        sa.Column("environment", sa.Text(), nullable=True),
        _time("started_at", nullable=True),
        _time("finished_at", nullable=True),
        sa.Column("content_sha256", sa.LargeBinary(), nullable=True),
        sa.Column("size_bytes", sa.BigInteger(), nullable=True),
        sa.Column("content_type", sa.Text(), nullable=True),
        sa.Column("artifact_key", sa.Text(), nullable=True),
        _time("received_at"),
        sa.CheckConstraint(
            "state IN ('received', 'processing', 'processed', 'rejected', "
            "'processing_failed')",
            name=op.f("ck_uploads_state"),
        ),
        sa.CheckConstraint(
            "(state = 'rejected') = (reject_reason IS NOT NULL)",
            name=op.f("ck_uploads_reject_reason"),
        ),
        sa.CheckConstraint(
            "state <> 'processed' OR (ci_source IS NOT NULL AND ci_run_id IS NOT NULL "
            "AND ci_attempt IS NOT NULL AND job_name IS NOT NULL "
            "AND commit_sha IS NOT NULL AND ref IS NOT NULL "
            "AND environment IS NOT NULL AND started_at IS NOT NULL "
            "AND finished_at IS NOT NULL AND content_sha256 IS NOT NULL "
            "AND size_bytes IS NOT NULL AND content_type IS NOT NULL "
            "AND artifact_key IS NOT NULL)",
            name=op.f("ck_uploads_processed_complete"),
        ),
        sa.CheckConstraint(
            "content_sha256 IS NULL OR octet_length(content_sha256) = 32",
            name=op.f("ck_uploads_content_sha256_length"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_uploads")),
        sa.UniqueConstraint("org_id", "repo_id", "id", name="uq_uploads_scope_id"),
        schema="ti",
    )
    op.create_index(
        "uq_uploads_idempotency_key",
        "uploads",
        ["org_id", "repo_id", "ci_source", "ci_run_id", "ci_attempt", "job_name"],
        unique=True,
        schema="ti",
        postgresql_where=sa.text("state <> 'rejected'"),
    )
    op.create_table(
        "runs",
        _id("id"),
        _id("org_id"),
        _id("repo_id"),
        _id("upload_id"),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("commit_sha", sa.Text(), nullable=False),
        sa.Column("environment", sa.Text(), nullable=False),
        sa.Column("revision_status", sa.Text(), nullable=False),
        _time("received_at"),
        *(
            sa.Column(name, sa.Integer(), nullable=False)
            for name in ("tests", "attempts", "passed", "failed", "errored", "skipped")
        ),
        sa.CheckConstraint(
            "source IN ('ci_upload', 'sandbox')", name=op.f("ck_runs_source")
        ),
        sa.CheckConstraint(
            "revision_status IN ('unchecked', 'verified', 'unverified_revision')",
            name=op.f("ck_runs_revision_status"),
        ),
        sa.ForeignKeyConstraint(
            [*SCOPE, "upload_id"],
            ["ti.uploads.org_id", "ti.uploads.repo_id", "ti.uploads.id"],
            name=op.f("fk_runs_org_id_uploads"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_runs")),
        sa.UniqueConstraint("upload_id", name=op.f("uq_runs_upload_id")),
        sa.UniqueConstraint("org_id", "repo_id", "id", name="uq_runs_scope_id"),
        schema="ti",
    )
    op.create_table(
        "test_identities",
        _id("id"),
        _id("org_id"),
        _id("repo_id"),
        sa.Column("identity_key", sa.LargeBinary(), nullable=False),
        sa.Column("classname", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        _time("created_at"),
        sa.CheckConstraint(
            "octet_length(identity_key) = 32",
            name=op.f("ck_test_identities_identity_key_length"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_test_identities")),
        sa.UniqueConstraint(
            "org_id", "repo_id", "identity_key", name="uq_test_identities_identity_key"
        ),
        sa.UniqueConstraint(
            "org_id", "repo_id", "id", name="uq_test_identities_scope_id"
        ),
        schema="ti",
    )
    op.create_table(
        "test_results",
        _id("org_id"),
        _id("repo_id"),
        _id("run_id"),
        _id("test_id"),
        sa.Column("attempt", sa.SmallInteger(), nullable=False),
        sa.Column("outcome", sa.Text(), nullable=False),
        sa.CheckConstraint("attempt >= 1", name=op.f("ck_test_results_attempt")),
        sa.CheckConstraint(
            "outcome IN ('passed', 'failed', 'errored', 'skipped')",
            name=op.f("ck_test_results_outcome"),
        ),
        sa.ForeignKeyConstraint(
            [*SCOPE, "run_id"],
            ["ti.runs.org_id", "ti.runs.repo_id", "ti.runs.id"],
            name=op.f("fk_test_results_org_id_runs"),
        ),
        sa.ForeignKeyConstraint(
            [*SCOPE, "test_id"],
            [
                "ti.test_identities.org_id",
                "ti.test_identities.repo_id",
                "ti.test_identities.id",
            ],
            name=op.f("fk_test_results_org_id_test_identities"),
        ),
        sa.PrimaryKeyConstraint(
            "run_id", "test_id", "attempt", name=op.f("pk_test_results")
        ),
        schema="ti",
    )
    op.create_index(
        op.f("ix_ti_test_results_org_id"),
        "test_results",
        [*SCOPE, "test_id"],
        schema="ti",
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
        schema="ti",
    )
    op.create_index(
        "ix_ti_outbox_unpublished",
        "outbox",
        ["seq"],
        schema="ti",
        postgresql_where=sa.text("published_at IS NULL"),
    )


def downgrade() -> None:
    op.execute("DROP SCHEMA ti CASCADE")

"""`ops.verification_runs`: execution evidence for one-off jobs.

Revision ID: ops_0001
Revises: ti_0003
Create Date: 2026-10-25

Render one-off jobs report only a coarse `status` (`succeeded`/`failed`) and
no exit code, and calibration on 2026-10-25 showed that status is not a
function of the command's exit code (exit 1, 3 and 26 were all marked
`succeeded`; an exit-0 shell wrapper was marked `failed`). This table is the
durable, append-only record the wrapper (`verify-run`) writes from inside the
job: the real exit code, monotonic-inside-the-container timestamps, the exact
target command and hashes of its output. Rows are never updated or deleted by
code (retention is an owner decision); they are tiny and sanitized.
"""

import sqlalchemy as sa
from alembic import op

revision = "ops_0001"
down_revision = "ti_0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE SCHEMA ops")
    op.create_table(
        "verification_runs",
        sa.Column("id", sa.Uuid(as_uuid=False), nullable=False),
        sa.Column("job_id", sa.Text(), nullable=True),
        sa.Column("service_id", sa.Text(), nullable=True),
        sa.Column("target_command", sa.Text(), nullable=False),
        sa.Column("target_sha256", sa.Text(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("duration_ms", sa.BigInteger(), nullable=False),
        sa.Column("exit_code", sa.Integer(), nullable=True),
        sa.Column("signal", sa.Integer(), nullable=True),
        sa.Column("timed_out", sa.Boolean(), nullable=False),
        sa.Column("spawn_error", sa.Text(), nullable=True),
        sa.Column("stdout_sha256", sa.Text(), nullable=False),
        sa.Column("stderr_sha256", sa.Text(), nullable=False),
        sa.Column("stdout_bytes", sa.BigInteger(), nullable=False),
        sa.Column("stderr_bytes", sa.BigInteger(), nullable=False),
        sa.Column("stdout_head", sa.Text(), nullable=True),
        sa.Column("stdout_tail", sa.Text(), nullable=True),
        sa.Column("stderr_head", sa.Text(), nullable=True),
        sa.Column("stderr_tail", sa.Text(), nullable=True),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("recorded_by", sa.Text(), nullable=False),
        sa.CheckConstraint("exit_code IS NULL OR exit_code >= 0", name="exit_code"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_verification_runs")),
        schema="ops",
    )
    op.create_index(
        op.f("ix_ops_verification_runs_job_id"),
        "verification_runs",
        ["job_id"],
        unique=False,
        schema="ops",
    )


def downgrade() -> None:
    op.drop_index(
        op.f("ix_ops_verification_runs_job_id"),
        table_name="verification_runs",
        schema="ops",
    )
    op.drop_table("verification_runs", schema="ops")
    op.execute("DROP SCHEMA ops")

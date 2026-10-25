"""TI backfill provenance: operator-imported CI history keeps its original time.

An upload is either a CI upload (it has a token) or a GitHub Actions backfill
(no token; the original run URL instead). A backfilled run carries the time the
code host reported for the original CI run; `received_at` stays receipt time.

Revision ID: ti_0002
Revises: aud_0003
Create Date: 2026-10-25
"""

import sqlalchemy as sa
from alembic import op

revision = "ti_0002"
down_revision = "aud_0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "uploads",
        sa.Column(
            "provenance",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'ci_upload'"),
        ),
        schema="ti",
    )
    op.add_column(
        "uploads", sa.Column("origin_run_url", sa.Text(), nullable=True), schema="ti"
    )
    op.alter_column("uploads", "token_id", nullable=True, schema="ti")
    op.create_check_constraint(
        op.f("ck_uploads_provenance"),
        "uploads",
        "provenance IN ('ci_upload', 'github_actions_backfill')",
        schema="ti",
    )
    op.create_check_constraint(
        op.f("ck_uploads_token_provenance"),
        "uploads",
        "(provenance = 'ci_upload') = (token_id IS NOT NULL)",
        schema="ti",
    )
    op.create_check_constraint(
        op.f("ck_uploads_origin_run_url"),
        "uploads",
        "(provenance = 'github_actions_backfill') = (origin_run_url IS NOT NULL)",
        schema="ti",
    )
    op.add_column(
        "runs",
        sa.Column("ci_completed_at", sa.DateTime(timezone=True), nullable=True),
        schema="ti",
    )
    op.create_check_constraint(
        op.f("ck_runs_ci_completed_at"),
        "runs",
        "ci_completed_at IS NULL OR ci_completed_at <= received_at",
        schema="ti",
    )


def downgrade() -> None:
    op.drop_constraint("ck_runs_ci_completed_at", "runs", schema="ti", type_="check")
    op.drop_column("runs", "ci_completed_at", schema="ti")
    for name in ("origin_run_url", "token_provenance", "provenance"):
        op.drop_constraint(f"ck_uploads_{name}", "uploads", schema="ti", type_="check")
    op.alter_column("uploads", "token_id", nullable=False, schema="ti")
    op.drop_column("uploads", "origin_run_url", schema="ti")
    op.drop_column("uploads", "provenance", schema="ti")

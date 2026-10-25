"""TI: the `github_actions_backfill` CI source belongs to the operator backfill only.

A CI upload that claimed it could take the backfill's idempotency key for a
run before the operator imported it. Ingestion refuses that source; this
constraint holds the same rule in the table.

Revision ID: ti_0003
Revises: iam_0003
Create Date: 2026-10-25
"""

from alembic import op

revision = "ti_0003"
down_revision = "iam_0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_check_constraint(
        op.f("ck_uploads_backfill_ci_source"),
        "uploads",
        "(provenance = 'github_actions_backfill') = "
        "(ci_source IS NOT DISTINCT FROM 'github_actions_backfill')",
        schema="ti",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_uploads_backfill_ci_source", "uploads", schema="ti", type_="check"
    )

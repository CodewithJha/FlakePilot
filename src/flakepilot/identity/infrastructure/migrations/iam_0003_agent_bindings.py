"""Auth0 agent bindings (goal 9): one Auth0 subject's delegation from an org admin.

Revision ID: iam_0003
Revises: ti_0002
Create Date: 2026-10-25
"""

from datetime import datetime

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "iam_0003"
down_revision = "ti_0002"
branch_labels = None
depends_on = None


def _id(name: str, nullable: bool = False) -> sa.Column[str]:
    return sa.Column(name, sa.Uuid(as_uuid=False), nullable=nullable)


def _time(name: str, nullable: bool = False) -> sa.Column[datetime]:
    return sa.Column(name, sa.DateTime(timezone=True), nullable=nullable)


def upgrade() -> None:
    op.create_table(
        "agent_bindings",
        _id("id"),
        _id("org_id"),
        _id("delegator_id"),
        _id("repo_id"),
        sa.Column("subject_hash", sa.LargeBinary(), nullable=False),
        sa.Column("agent_id", sa.Text(), nullable=False),
        sa.Column("grants", postgresql.ARRAY(sa.Text()), nullable=False),
        _time("created_at"),
        _time("expires_at"),
        _time("revoked_at", nullable=True),
        sa.CheckConstraint(
            "octet_length(subject_hash) = 32",
            name=op.f("ck_agent_bindings_subject_hash_length"),
        ),
        sa.CheckConstraint(
            "agent_id ~ '^[a-z][a-z0-9_]{0,39}$'",
            name=op.f("ck_agent_bindings_agent_id"),
        ),
        sa.CheckConstraint(
            "cardinality(grants) BETWEEN 1 AND 2 "
            "AND grants <@ ARRAY['repo.view', 'repo.investigate']::text[]",
            name=op.f("ck_agent_bindings_grants"),
        ),
        sa.CheckConstraint(
            "expires_at > created_at AND expires_at <= created_at + interval '8 hours'",
            name=op.f("ck_agent_bindings_ttl"),
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "delegator_id"],
            ["iam.memberships.org_id", "iam.memberships.user_id"],
            name=op.f("fk_agent_bindings_org_id_memberships"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_agent_bindings")),
        schema="iam",
    )
    op.create_index(
        "uq_agent_bindings_active_subject",
        "agent_bindings",
        ["org_id", "subject_hash"],
        unique=True,
        schema="iam",
        postgresql_where=sa.text("revoked_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_index(
        "uq_agent_bindings_active_subject", table_name="agent_bindings", schema="iam"
    )
    op.drop_table("agent_bindings", schema="iam")

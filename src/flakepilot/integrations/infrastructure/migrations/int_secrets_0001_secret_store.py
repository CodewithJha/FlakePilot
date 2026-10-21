"""Encrypted secret store table (ADR-009; ADR-005 decision 6).

Revision ID: int_secrets_0001
Revises: iam_0001
Create Date: 2026-10-21
"""

import sqlalchemy as sa
from alembic import op

revision = "int_secrets_0001"
down_revision = "iam_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE SCHEMA int_secrets")
    op.create_table(
        "secrets",
        sa.Column("id", sa.Uuid(as_uuid=False), nullable=False),
        sa.Column("org_id", sa.Uuid(as_uuid=False), nullable=True),
        sa.Column("repo_id", sa.Uuid(as_uuid=False), nullable=True),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("key_version", sa.SmallInteger(), nullable=False),
        sa.Column("nonce", sa.LargeBinary(), nullable=False),
        sa.Column("ciphertext", sa.LargeBinary(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "octet_length(nonce) = 12", name=op.f("ck_secrets_nonce_length")
        ),
        sa.CheckConstraint(
            "repo_id IS NULL OR org_id IS NOT NULL",
            name=op.f("ck_secrets_repo_needs_org"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_secrets")),
        sa.UniqueConstraint(
            "org_id",
            "repo_id",
            "name",
            name=op.f("uq_secrets_org_id"),
            postgresql_nulls_not_distinct=True,
        ),
        schema="int_secrets",
    )


def downgrade() -> None:
    op.execute("DROP SCHEMA int_secrets CASCADE")

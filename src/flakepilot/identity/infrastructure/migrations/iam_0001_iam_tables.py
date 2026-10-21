"""IAM tables (docs/architecture/IAM-DATA-MODEL.md).

Revision ID: iam_0001
Revises:
Create Date: 2026-10-21
"""

from datetime import datetime

import sqlalchemy as sa
from alembic import op

revision = "iam_0001"
down_revision = None
branch_labels = None
depends_on = None


def _id(name: str, nullable: bool = False) -> sa.Column[str]:
    return sa.Column(name, sa.Uuid(as_uuid=False), nullable=nullable)


def _time(name: str, nullable: bool = False) -> sa.Column[datetime]:
    return sa.Column(name, sa.DateTime(timezone=True), nullable=nullable)


def upgrade() -> None:
    op.execute("CREATE SCHEMA iam")
    op.create_table(
        "orgs",
        _id("id"),
        sa.Column("name", sa.Text(), nullable=False),
        _time("created_at"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_orgs")),
        schema="iam",
    )
    op.create_table(
        "users",
        _id("id"),
        sa.Column("idp_issuer", sa.Text(), nullable=False),
        sa.Column("idp_subject", sa.Text(), nullable=False),
        sa.Column("github_login", sa.Text(), nullable=False),
        _time("created_at"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_users")),
        sa.UniqueConstraint(
            "idp_issuer", "idp_subject", name=op.f("uq_users_idp_issuer")
        ),
        schema="iam",
    )
    op.create_table(
        "ci_upload_tokens",
        _id("id"),
        _id("org_id"),
        _id("repo_id"),
        sa.Column("token_hash", sa.LargeBinary(), nullable=False),
        sa.Column("display_prefix", sa.Text(), nullable=False),
        _id("created_by"),
        _time("created_at"),
        _time("revoked_at", nullable=True),
        sa.CheckConstraint(
            "char_length(display_prefix) = 8",
            name=op.f("ck_ci_upload_tokens_display_prefix_length"),
        ),
        sa.CheckConstraint(
            "octet_length(token_hash) = 32",
            name=op.f("ck_ci_upload_tokens_token_hash_length"),
        ),
        sa.ForeignKeyConstraint(
            ["org_id"], ["iam.orgs.id"], name=op.f("fk_ci_upload_tokens_org_id_orgs")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_ci_upload_tokens")),
        sa.UniqueConstraint("token_hash", name=op.f("uq_ci_upload_tokens_token_hash")),
        schema="iam",
    )
    op.create_index(
        op.f("ix_iam_ci_upload_tokens_org_id"),
        "ci_upload_tokens",
        ["org_id", "repo_id"],
        schema="iam",
    )
    op.create_table(
        "memberships",
        _id("org_id"),
        _id("user_id"),
        sa.Column(
            "org_admin", sa.Boolean(), server_default=sa.text("false"), nullable=False
        ),
        _time("created_at"),
        sa.ForeignKeyConstraint(
            ["org_id"], ["iam.orgs.id"], name=op.f("fk_memberships_org_id_orgs")
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["iam.users.id"], name=op.f("fk_memberships_user_id_users")
        ),
        sa.PrimaryKeyConstraint("org_id", "user_id", name=op.f("pk_memberships")),
        schema="iam",
    )
    op.create_index(
        op.f("ix_iam_memberships_user_id"), "memberships", ["user_id"], schema="iam"
    )
    op.create_table(
        "sessions",
        sa.Column("id_hash", sa.LargeBinary(), nullable=False),
        _id("user_id"),
        _time("created_at"),
        _time("last_seen_at"),
        _time("expires_at"),
        _time("revoked_at", nullable=True),
        sa.Column("revoke_reason", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "revoke_reason IN ('rotated', 'signed_out', 'revoked_by_admin', "
            "'privilege_change')",
            name=op.f("ck_sessions_revoke_reason"),
        ),
        sa.CheckConstraint(
            "(revoked_at IS NULL) = (revoke_reason IS NULL)",
            name=op.f("ck_sessions_revocation"),
        ),
        sa.CheckConstraint(
            "octet_length(id_hash) = 32", name=op.f("ck_sessions_id_hash_length")
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["iam.users.id"],
            name=op.f("fk_sessions_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id_hash", name=op.f("pk_sessions")),
        schema="iam",
    )
    op.create_index(
        op.f("ix_iam_sessions_expires_at"), "sessions", ["expires_at"], schema="iam"
    )
    op.create_index(
        op.f("ix_iam_sessions_user_id"), "sessions", ["user_id"], schema="iam"
    )
    op.create_table(
        "repo_grants",
        _id("org_id"),
        _id("user_id"),
        _id("repo_id"),
        sa.Column("grant", sa.Text(), nullable=False),
        _id("granted_by"),
        _time("created_at"),
        sa.CheckConstraint(
            "\"grant\" IN ('repo.view', 'repo.investigate', 'repo.approve', "
            "'repo.configure')",
            name=op.f("ck_repo_grants_grant"),
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "user_id"],
            ["iam.memberships.org_id", "iam.memberships.user_id"],
            name=op.f("fk_repo_grants_org_id_memberships"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "org_id", "user_id", "repo_id", "grant", name=op.f("pk_repo_grants")
        ),
        schema="iam",
    )
    op.create_index(
        op.f("ix_iam_repo_grants_org_id"),
        "repo_grants",
        ["org_id", "repo_id"],
        schema="iam",
    )


def downgrade() -> None:
    op.execute("DROP SCHEMA iam CASCADE")

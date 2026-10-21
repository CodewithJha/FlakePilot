"""IAM's tables in the `iam` schema (docs/architecture/IAM-DATA-MODEL.md, Accepted)."""

from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    LargeBinary,
    MetaData,
    PrimaryKeyConstraint,
    Table,
    Text,
    UniqueConstraint,
    Uuid,
    false,
)

# Same convention in every context's metadata (tests/architecture checks they match).
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

metadata = MetaData(schema="iam", naming_convention=NAMING_CONVENTION)


def _id(
    name: str, *args: ForeignKey, primary_key: bool = False, nullable: bool = False
) -> Column[str]:
    return Column(
        name, Uuid(as_uuid=False), *args, primary_key=primary_key, nullable=nullable
    )


def _time(name: str, nullable: bool = False) -> Column[datetime]:
    return Column(name, DateTime(timezone=True), nullable=nullable)


orgs = Table(
    "orgs",
    metadata,
    _id("id", primary_key=True),
    Column("name", Text, nullable=False),
    _time("created_at"),
)

users = Table(
    "users",
    metadata,
    _id("id", primary_key=True),
    Column("idp_issuer", Text, nullable=False),
    Column("idp_subject", Text, nullable=False),
    Column("github_login", Text, nullable=False),
    _time("created_at"),
    UniqueConstraint("idp_issuer", "idp_subject"),
)

memberships = Table(
    "memberships",
    metadata,
    _id("org_id", ForeignKey(orgs.c.id), nullable=False),
    _id("user_id", ForeignKey(users.c.id), nullable=False),
    Column("org_admin", Boolean, nullable=False, server_default=false()),
    _time("created_at"),
    PrimaryKeyConstraint("org_id", "user_id"),
    Index(None, "user_id"),
)

repo_grants = Table(
    "repo_grants",
    metadata,
    _id("org_id", nullable=False),
    _id("user_id", nullable=False),
    _id("repo_id", nullable=False),
    Column("grant", Text, nullable=False),
    _id("granted_by", nullable=False),
    _time("created_at"),
    PrimaryKeyConstraint("org_id", "user_id", "repo_id", "grant"),
    ForeignKeyConstraint(
        ["org_id", "user_id"],
        [memberships.c.org_id, memberships.c.user_id],
        ondelete="CASCADE",
    ),
    CheckConstraint(
        "\"grant\" IN ('repo.view', 'repo.investigate', 'repo.approve', "
        "'repo.configure')",
        name="grant",
    ),
    Index(None, "org_id", "repo_id"),
)

sessions = Table(
    "sessions",
    metadata,
    Column("id_hash", LargeBinary, primary_key=True),
    _id("user_id", ForeignKey(users.c.id, ondelete="CASCADE"), nullable=False),
    _time("created_at"),
    _time("last_seen_at"),
    _time("expires_at"),
    _time("revoked_at", nullable=True),
    Column("revoke_reason", Text),
    CheckConstraint("octet_length(id_hash) = 32", name="id_hash_length"),
    CheckConstraint(
        "revoke_reason IN ('rotated', 'signed_out', 'revoked_by_admin', "
        "'privilege_change')",
        name="revoke_reason",
    ),
    CheckConstraint(
        "(revoked_at IS NULL) = (revoke_reason IS NULL)", name="revocation"
    ),
    Index(None, "user_id"),
    Index(None, "expires_at"),
)

ci_upload_tokens = Table(
    "ci_upload_tokens",
    metadata,
    _id("id", primary_key=True),
    _id("org_id", ForeignKey(orgs.c.id), nullable=False),
    _id("repo_id", nullable=False),
    Column("token_hash", LargeBinary, nullable=False, unique=True),
    Column("display_prefix", Text, nullable=False),
    _id("created_by", nullable=False),
    _time("created_at"),
    _time("revoked_at", nullable=True),
    CheckConstraint("octet_length(token_hash) = 32", name="token_hash_length"),
    CheckConstraint("char_length(display_prefix) = 8", name="display_prefix_length"),
    Index(None, "org_id", "repo_id"),
)

"""IAM's tables in the `iam` schema (docs/architecture/IAM-DATA-MODEL.md, Accepted)."""

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Identity,
    Index,
    LargeBinary,
    MetaData,
    PrimaryKeyConstraint,
    SmallInteger,
    Table,
    Text,
    UniqueConstraint,
    Uuid,
    false,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB

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
# Org names are unique case-insensitively (owner decision B, 2026-10-21).
Index("uq_orgs_lower_name", func.lower(orgs.c.name), unique=True)

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

# ADR-012: IAM's event envelopes, appended in the same transaction as the change.
# The relay sets `published_at` once AUD has stored the envelope. `dead_at` parks an
# invalid envelope; the row is kept.
outbox = Table(
    "outbox",
    metadata,
    _id("event_id", primary_key=True),
    # Insertion order, also within one transaction (occurred_at is per transaction).
    Column("seq", BigInteger, Identity(always=True), nullable=False),
    Column("name", Text, nullable=False),
    Column("schema_version", SmallInteger, nullable=False),
    _time("occurred_at"),
    _id("org_id", nullable=True),
    _id("repo_id", nullable=True),
    Column("actor_type", Text, nullable=False),
    _id("actor_id", nullable=True),
    Column("channel", Text, nullable=False),
    Column("correlation_id", Text, nullable=False),
    Column("subject_type", Text, nullable=False),
    _id("subject_id", nullable=False),
    Column("before", Text),
    Column("after", Text),
    Column("reason", Text),
    Column("payload", JSONB, nullable=False),
    _time("published_at", nullable=True),
    Column("attempts", SmallInteger, nullable=False, server_default=text("0")),
    Column("last_error", Text),
    _time("dead_at", nullable=True),
    CheckConstraint(
        "actor_type IN ('user', 'ci_token', 'operator_cli', 'system')",
        name="actor_type",
    ),
    CheckConstraint(
        "(actor_type IN ('user', 'ci_token')) = (actor_id IS NOT NULL)",
        name="actor_id",
    ),
    CheckConstraint("channel IN ('http', 'cli')", name="channel"),
    CheckConstraint("attempts BETWEEN 0 AND 20", name="attempts"),
    CheckConstraint(
        "last_error IS NULL OR last_error ~ '^[a-z][a-z0-9_]{0,39}$'",
        name="last_error",
    ),
    CheckConstraint("published_at IS NULL OR dead_at IS NULL", name="terminal"),
    Index(
        "ix_iam_outbox_unpublished",
        "seq",
        postgresql_where=text("published_at IS NULL AND dead_at IS NULL"),
    ),
)

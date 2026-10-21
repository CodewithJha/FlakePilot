"""Diagnosis tables in the `diag` schema. No cross-context foreign keys."""

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    Identity,
    Index,
    Integer,
    LargeBinary,
    MetaData,
    SmallInteger,
    Table,
    Text,
    Uuid,
    false,
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

metadata = MetaData(schema="diag", naming_convention=NAMING_CONVENTION)

_LEVELS = "('disabled', 'evidence_only', 'evidence_and_source')"
_OUTCOMES = "('produced', 'no_hypothesis', 'rejected', 'unavailable')"
_CODE = "'^[a-z][a-z0-9_]{0,39}$'"


def _id(name: str, *, primary_key: bool = False, nullable: bool = False) -> Column[str]:
    return Column(name, Uuid(as_uuid=False), primary_key=primary_key, nullable=nullable)


def _time(name: str, nullable: bool = False) -> Column[datetime]:
    return Column(name, DateTime(timezone=True), nullable=nullable)


ai_policies = Table(
    "ai_policies",
    metadata,
    _id("org_id", primary_key=True),
    Column("level", Text, nullable=False),
    Column("retain_raw", Boolean, nullable=False, server_default=false()),
    _time("updated_at"),
    CheckConstraint(f"level IN {_LEVELS}", name="level"),
)

ai_requests = Table(
    "ai_requests",
    metadata,
    _id("id", primary_key=True),
    _id("org_id"),
    _id("repo_id"),
    _id("actor_id"),
    Column("policy_level", Text, nullable=False),
    Column("retain_raw", Boolean, nullable=False),
    Column("provider", Text, nullable=False),
    Column("model", Text),
    Column("payload_sha256", LargeBinary, nullable=False),
    Column("payload_size", Integer, nullable=False),
    Column("outcome", Text, nullable=False),
    Column("validation_reason", Text),
    Column("linked_count", Integer, nullable=False),
    Column("unverified_count", Integer, nullable=False),
    Column("contradicted_count", Integer, nullable=False),
    Column("raw_output", Text),
    _time("created_at"),
    CheckConstraint(f"policy_level IN {_LEVELS}", name="policy_level"),
    CheckConstraint(f"outcome IN {_OUTCOMES}", name="outcome"),
    CheckConstraint("octet_length(payload_sha256) = 32", name="payload_sha256_length"),
    CheckConstraint("payload_size >= 0", name="payload_size"),
    CheckConstraint("linked_count >= 0", name="linked_count"),
    CheckConstraint("unverified_count >= 0", name="unverified_count"),
    CheckConstraint("contradicted_count >= 0", name="contradicted_count"),
    CheckConstraint("raw_output IS NULL OR retain_raw", name="raw_output_opt_in"),
    CheckConstraint(
        "outcome <> 'rejected' OR validation_reason IS NOT NULL",
        name="rejected_reason",
    ),
    CheckConstraint(
        "raw_output IS NULL OR octet_length(raw_output) <= 65536",
        name="raw_output_length",
    ),
    CheckConstraint(f"provider ~ {_CODE}", name="provider"),
    CheckConstraint(f"model IS NULL OR model ~ {_CODE}", name="model"),
    CheckConstraint(
        f"validation_reason IS NULL OR validation_reason ~ {_CODE}",
        name="validation_reason",
    ),
)

# Appended in the same transaction as the change. `published_at` is set by the
# relay (ADR-006 job runtime, not built yet).
outbox = Table(
    "outbox",
    metadata,
    _id("event_id", primary_key=True),
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
    _id("subject_id"),
    Column("before", Text),
    Column("after", Text),
    Column("reason", Text),
    Column("payload", JSONB, nullable=False),
    _time("published_at", nullable=True),
    CheckConstraint(
        "actor_type IN ('user', 'ci_token', 'operator_cli', 'system')",
        name="actor_type",
    ),
    CheckConstraint(
        "(actor_type IN ('user', 'ci_token')) = (actor_id IS NOT NULL)",
        name="actor_id",
    ),
    CheckConstraint("channel IN ('http', 'cli')", name="channel"),
    Index(
        "ix_diag_outbox_unpublished",
        "seq",
        postgresql_where=text("published_at IS NULL"),
    ),
)

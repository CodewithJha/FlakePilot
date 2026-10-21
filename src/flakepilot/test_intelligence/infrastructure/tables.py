"""TI's tables in the `ti` schema (ADR-013). Every row carries org_id and repo_id; rows refer
to each other through (org_id, repo_id, id) foreign keys, so a reference can never cross
tenants. No raw report bytes: the report is an artifact, referenced by its key."""

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKeyConstraint,
    Identity,
    Index,
    Integer,
    LargeBinary,
    MetaData,
    PrimaryKeyConstraint,
    SmallInteger,
    Table,
    Text,
    UniqueConstraint,
    Uuid,
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

metadata = MetaData(schema="ti", naming_convention=NAMING_CONVENTION)

UPLOAD_STATES = ("received", "processing", "processed", "rejected", "processing_failed")
KEY_COLUMNS = ("org_id", "repo_id", "ci_source", "ci_run_id", "ci_attempt", "job_name")


def _id(name: str, nullable: bool = False) -> Column[str]:
    return Column(name, Uuid(as_uuid=False), nullable=nullable)


def _time(name: str, nullable: bool = False) -> Column[datetime]:
    return Column(name, DateTime(timezone=True), nullable=nullable)


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


uploads = Table(
    "uploads",
    metadata,
    _id("id"),
    _id("org_id"),
    _id("repo_id"),
    _id("token_id"),
    Column("state", Text, nullable=False),
    Column("reject_reason", Text),
    Column("ci_source", Text),
    Column("ci_run_id", Text),
    Column("ci_attempt", Integer),
    Column("job_name", Text),
    Column("commit_sha", Text),
    Column("ref", Text),
    Column("environment", Text),
    _time("started_at", nullable=True),
    _time("finished_at", nullable=True),
    Column("content_sha256", LargeBinary),
    Column("size_bytes", BigInteger),
    Column("content_type", Text),
    Column("artifact_key", Text),
    _time("received_at"),
    PrimaryKeyConstraint("id"),
    UniqueConstraint("org_id", "repo_id", "id", name="uq_uploads_scope_id"),
    CheckConstraint(_in("state", UPLOAD_STATES), name="state"),
    CheckConstraint(
        "(state = 'rejected') = (reject_reason IS NOT NULL)", name="reject_reason"
    ),
    CheckConstraint(
        "state <> 'processed' OR (ci_source IS NOT NULL AND ci_run_id IS NOT NULL "
        "AND ci_attempt IS NOT NULL AND job_name IS NOT NULL AND commit_sha IS NOT NULL "
        "AND ref IS NOT NULL AND environment IS NOT NULL AND started_at IS NOT NULL "
        "AND finished_at IS NOT NULL AND content_sha256 IS NOT NULL "
        "AND size_bytes IS NOT NULL AND content_type IS NOT NULL "
        "AND artifact_key IS NOT NULL)",
        name="processed_complete",
    ),
    CheckConstraint(
        "content_sha256 IS NULL OR octet_length(content_sha256) = 32",
        name="content_sha256_length",
    ),
)
# The idempotency key: unique among uploads that were not rejected.
Index(
    "uq_uploads_idempotency_key",
    *(uploads.c[name] for name in KEY_COLUMNS),
    unique=True,
    postgresql_where=text("state <> 'rejected'"),
)

runs = Table(
    "runs",
    metadata,
    _id("id"),
    _id("org_id"),
    _id("repo_id"),
    _id("upload_id"),
    Column("source", Text, nullable=False),
    Column("commit_sha", Text, nullable=False),
    Column("environment", Text, nullable=False),
    Column("revision_status", Text, nullable=False),
    _time("received_at"),
    Column("tests", Integer, nullable=False),
    Column("attempts", Integer, nullable=False),
    Column("passed", Integer, nullable=False),
    Column("failed", Integer, nullable=False),
    Column("errored", Integer, nullable=False),
    Column("skipped", Integer, nullable=False),
    PrimaryKeyConstraint("id"),
    UniqueConstraint("upload_id"),
    UniqueConstraint("org_id", "repo_id", "id", name="uq_runs_scope_id"),
    ForeignKeyConstraint(
        ["org_id", "repo_id", "upload_id"],
        [uploads.c.org_id, uploads.c.repo_id, uploads.c.id],
    ),
    CheckConstraint(_in("source", ("ci_upload", "sandbox")), name="source"),
    CheckConstraint(
        _in("revision_status", ("unchecked", "verified", "unverified_revision")),
        name="revision_status",
    ),
)

test_identities = Table(
    "test_identities",
    metadata,
    _id("id"),
    _id("org_id"),
    _id("repo_id"),
    Column("identity_key", LargeBinary, nullable=False),
    Column("classname", Text, nullable=False),
    Column("name", Text, nullable=False),
    _time("created_at"),
    PrimaryKeyConstraint("id"),
    UniqueConstraint(
        "org_id", "repo_id", "identity_key", name="uq_test_identities_identity_key"
    ),
    UniqueConstraint("org_id", "repo_id", "id", name="uq_test_identities_scope_id"),
    CheckConstraint("octet_length(identity_key) = 32", name="identity_key_length"),
)

test_results = Table(
    "test_results",
    metadata,
    _id("org_id"),
    _id("repo_id"),
    _id("run_id"),
    _id("test_id"),
    Column("attempt", SmallInteger, nullable=False),
    Column("outcome", Text, nullable=False),
    PrimaryKeyConstraint("run_id", "test_id", "attempt"),
    ForeignKeyConstraint(
        ["org_id", "repo_id", "run_id"], [runs.c.org_id, runs.c.repo_id, runs.c.id]
    ),
    ForeignKeyConstraint(
        ["org_id", "repo_id", "test_id"],
        [test_identities.c.org_id, test_identities.c.repo_id, test_identities.c.id],
    ),
    CheckConstraint("attempt >= 1", name="attempt"),
    CheckConstraint(
        _in("outcome", ("passed", "failed", "errored", "skipped")), name="outcome"
    ),
    Index(None, "org_id", "repo_id", "test_id"),
)

# ADR-012: TI's event envelopes, appended in the same transaction as the change.
outbox = Table(
    "outbox",
    metadata,
    _id("event_id"),
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
    Column("attempts", SmallInteger, nullable=False, server_default=text("0")),
    Column("last_error", Text),
    _time("dead_at", nullable=True),
    PrimaryKeyConstraint("event_id"),
    CheckConstraint(
        _in("actor_type", ("user", "ci_token", "operator_cli", "system")),
        name="actor_type",
    ),
    CheckConstraint(
        "(actor_type IN ('user', 'ci_token')) = (actor_id IS NOT NULL)",
        name="actor_id",
    ),
    CheckConstraint(_in("channel", ("http", "cli")), name="channel"),
    CheckConstraint("attempts BETWEEN 0 AND 20", name="attempts"),
    CheckConstraint(
        "last_error IS NULL OR last_error ~ '^[a-z][a-z0-9_]{0,39}$'",
        name="last_error",
    ),
    CheckConstraint("published_at IS NULL OR dead_at IS NULL", name="terminal"),
    Index(
        "ix_ti_outbox_unpublished",
        "seq",
        postgresql_where=text("published_at IS NULL AND dead_at IS NULL"),
    ),
)

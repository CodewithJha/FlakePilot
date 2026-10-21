"""Durable browser reproductions in the `exec` schema. No cross-context foreign keys.

One active row per org, repo, selector and SHA. Terminal rows stay. The outbox
is appended in the same transaction as the execution change.
"""

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
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

NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

metadata = MetaData(schema="exec", naming_convention=NAMING_CONVENTION)

_SHA = "'^[0-9a-f]{40}$'"
_CODE = "'^[a-z][a-z0-9_]{0,39}$'"
_TOKEN = "'^[A-Za-z0-9._:-]{1,100}$'"
_SELECTOR = "selector ~ '^[A-Za-z0-9_.:-]{1,120}$' AND strpos(selector, '..') = 0"
_OUTCOME = (
    "(state IN ('scheduled', 'running') AND outcome IS NULL AND reason IS NULL) "
    "OR (state = 'completed' AND ("
    "(outcome = 'success' AND reason = 'passed') "
    "OR (outcome = 'test_failure' AND reason = 'failed') "
    "OR (outcome = 'timeout' AND reason = 'timeout') "
    "OR (outcome = 'invalid_result' AND reason = 'invalid_result') "
    "OR (outcome = 'infra_failure' AND reason ~ " + _CODE + ") "
    "OR (outcome = 'inconclusive' AND reason IN "
    "('execution_closed', 'source_unverified'))"
    ")) OR (state = 'cancelled' AND outcome = 'cancelled' AND reason = 'cancelled')"
)

executions = Table(
    "executions",
    metadata,
    Column("id", Uuid(as_uuid=False), primary_key=True),
    Column("org_id", Uuid(as_uuid=False), nullable=False),
    Column("repo_id", Uuid(as_uuid=False), nullable=False),
    Column("selector", Text, nullable=False),
    Column("sha", Text, nullable=False),
    Column("state", Text, nullable=False),
    Column("outcome", Text),
    Column("reason", Text),
    Column("generation", SmallInteger, nullable=False),
    Column("lease_owner", Text),
    Column("lease_expires_at", DateTime(timezone=True)),
    Column("job_id", Uuid(as_uuid=False)),
    Column("evidence_partial", Boolean, nullable=False, server_default=false()),
    Column("cancel_requested", Boolean, nullable=False, server_default=false()),
    Column("actor_type", Text, nullable=False),
    Column("actor_id", Uuid(as_uuid=False)),
    Column("channel", Text, nullable=False),
    Column("correlation_id", Text, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
    CheckConstraint(_SELECTOR, name="selector"),
    CheckConstraint(f"sha ~ {_SHA}", name="sha"),
    CheckConstraint(
        "state IN ('scheduled', 'running', 'completed', 'cancelled')",
        name="state",
    ),
    CheckConstraint(_OUTCOME, name="outcome"),
    CheckConstraint("generation BETWEEN 0 AND 20", name="generation"),
    CheckConstraint(
        "(state = 'running') = "
        "(lease_owner IS NOT NULL AND lease_expires_at IS NOT NULL)",
        name="lease",
    ),
    CheckConstraint(
        "lease_owner IS NULL OR lease_owner ~ '^[a-z0-9][a-z0-9-]{0,63}$'",
        name="lease_owner",
    ),
    CheckConstraint(
        "actor_type IN ('user', 'ci_token', 'operator_cli', 'system')",
        name="actor_type",
    ),
    CheckConstraint(
        "(actor_type IN ('user', 'ci_token')) = (actor_id IS NOT NULL)",
        name="actor_id",
    ),
    CheckConstraint("channel IN ('http', 'cli')", name="channel"),
    CheckConstraint(f"correlation_id ~ {_TOKEN}", name="correlation_id"),
    Index(
        "uq_exec_executions_active",
        "org_id",
        "repo_id",
        "selector",
        "sha",
        unique=True,
        postgresql_where=text("state = 'scheduled' OR state = 'running'"),
    ),
    Index(
        "ix_exec_executions_expired",
        "lease_expires_at",
        postgresql_where=text("state = 'running'"),
    ),
)

execution_artifacts = Table(
    "execution_artifacts",
    metadata,
    Column("id", Uuid(as_uuid=False), primary_key=True),
    Column(
        "execution_id", Uuid(as_uuid=False), ForeignKey("executions.id"), nullable=False
    ),
    Column("kind", Text, nullable=False),
    Column("sha256", LargeBinary, nullable=False),
    Column("size", Integer, nullable=False),
    Column("artifact_key", Text, nullable=False),
    CheckConstraint("kind IN ('result', 'screenshot')", name="kind"),
    CheckConstraint("octet_length(sha256) = 32", name="sha256"),
    CheckConstraint("size BETWEEN 1 AND 1048576", name="size"),
    CheckConstraint(
        "artifact_key ~ '^exec/[0-9a-f-]{36}/[0-9a-f-]{36}/sha256/[0-9a-f]{64}$'",
        name="artifact_key",
    ),
    Index("ix_exec_execution_artifacts_execution", "execution_id"),
)

outbox = Table(
    "outbox",
    metadata,
    Column("event_id", Uuid(as_uuid=False), primary_key=True),
    Column("seq", BigInteger, Identity(always=True), nullable=False),
    Column("name", Text, nullable=False),
    Column("schema_version", SmallInteger, nullable=False),
    Column("occurred_at", DateTime(timezone=True), nullable=False),
    Column("org_id", Uuid(as_uuid=False)),
    Column("repo_id", Uuid(as_uuid=False)),
    Column("actor_type", Text, nullable=False),
    Column("actor_id", Uuid(as_uuid=False)),
    Column("channel", Text, nullable=False),
    Column("correlation_id", Text, nullable=False),
    Column("subject_type", Text, nullable=False),
    Column("subject_id", Uuid(as_uuid=False), nullable=False),
    Column("before", Text),
    Column("after", Text),
    Column("reason", Text),
    Column("payload", JSONB, nullable=False),
    Column("published_at", DateTime(timezone=True)),
    Column("attempts", SmallInteger, nullable=False, server_default=text("0")),
    Column("last_error", Text),
    Column("dead_at", DateTime(timezone=True)),
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
        "ix_exec_outbox_unpublished",
        "seq",
        postgresql_where=text("published_at IS NULL AND dead_at IS NULL"),
    ),
)

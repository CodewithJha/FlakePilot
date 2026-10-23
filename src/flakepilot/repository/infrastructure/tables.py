"""Registered repositories in the `repo` schema. No tokens and no foreign keys.

`outbox` carries this context's own audit envelopes (ADR-012), born with the delivery
columns the worker relay needs, like the `exec` outbox.
"""

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Column,
    DateTime,
    Identity,
    Index,
    MetaData,
    SmallInteger,
    Table,
    Text,
    UniqueConstraint,
    Uuid,
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

metadata = MetaData(schema="repo", naming_convention=NAMING_CONVENTION)

repositories = Table(
    "repositories",
    metadata,
    Column("id", Uuid(as_uuid=False), primary_key=True),
    Column("org_id", Uuid(as_uuid=False), nullable=False),
    Column("host", Text, nullable=False),
    Column("owner", Text, nullable=False),
    Column("name", Text, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    CheckConstraint("host = 'github'", name="host"),
    CheckConstraint(
        "owner ~ '^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})$'",
        name="owner",
    ),
    CheckConstraint(
        "name ~ '^[A-Za-z0-9._-]{1,100}$' AND strpos(name, '..') = 0 "
        "AND left(name, 1) <> '.'",
        name="name",
    ),
    UniqueConstraint("host", "owner", "name", name="uq_repositories_coordinates"),
)

outbox = Table(
    "outbox",
    metadata,
    Column("event_id", Uuid(as_uuid=False), primary_key=True),
    # Insertion order, also within one transaction (occurred_at is per transaction).
    Column("seq", BigInteger, Identity(always=True), nullable=False),
    Column("name", Text, nullable=False),
    Column("schema_version", SmallInteger, nullable=False),
    Column("occurred_at", DateTime(timezone=True), nullable=False),
    Column("org_id", Uuid(as_uuid=False), nullable=True),
    Column("repo_id", Uuid(as_uuid=False), nullable=True),
    Column("actor_type", Text, nullable=False),
    Column("actor_id", Uuid(as_uuid=False), nullable=True),
    Column("channel", Text, nullable=False),
    Column("correlation_id", Text, nullable=False),
    Column("subject_type", Text, nullable=False),
    Column("subject_id", Uuid(as_uuid=False), nullable=False),
    Column("before", Text),
    Column("after", Text),
    Column("reason", Text),
    Column("payload", JSONB, nullable=False),
    Column("published_at", DateTime(timezone=True), nullable=True),
    Column("attempts", SmallInteger, nullable=False, server_default=text("0")),
    Column("last_error", Text),
    Column("dead_at", DateTime(timezone=True), nullable=True),
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
        "ix_repo_outbox_unpublished",
        "seq",
        postgresql_where=text("published_at IS NULL AND dead_at IS NULL"),
    ),
)

"""Append-only audit log in the `aud` schema. No foreign keys to other contexts."""

from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    Column,
    DateTime,
    MetaData,
    SmallInteger,
    Table,
    Text,
    Uuid,
)
from sqlalchemy.dialects.postgresql import JSONB

NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

metadata = MetaData(schema="aud", naming_convention=NAMING_CONVENTION)


def _id(name: str, nullable: bool = False) -> Column[str]:
    return Column(name, Uuid(as_uuid=False), nullable=nullable)


def _time(name: str) -> Column[datetime]:
    return Column(name, DateTime(timezone=True), nullable=False)


events = Table(
    "events",
    metadata,
    Column("event_id", Uuid(as_uuid=False), primary_key=True),
    Column("source", Text, nullable=False),
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
    _time("recorded_at"),
    CheckConstraint(
        "source IN ('iam', 'ti', 'diag', 'exec')",
        name="source",
    ),
    CheckConstraint("schema_version = 1", name="schema_version"),
    CheckConstraint(
        "actor_type IN ('user', 'ci_token', 'operator_cli', 'system')",
        name="actor_type",
    ),
    CheckConstraint(
        "(actor_type IN ('user', 'ci_token')) = (actor_id IS NOT NULL)",
        name="actor_id",
    ),
    CheckConstraint("channel IN ('http', 'cli')", name="channel"),
)

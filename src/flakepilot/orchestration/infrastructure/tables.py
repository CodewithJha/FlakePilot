"""ORCH durable jobs (ADR-006) in the `orch` schema. No cross-context foreign keys.

The due index matches `claim`'s scheduled branch (`run_at`, `id` where `state` is
scheduled). The lease index matches the expired-running branch (`lease_expires_at`).
"""

from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    Index,
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

metadata = MetaData(schema="orch", naming_convention=NAMING_CONVENTION)

_TOKEN = "'^[A-Za-z0-9._:-]{1,100}$'"
_CODE = "'^[a-z][a-z0-9_]{0,39}$'"


def _time(name: str, nullable: bool = False) -> Column[datetime]:
    return Column(name, DateTime(timezone=True), nullable=nullable)


jobs = Table(
    "jobs",
    metadata,
    Column("id", Uuid(as_uuid=False), primary_key=True),
    Column("job_type", Text, nullable=False),
    Column("payload_version", SmallInteger, nullable=False),
    Column("payload", JSONB, nullable=False),
    Column("state", Text, nullable=False),
    _time("run_at"),
    Column("attempts", SmallInteger, nullable=False),
    Column("max_attempts", SmallInteger, nullable=False),
    Column("lease_owner", Text),
    _time("lease_expires_at", nullable=True),
    _time("created_at"),
    _time("updated_at"),
    Column("last_error", Text),
    Column("correlation_id", Text),
    Column("causation_id", Text),
    Column("cancel_requested", Boolean, nullable=False, server_default=false()),
    CheckConstraint(
        "state IN ('scheduled', 'running', 'completed', 'failed', 'cancelled')",
        name="state",
    ),
    CheckConstraint(
        "job_type ~ '^[a-z][a-z0-9_]{0,63}$'",
        name="job_type",
    ),
    CheckConstraint("payload_version = 1", name="payload_version"),
    CheckConstraint(
        "octet_length(payload::text) <= 8192",
        name="payload_size",
    ),
    CheckConstraint(
        "attempts >= 0 AND attempts <= max_attempts",
        name="attempts",
    ),
    CheckConstraint("max_attempts BETWEEN 1 AND 20", name="max_attempts"),
    CheckConstraint(
        "(state = 'running') = (lease_owner IS NOT NULL AND lease_expires_at IS NOT NULL)",
        name="lease",
    ),
    CheckConstraint(
        "lease_owner IS NULL OR lease_owner ~ '^[a-z0-9][a-z0-9-]{0,63}$'",
        name="lease_owner",
    ),
    CheckConstraint(f"last_error IS NULL OR last_error ~ {_CODE}", name="last_error"),
    CheckConstraint(
        f"correlation_id IS NULL OR correlation_id ~ {_TOKEN}",
        name="correlation_id",
    ),
    CheckConstraint(
        f"causation_id IS NULL OR causation_id ~ {_TOKEN}",
        name="causation_id",
    ),
    Index(
        "ix_orch_jobs_due",
        "run_at",
        "id",
        postgresql_where=text("state = 'scheduled'"),
    ),
    Index(
        "ix_orch_jobs_expired",
        "lease_expires_at",
        postgresql_where=text("state = 'running'"),
    ),
)

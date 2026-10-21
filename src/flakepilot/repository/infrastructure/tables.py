"""Registered repositories in the `repo` schema. No tokens and no foreign keys."""

from sqlalchemy import (
    CheckConstraint,
    Column,
    DateTime,
    MetaData,
    Table,
    Text,
    UniqueConstraint,
    Uuid,
)

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

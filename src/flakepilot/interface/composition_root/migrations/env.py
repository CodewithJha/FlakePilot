"""Alembic environment (async). Run explicitly, e.g. `uv run alembic upgrade head`.

Migrations never run at application startup (ADR-005 decision 7: an explicit step).
The database URL comes only from FLAKEPILOT_DATABASE_URL through Settings; it is never
stored in Alembic config or printed. Offline (`--sql`) mode needs no URL.

Each context owns its tables (its own schema and MetaData) and its migrations, in a
version location inside its infrastructure package (pyproject.toml [tool.alembic]).
Only those schemas are compared, so other objects in the database are never touched.
"""

import asyncio
import os
from typing import Any

from alembic import context
from sqlalchemy.engine import Connection

from flakepilot.audit.infrastructure.tables import metadata as aud_metadata
from flakepilot.diagnosis.infrastructure.tables import metadata as diag_metadata
from flakepilot.identity.infrastructure.tables import metadata as iam_metadata
from flakepilot.integrations.infrastructure.secret_store import (
    metadata as secret_store_metadata,
)
from flakepilot.interface.composition_root.database import create_engine
from flakepilot.interface.composition_root.settings import Settings, SettingsError
from flakepilot.orchestration.infrastructure.tables import metadata as orch_metadata
from flakepilot.test_intelligence.infrastructure.tables import metadata as ti_metadata

target_metadata = [
    iam_metadata,
    secret_store_metadata,
    ti_metadata,
    diag_metadata,
    orch_metadata,
    aud_metadata,
]
_SCHEMAS = {metadata.schema for metadata in target_metadata}
_OPTIONS: dict[str, Any] = {
    "target_metadata": target_metadata,
    "include_schemas": True,
    "include_name": lambda name, type_, parents: type_ != "schema" or name in _SCHEMAS,
}


def _database_url() -> str:
    try:
        return Settings.from_environ(os.environ).database_url.reveal()
    except SettingsError as exc:
        raise SystemExit(f"Invalid configuration: {exc}") from None


def _migrate(connection: Connection) -> None:
    context.configure(connection=connection, **_OPTIONS)
    with context.begin_transaction():
        context.run_migrations()


async def _migrate_online() -> None:
    engine = create_engine(_database_url())
    try:
        try:
            connection = await engine.connect()
        except Exception as exc:  # noqa: BLE001 - errors may carry connection details
            raise SystemExit(f"database unavailable: {type(exc).__name__}") from None
        try:
            await connection.run_sync(_migrate)
        finally:
            await connection.close()
    finally:
        await engine.dispose()


if context.is_offline_mode():
    context.configure(dialect_name="postgresql", literal_binds=True, **_OPTIONS)
    with context.begin_transaction():
        context.run_migrations()
else:
    asyncio.run(_migrate_online())

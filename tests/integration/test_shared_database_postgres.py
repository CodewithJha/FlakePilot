"""The shared-database mode for hosts that refuse CREATE DATABASE (Tiger Cloud).

The helpers under test (`support._assert_shared_tables_empty`,
`support._truncate_shared_tables`) are the safety story for running the whole
suite against ONE persistent database: the gate refuses a database whose
migration schemas hold anything (possibly unrelated owner data) or anything
truncation could damage outside the whitelist, and the teardown TRUNCATE only
ever touches the schemas the migrations create. The tests use a throwaway
database from the default fixture as the scratch, so they run identically under
default and shared mode.
"""

import asyncio

import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from support import (
    SHARED_SCHEMAS,
    _assert_shared_tables_empty,
    _run_alembic_upgrade,
    _shared_mode_enabled,
    _truncate_shared_tables,
    admin_execute,
    throwaway_database,
)

from flakepilot.interface.composition_root.database import create_engine

pytestmark = pytest.mark.integration


def ddl(url: str, statement: str) -> None:
    """`admin_execute` is async; every call here runs one statement to completion."""
    asyncio.run(admin_execute(url, statement))


# A probe table inside a whitelisted schema, created by these tests themselves,
# not by a migration — exactly the situation the gate must refuse. Dropped by
# every test that creates one (finally), so no residue can block a later run.
PROBE = "ti.zz_shared_mode_probe"


def test_the_gate_passes_on_an_empty_migrated_database(database_url: str) -> None:
    _run_alembic_upgrade(database_url)
    _assert_shared_tables_empty(database_url)  # nothing whitelisted holds rows


def test_the_gate_refuses_a_non_empty_whitelisted_table(database_url: str) -> None:
    _run_alembic_upgrade(database_url)
    try:
        ddl(database_url, f"CREATE TABLE {PROBE} (id int, data text)")
        ddl(database_url, f"INSERT INTO {PROBE} VALUES (1, 'maybe owner data')")
        with pytest.raises(RuntimeError) as raised:
            _assert_shared_tables_empty(database_url)
        assert "these migration tables are not empty" in str(raised.value)
        assert "zz_shared_mode_probe" in str(raised.value)
    finally:
        ddl(database_url, f"DROP TABLE IF EXISTS {PROBE}")


def test_the_gate_refuses_references_from_outside_the_whitelist(
    database_url: str,
) -> None:
    """TRUNCATE CASCADE is never used, so an external foreign key must refuse."""
    _run_alembic_upgrade(database_url)
    try:
        _truncate_shared_tables(database_url)  # ti.runs must exist for the FK
        ddl(
            database_url,
            "CREATE TABLE external_reference (run_id uuid REFERENCES ti.runs(id))",
        )
        with pytest.raises(RuntimeError) as raised:
            _assert_shared_tables_empty(database_url)
        assert "foreign keys into it" in str(raised.value)
        assert "external_reference -> ti" in str(raised.value)
    finally:
        ddl(database_url, "DROP TABLE IF EXISTS external_reference")


def test_truncate_clears_whitelisted_tables_and_nothing_else(database_url: str) -> None:
    """The teardown restores exactly the empty-database contract, and only inside
    the migration schemas: alembic_version and public leftovers survive."""
    _run_alembic_upgrade(database_url)
    _assert_shared_tables_empty(database_url)
    try:
        ddl(database_url, f"CREATE TABLE {PROBE} (id int)")
        ddl(database_url, f"INSERT INTO {PROBE} VALUES (7)")
        ddl(database_url, "CREATE TABLE outside_probe (id int)")
        ddl(database_url, "INSERT INTO outside_probe VALUES (7)")

        _truncate_shared_tables(database_url)

        counts = _scalar_counts(database_url, (PROBE, "public.outside_probe"))
        assert counts[PROBE] == 0, "the whitelisted probe table must be truncated"
        assert counts["public.outside_probe"] == 1, "public is never truncated"
        assert (
            _scalar_counts(database_url, ("alembic_version",))["alembic_version"] == 1
        ), "the version row survives teardown"
    finally:
        ddl(database_url, f"DROP TABLE IF EXISTS {PROBE}, outside_probe")


def test_shared_mode_hands_out_one_reusable_database(database_url: str) -> None:
    """Default mode: one throwaway per call (fresh names). Shared mode: the same
    database every call, truncated between fixtures."""
    with throwaway_database() as first, throwaway_database() as second:
        first_db = make_url(first).database
        second_db = make_url(second).database
        admin_db = make_url(database_url).database
        if _shared_mode_enabled():
            assert first_db == second_db == admin_db
        else:
            assert first_db != second_db
            assert first_db != admin_db and second_db != admin_db


def test_shared_schemas_cover_exactly_the_migration_schemas() -> None:
    """The whitelist is the exact set of schemas the migrations create; a new
    migration schema arriving without joining this tuple must not go unnoticed."""
    expected = {
        "aud",
        "diag",
        "exec",
        "iam",
        "int_secrets",
        "ops",
        "orch",
        "repo",
        "ti",
    }
    assert set(SHARED_SCHEMAS) == expected
    assert SHARED_SCHEMAS == tuple(sorted(SHARED_SCHEMAS))


def _scalar_counts(url: str, qualified: tuple[str, ...]) -> dict[str, int]:
    """count(*) for each already-qualified, statically validated table name."""

    async def run() -> dict[str, int]:
        engine = create_engine(url, isolation_level="AUTOCOMMIT")
        counted: dict[str, int] = {}
        try:
            async with engine.connect() as connection:
                for entry in qualified:
                    counted[entry] = int(
                        (
                            await connection.execute(
                                text(f"SELECT count(*) FROM {entry}")
                            )
                        ).scalar_one()
                    )
        finally:
            await engine.dispose()
        return counted

    return asyncio.run(run())

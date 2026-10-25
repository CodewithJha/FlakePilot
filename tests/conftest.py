import os
from collections.abc import Iterator

import pytest
from support import throwaway_database


def pytest_make_parametrize_id(val: object) -> str | None:
    # CI uploads its own JUnit XML for the backfill, whose parser refuses names
    # over 1000 characters; huge parameters keep a short, unique-enough id.
    if isinstance(val, str | bytes) and len(val) > 80:
        return f"{val[:40]!r}...len{len(val)}"
    return None


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    integration = [item for item in items if item.get_closest_marker("integration")]
    if not integration or os.environ.get("FLAKEPILOT_TEST_DATABASE_URL"):
        return
    # GitHub Actions sets CI; there the integration tests are a required gate.
    if os.environ.get("CI"):
        raise pytest.UsageError(
            "FLAKEPILOT_TEST_DATABASE_URL must be set when CI is set"
        )
    skip = pytest.mark.skip(reason="FLAKEPILOT_TEST_DATABASE_URL is not set")
    for item in integration:
        item.add_marker(skip)


@pytest.fixture
def database_url() -> Iterator[str]:
    """A throwaway database for one test (integration tests only).

    With FLAKEPILOT_TEST_SHARED_DATABASE=1 this is FLAKEPILOT_TEST_DATABASE_URL
    itself instead (for hosts that refuse CREATE DATABASE, e.g. Tiger Cloud):
    the shared database must be empty of migration-schema data up front and is
    TRUNCATEd back to empty after each test; see tests.support.shared_database.
    """
    with throwaway_database() as url:
        yield url


@pytest.fixture(scope="module")
def migrated_database_url() -> Iterator[str]:
    """A throwaway database at `alembic upgrade head`, shared by one test module.

    Shared mode (FLAKEPILOT_TEST_SHARED_DATABASE=1) as above, plus the migration
    upgrade runs once per test session in that mode.
    """
    with throwaway_database(migrate=True) as url:
        yield url

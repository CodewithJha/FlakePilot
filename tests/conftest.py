import os
from collections.abc import Iterator

import pytest
from support import throwaway_database


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
    """An empty throwaway database for one test (integration tests only)."""
    with throwaway_database() as url:
        yield url


@pytest.fixture(scope="module")
def migrated_database_url() -> Iterator[str]:
    """A throwaway database at `alembic upgrade head`, shared by one test module."""
    with throwaway_database(migrate=True) as url:
        yield url

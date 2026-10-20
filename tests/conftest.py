import os

import pytest


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

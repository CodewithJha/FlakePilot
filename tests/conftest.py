import os

import pytest


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    if os.environ.get("FLAKEPILOT_TEST_DATABASE_URL"):
        return
    skip = pytest.mark.skip(reason="FLAKEPILOT_TEST_DATABASE_URL is not set")
    for item in items:
        if item.get_closest_marker("integration"):
            item.add_marker(skip)

import asyncio
import os

import pytest

from flakepilot.interface.composition_root import Secret, Settings, create_app

pytestmark = pytest.mark.integration


def test_startup_readiness_and_shutdown_against_postgresql() -> None:
    app = create_app(Settings(Secret(os.environ["FLAKEPILOT_TEST_DATABASE_URL"])))

    async def scenario() -> bool:
        async with app.router.lifespan_context(app):
            ready: bool = await app.state.readiness_probe()
        return ready

    assert asyncio.run(scenario()) is True
    assert app.state.ready is False

"""A Temporal worker process the local-server test SIGKILLs mid-activity (goal 5).

    python tests/temporal_kill_worker.py <temporal address> <database url> <marker>

It holds the real activity (advisory lock, outbox check) around an investigation
that writes `<marker>` and then hangs, so the test can kill it while the lock
and the activity attempt are both live.
"""

import asyncio
import sys
from pathlib import Path
from typing import Any

from temporalio.client import Client

from flakepilot.interface.composition_root.database import create_engine
from flakepilot.interface.composition_root.temporal_investigation import (
    InvestigationActivity,
    build_worker,
)
from flakepilot.interface.composition_root.temporal_workflow import (
    InvestigationRequest,
)
from flakepilot.shared_kernel.events import Origin


async def main(address: str, database_url: str, marker: Path) -> None:
    async def hang(request: InvestigationRequest, origin: Origin) -> Any:
        marker.write_text(request.key)
        await asyncio.sleep(3600)

    engine = create_engine(database_url)
    client = await Client.connect(address)
    activity = InvestigationActivity(engine, hang, heartbeat_every=0.2)
    await build_worker(client, activity).run()


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1], sys.argv[2], Path(sys.argv[3])))

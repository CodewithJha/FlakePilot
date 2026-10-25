"""Operator visibility for durable work: outbox backlogs and the job queue.

`backlog-status` counts, read-only, for the deployment's operator:

- per outbox (iam, ti, diag, exec, repo): undelivered rows (`published_at`
  null, not dead), dead rows (`dead_at` set), and the age in seconds of the
  oldest undelivered row;
- `orch.jobs` by state, plus the age in seconds of the oldest scheduled job.

Counts and ages only — never event names, correlation IDs or payloads, so the
output is safe to paste into a ticket. Exit code 0 even with backlogged rows:
the number is the message; alerting decides, not the CLI. Failures print the
cause class and never a database detail.
"""

import argparse
import asyncio
import json
import sys
from collections.abc import Sequence
from typing import Any

from sqlalchemy import text

from flakepilot.interface.composition_root import Settings
from flakepilot.interface.composition_root.database import (
    create_engine,
    dispose_engine,
)

PROG = "backlog-status"

_OUTBOXES = ("iam", "ti", "diag", "exec", "repo")

_OUTBOX_SQL = """
SELECT count(*) FILTER (WHERE published_at IS NULL AND dead_at IS NULL) AS pending,
       count(*) FILTER (WHERE dead_at IS NOT NULL) AS dead,
       COALESCE(
           EXTRACT(
               EPOCH FROM
               now() - min(occurred_at) FILTER (WHERE published_at IS NULL AND dead_at IS NULL)
           ),
           0
       )::bigint AS oldest_pending_age_seconds
FROM {schema}.outbox
"""

_JOBS_SQL = """
SELECT state,
       count(*) AS jobs,
       COALESCE(
           EXTRACT(
               EPOCH FROM
               now() - min(run_at) FILTER (WHERE state = 'scheduled')
           ),
           0
       )::bigint AS oldest_age_seconds
FROM orch.jobs
GROUP BY state
ORDER BY state
"""


async def _backlog(settings: Settings) -> dict[str, Any]:
    engine = create_engine(settings.database_url.reveal())
    try:
        outboxes: dict[str, Any] = {}
        async with engine.connect() as connection:
            for schema in _OUTBOXES:
                row = (
                    await connection.execute(text(_OUTBOX_SQL.format(schema=schema)))
                ).one()
                outboxes[schema] = {
                    "pending": int(row.pending),
                    "dead": int(row.dead),
                    "oldest_pending_age_seconds": int(row.oldest_pending_age_seconds),
                }
            jobs: dict[str, Any] = {}
            for row in (await connection.execute(text(_JOBS_SQL))).all():
                jobs[row.state] = {
                    "count": int(row.jobs),
                    "oldest_age_seconds": int(row.oldest_age_seconds),
                }
        return {"outboxes": outboxes, "jobs": jobs}
    finally:
        await dispose_engine(engine)


def run_backlog_status(settings: Settings, argv: Sequence[str]) -> int:
    parser = argparse.ArgumentParser(
        prog=PROG,
        description="Count undelivered and dead outbox rows and the job-queue "
        "backlog (read-only; counts and ages only, never event content)",
    )
    parser.parse_args(argv)
    try:
        payload = asyncio.run(_backlog(settings))
    except Exception as exc:  # noqa: BLE001 - operator CLI: cause class only
        print(f"{PROG}: {type(exc).__name__}", file=sys.stderr)
        return 1
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0

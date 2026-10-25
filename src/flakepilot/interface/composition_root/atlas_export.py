"""`export-reports-atlas`: copy a pair's finished run reports to MongoDB Atlas (goal 4).

    python -m flakepilot.interface.composition_root export-reports-atlas \\
        --org-id <uuid> --repo-id <uuid> [--limit N]

Operator trust, like `ci-run-report`. Needs `FLAKEPILOT_ATLAS_URI` and
`FLAKEPILOT_ATLAS_DATABASE`; without them nothing connects anywhere. Reads the
pair's newest `--limit` (1-200) stored runs from PostgreSQL, upserts each
classified report (`ReportExport`), audits every outcome in TI's outbox, and
prints a JSON summary. Atlas is never read back. Failures print a reason code
or the cause class, never a URI, host or driver message.
"""

import argparse
import asyncio
import json
import sys
import uuid
from collections.abc import Sequence
from typing import Any

from flakepilot.identity.application.authorization import ResourceNotFound
from flakepilot.integrations.infrastructure.atlas import (
    AtlasReportSink,
    ReportSinkError,
)
from flakepilot.interface.composition_root import Settings, ti_unit_of_work
from flakepilot.interface.composition_root.database import (
    create_engine,
    dispose_engine,
)
from flakepilot.shared_kernel.events import Channel, Origin
from flakepilot.test_intelligence.application.report_export import (
    ExportStatus,
    ReportExport,
)
from flakepilot.test_intelligence.application.run_view import MAX_INDEX_LIMIT

PROG = "export-reports-atlas"


def _limit(value: str) -> int:
    if not value.isdigit() or not 1 <= int(value) <= MAX_INDEX_LIMIT:
        raise argparse.ArgumentTypeError(
            f"must be an integer from 1 to {MAX_INDEX_LIMIT}"
        )
    return int(value)


def _parse(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog=PROG,
        description=(
            "Upsert a pair's newest classified run reports into MongoDB Atlas "
            "(write-only copy; PostgreSQL stays the system of record)."
        ),
    )
    parser.add_argument("--org-id", required=True, help="org ID (a UUID)")
    parser.add_argument("--repo-id", required=True, help="repo ID (a UUID)")
    parser.add_argument(
        "--limit", type=_limit, default=50, help=f"newest runs (1-{MAX_INDEX_LIMIT})"
    )
    return parser.parse_args(argv)


async def export_reports(
    settings: Settings, *, org_id: str, repo_id: str, limit: int
) -> dict[str, Any]:
    assert settings.atlas_uri is not None and settings.atlas_database is not None
    sink = AtlasReportSink(
        uri=settings.atlas_uri.reveal(), database=settings.atlas_database
    )
    engine = create_engine(settings.database_url.reveal())
    try:
        outcomes = await ReportExport(ti_unit_of_work(engine), sink).export(
            org_id=org_id,
            repo_id=repo_id,
            limit=limit,
            origin=Origin(Channel.CLI, str(uuid.uuid4())),
        )
    finally:
        await dispose_engine(engine)
        await sink.close()
    return {
        "org_id": org_id,
        "repo_id": repo_id,
        "sink": "mongodb_atlas",
        "totals": {
            status.value: sum(1 for o in outcomes if o.status is status)
            for status in ExportStatus
        },
        "reports": [
            {
                "run_id": o.run_id,
                "status": o.status.value,
                "report_revision": o.report_revision,
                "reason": o.reason,
            }
            for o in outcomes
        ],
    }


def run_export_reports_atlas(settings: Settings, argv: Sequence[str]) -> int:
    args = _parse(argv)
    if settings.atlas_uri is None or settings.atlas_database is None:
        print(
            f"{PROG}: needs FLAKEPILOT_ATLAS_URI and FLAKEPILOT_ATLAS_DATABASE",
            file=sys.stderr,
        )
        return 1
    try:
        summary = asyncio.run(
            export_reports(
                settings, org_id=args.org_id, repo_id=args.repo_id, limit=args.limit
            )
        )
    except ResourceNotFound:
        print(f"{PROG}: --org-id and --repo-id must be UUIDs", file=sys.stderr)
        return 1
    except ReportSinkError as exc:
        print(f"{PROG}: {exc.reason}", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001 - driver errors may carry hosts or URIs
        print(f"{PROG} failed: {type(exc).__name__}", file=sys.stderr)
        return 1
    print(json.dumps(summary, indent=2))
    return 0 if summary["totals"][ExportStatus.FAILED.value] == 0 else 1

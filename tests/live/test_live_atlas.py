"""One synthetic report through MongoDB Atlas (goal 4). Owner-run only.

Skipped unless the Atlas URI and database are in the environment, so the
default suite and CI contact nothing. A skip is not verification. The
cluster's network access list must allow this machine's IP:

    FLAKEPILOT_LIVE_ATLAS_URI=mongodb+srv://... FLAKEPILOT_LIVE_ATLAS_DATABASE=... \\
        uv run pytest tests/live -k atlas

Upserts one synthetic classified report (random org, repo and run IDs; no
repository data), upserts it again (no duplicate), reads it back by key (the
test's check only; FlakePilot itself never reads Atlas), then deletes that one
document.
"""

import asyncio
import os

import pytest
from atlas_support import SECRET_NAME, synthetic_report

from flakepilot.integrations.infrastructure.atlas import AtlasReportSink
from flakepilot.test_intelligence.application.report_export import (
    export_document,
    export_key,
    report_revision,
)

URI = os.environ.get("FLAKEPILOT_LIVE_ATLAS_URI", "")
DATABASE = os.environ.get("FLAKEPILOT_LIVE_ATLAS_DATABASE", "")

pytestmark = pytest.mark.skipif(
    not (URI and DATABASE),
    reason="FLAKEPILOT_LIVE_ATLAS_URI and FLAKEPILOT_LIVE_ATLAS_DATABASE are not set",
)


def test_live_atlas_upsert_is_idempotent_then_cleaned_up() -> None:
    document = export_document(synthetic_report())
    revision = report_revision(document)
    key = export_key(document, revision)

    async def scenario() -> tuple[bool, bool, dict[str, object] | None, int, int]:
        sink = AtlasReportSink(uri=URI, database=DATABASE)
        collection = sink._collection
        try:
            first = await sink.upsert(key, {**document, "report_revision": revision})
            second = await sink.upsert(key, {**document, "report_revision": revision})
            stored = await collection.find_one({"_id": key})
            count = await collection.count_documents({"_id": key})
            deleted = (await collection.delete_one({"_id": key})).deleted_count
        finally:
            await collection.delete_one({"_id": key})
            await sink.close()
        return first, second, stored, count, deleted

    first, second, stored, count, deleted = asyncio.run(scenario())
    assert (first, second, count, deleted) == (True, False, 1, 1)
    assert stored is not None
    assert stored["report_revision"] == revision
    assert stored["run_id"] == document["run_id"]
    assert SECRET_NAME not in repr(stored)
    print(f"atlas: inserted, unchanged on re-upsert, read back, deleted ({key[-12:]})")

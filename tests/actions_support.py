"""A scripted GitHub Actions API for backfill tests: runs, artifacts, signed zips."""

import io
import json
import urllib.parse
import urllib.request
import zipfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime

STORAGE = "https://productionresultssa1.blob.core.windows.net/actions-results"


def junit(*cases: tuple[str, str, str]) -> bytes:
    """(classname, name, outcome) with outcome passed, failed, flaky or errored."""
    body = {
        "passed": "",
        "failed": '<failure message="x"/>',
        "errored": '<error message="x"/>',
        "flaky": '<flakyFailure message="x"/>',
    }
    rows = "".join(
        f'<testcase classname="{c}" name="{n}">{body[o]}</testcase>'
        for c, n, o in cases
    )
    return f'<?xml version="1.0"?><testsuite name="s">{rows}</testsuite>'.encode()


def zip_of(entries: Mapping[str, bytes]) -> bytes:
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as bundle:
        for name, data in entries.items():
            bundle.writestr(name, data)
    return out.getvalue()


@dataclass
class FakeRun:
    id: int
    completed_at: datetime
    artifacts: dict[str, bytes]  # artifact name -> zip bytes
    attempt: int = 1
    sha: str = "ab" * 20
    branch: str | None = "main"


class FakeTokens:
    def __init__(self) -> None:
        self.calls = 0

    async def token(self, *, owner: str, name: str) -> str:
        del owner, name
        self.calls += 1
        return "ghs_" + "t" * 20


@dataclass
class ActionsApi:
    """Answers like api.github.com for one repository, plus its signed storage."""

    owner: str
    name: str
    runs: Sequence[FakeRun]
    requests: list[urllib.request.Request] = field(default_factory=list)

    def _artifact_ids(self) -> dict[int, tuple[FakeRun, str]]:
        ids: dict[int, tuple[FakeRun, str]] = {}
        for run in self.runs:
            for number, label in enumerate(sorted(run.artifacts), start=1):
                ids[run.id * 100 + number] = (run, label)
        return ids

    def __call__(
        self, request: urllib.request.Request, limit: int
    ) -> tuple[int, Mapping[str, str], bytes]:
        self.requests.append(request)
        parts = urllib.parse.urlsplit(request.full_url)
        if request.full_url.startswith(STORAGE):
            artifact_id = int(parts.path.rsplit("/", 1)[1])
            run, label = self._artifact_ids()[artifact_id]
            return 200, {}, run.artifacts[label]
        prefix = f"/repos/{self.owner}/{self.name}/actions/"
        assert parts.netloc == "api.github.com" and parts.path.startswith(prefix)
        tail = parts.path[len(prefix) :]
        if tail == "runs":
            return 200, {}, self._runs_page(urllib.parse.parse_qs(parts.query))
        if tail.startswith("runs/") and tail.endswith("/artifacts"):
            run_id = int(tail.split("/")[1])
            return 200, {}, self._artifacts(run_id)
        if tail.startswith("artifacts/") and tail.endswith("/zip"):
            artifact_id = int(tail.split("/")[1])
            return 302, {"location": f"{STORAGE}/{artifact_id}?sig=x"}, b""
        raise AssertionError(f"unexpected path {parts.path}")

    def _runs_page(self, query: Mapping[str, list[str]]) -> bytes:
        limit = int(query["per_page"][0])
        newest = sorted(self.runs, key=lambda r: r.completed_at, reverse=True)
        return json.dumps(
            {
                "total_count": len(newest),
                "workflow_runs": [
                    {
                        "id": run.id,
                        "run_attempt": run.attempt,
                        "status": "completed",
                        "conclusion": "success",
                        "head_sha": run.sha,
                        "head_branch": run.branch,
                        "run_started_at": run.completed_at.replace(
                            minute=0
                        ).isoformat(),
                        "updated_at": run.completed_at.isoformat(),
                    }
                    for run in newest[:limit]
                ],
            }
        ).encode()

    def _artifacts(self, run_id: int) -> bytes:
        found = [
            {
                "id": artifact_id,
                "name": label,
                "size_in_bytes": len(run.artifacts[label]),
                "expired": False,
            }
            for artifact_id, (run, label) in self._artifact_ids().items()
            if run.id == run_id
        ]
        return json.dumps({"total_count": len(found), "artifacts": found}).encode()

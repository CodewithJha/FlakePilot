"""Operator CLI commands: arg validation, fail-closed error display, no secrets."""

import json
import socket
import subprocess
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, contextmanager
from datetime import UTC, datetime
from typing import Any

import pytest
from support import SETTINGS

from flakepilot.interface.composition_root import Secret, Settings, operator
from flakepilot.interface.composition_root.operator import (
    run_ci_run_report,
    run_issue_ci_upload_token,
    run_list_runs,
)
from flakepilot.test_intelligence.application.run_view import (
    ClassifiedCase,
    ClassifiedRunReport,
)
from flakepilot.test_intelligence.application.store import (
    ReportedCase,
    RunIndexEntry,
    RunReport,
    RunSummary,
)
from flakepilot.test_intelligence.domain.classification import (
    Classification,
    FailureClass,
    Reason,
)

ORG = "11111111-2222-4333-8444-555555555555"
REPO = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
RUN = "cccccccc-dddd-4eee-8fff-000000000001"


def entry(
    run: str = RUN,
    *,
    environment: str = "demo.local",
    source: str = "ci_upload",
    commit: str = "0" * 40,
) -> RunIndexEntry:
    return RunIndexEntry(
        run_id=run,
        source=source,
        commit_sha=commit,
        environment=environment,
        revision_status="unchecked",
        received_at=datetime(2026, 10, 23, tzinfo=UTC),
        summary=RunSummary(
            tests=2, attempts=3, passed=1, failed=1, errored=0, skipped=1
        ),
    )


class _FakeEngine:
    """Touches nothing: run_list_runs wires a real engine; here it is replaced."""

    async def dispose(self) -> None: ...


@contextmanager
def _wire_index(monkeypatch: pytest.MonkeyPatch, entries: tuple[Any, ...]) -> Any:
    """The operator command's real flow, with the database edge replaced by one
    fake store delivering `entries`. Nothing else about the flow is altered."""

    class Store:
        async def runs_index(
            self, *, org_id: str, repo_id: str, limit: int
        ) -> tuple[Any, ...]:
            assert (org_id, repo_id) == (ORG, REPO)
            assert 1 <= limit <= 200
            return entries

    created: list[str] = []

    @asynccontextmanager
    async def unit_of_work() -> AsyncIterator[Store]:
        yield Store()

    def create_engine(url: str) -> _FakeEngine:
        created.append(url)
        return _FakeEngine()

    monkeypatch.setattr(operator, "create_engine", create_engine)
    monkeypatch.setattr(operator, "dispose_engine", _FakeEngine.dispose)
    monkeypatch.setattr(operator, "ti_unit_of_work", lambda _engine: unit_of_work)
    yield created


def test_token_issuance_refuses_non_uuid_ids_before_any_database_access(
    capsys: pytest.CaptureFixture[str],
) -> None:
    for org_id, repo_id in (("Organ-1", REPO), (ORG, "repo-demo")):
        assert (
            run_issue_ci_upload_token(
                SETTINGS, ["--org-id", org_id, "--repo-id", repo_id]
            )
            == 1
        )
        out, err = capsys.readouterr()
        assert out == "" and err.startswith("issue-ci-upload-token:")


def test_token_issuance_prints_usage_on_missing_arguments() -> None:
    with pytest.raises(SystemExit) as excinfo:
        run_issue_ci_upload_token(SETTINGS, ["--org-id", ORG])
    assert excinfo.value.code == 2


def test_token_issuance_reports_an_unreachable_database_by_class_only(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    dsn = f"postgresql://flakepilot:demo-SENTINEL-pw@127.0.0.1:{port}/flakepilot"
    argv = ["--org-id", ORG, "--repo-id", REPO]
    assert run_issue_ci_upload_token(Settings(Secret(dsn)), argv) == 1
    out, err = capsys.readouterr()
    assert out == ""
    assert err == "issue-ci-upload-token failed: ConnectionRefusedError\n"
    assert "demo-SENTINEL-pw" not in err


def test_report_reports_an_unreachable_database_by_class_only(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    dsn = f"postgresql://flakepilot:demo-SENTINEL-pw@127.0.0.1:{port}/flakepilot"
    argv = ["--org-id", ORG, "--repo-id", REPO, "--run-id", RUN]
    assert run_ci_run_report(Settings(Secret(dsn)), argv) == 1
    out, err = capsys.readouterr()
    assert out == "" and err.startswith("ci-run-report failed: ConnectionRefusedError")


def test_report_refuses_non_uuid_ids_before_any_database_access(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert (
        run_ci_run_report(
            SETTINGS, ["--org-id", "N", "--repo-id", REPO, "--run-id", RUN]
        )
        == 1
    )
    out, err = capsys.readouterr()
    assert out == "" and err.startswith("ci-run-report:")


def test_report_prints_usage_on_missing_arguments() -> None:
    with pytest.raises(SystemExit) as excinfo:
        run_ci_run_report(SETTINGS, ["--org-id", ORG])
    assert excinfo.value.code == 2


def _report() -> RunReport:
    return RunReport(
        run_id=RUN,
        org_id=ORG,
        repo_id=REPO,
        upload_id="cccccccc-dddd-4eee-8fff-000000000002",
        source="ci_upload",
        commit_sha="0" * 40,
        environment="demo.local",
        revision_status="unchecked",
        received_at=None,
        summary=RunSummary(
            tests=1, attempts=2, passed=1, failed=0, errored=0, skipped=0
        ),
        metadata=None,
        content_sha256=None,
        size_bytes=None,
        artifact_key=None,
        results=(
            ReportedCase(
                classname="demo.auth",
                name="test_login & <totals>",
                attempts=("failed", "passed"),
                test_id="dddddddd-eeee-4fff-8aaa-000000000003",
            ),
        ),
    )


FLAKY = Classification(
    failure_class=FailureClass.FLAKY,
    reason=None,
    target_sha="0" * 40,
    evidence={
        "environment": "demo.local",
        "failed": 1,
        "passed": 1,
        "target_runs": ["run-a", "run<b>"],
    },
    rule_version="ti-classification/1",
)


def _classified(
    classification: Classification | None = FLAKY,
) -> ClassifiedRunReport:
    run = _report()
    [case] = run.results
    return ClassifiedRunReport(
        run=run, cases=(ClassifiedCase(case=case, classification=classification),)
    )


def test_html_report_escapes_names_and_all_recorded_evidence() -> None:
    from flakepilot.interface.composition_root.operator import _html_report

    rendered = _html_report(_classified())
    assert "<h2>Summary</h2>" in rendered
    assert "test_login &amp; &lt;totals&gt;" in rendered
    assert "failed &rarr; passed" in rendered
    assert "<td>failed &rarr; passed</td>" in rendered  # cell, not just the note
    assert "operator demo" in rendered
    assert "revision status stays <code>unchecked</code>" in rendered
    assert "run&lt;b&gt;" in rendered  # evidence values are escaped too
    assert "flaky<br><span" in rendered
    assert "rule ti-classification/1" in rendered
    assert "target_sha " + "0" * 40 in rendered


def test_html_report_shows_unavailable_when_there_is_no_verdict() -> None:
    from flakepilot.interface.composition_root.operator import _html_report

    rendered = _html_report(_classified(None))
    assert "classification unavailable" in rendered


def test_html_report_carries_the_reason_when_the_rule_set_gives_one() -> None:
    from flakepilot.interface.composition_root.operator import _html_report

    verdict = Classification(
        failure_class=FailureClass.INCONCLUSIVE,
        reason=Reason.NO_PASSING_BASELINE,
        target_sha="0" * 40,
        evidence={"failed": 2},
        rule_version="ti-classification/1",
    )
    rendered = _html_report(_classified(verdict))
    assert "inconclusive" in rendered and "no_passing_baseline" in rendered


def test_json_report_carries_the_attempt_chain_and_the_verdict() -> None:
    import json

    from flakepilot.interface.composition_root.operator import _json_report

    payload = json.loads(_json_report(_classified()))
    assert payload["summary"]["attempts"] == 2
    first = payload["results"][0]
    assert first["attempts"] == ["failed", "passed"]  # regression: chain intact
    assert first["classname"] == "demo.auth"
    assert first["test_id"] == "dddddddd-eeee-4fff-8aaa-000000000003"
    verdict = first["classification"]
    assert verdict is not None
    assert verdict["failure_class"] == "flaky"
    assert verdict["reason"] is None
    assert verdict["target_sha"] == "0" * 40
    assert verdict["rule_version"] == "ti-classification/1"
    assert verdict["evidence"]["environment"] == "demo.local"
    assert verdict["evidence"]["target_runs"] == [
        "run-a",
        "run<b>",
    ]  # not escaped in JSON
    assert json.dumps(verdict["evidence"])  # evidence survives a real JSON round-trip


def test_json_report_uses_null_when_no_verdict_exists() -> None:
    import json

    from flakepilot.interface.composition_root.operator import _json_report

    payload = json.loads(_json_report(_classified(None)))
    assert payload["results"][0]["classification"] is None


def test_server_entry_point_still_refuses_unknown_commands() -> None:
    import os

    environ = {k: v for k, v in os.environ.items() if not k.startswith("FLAKEPILOT_")}
    result = subprocess.run(
        [sys.executable, "-m", "flakepilot.interface.composition_root", "frobnicate"],
        env={**environ, "FLAKEPILOT_DATABASE_URL": "postgresql://u@db.invalid/x"},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 1
    assert "Usage" in result.stderr


def test_list_runs_refuses_non_uuid_ids_before_any_database_access(
    capsys: pytest.CaptureFixture[str],
) -> None:
    for bad_id, flag in (("Organ-1", "--org-id"), ("repo-demo", "--repo-id")):
        argv = (
            ["--org-id", bad_id, "--repo-id", REPO]
            if flag == "--org-id"
            else ["--org-id", ORG, "--repo-id", bad_id]
        )
        assert run_list_runs(SETTINGS, argv) == 1
        err = capsys.readouterr().err
        assert f"{flag} must be a canonical UUID" in err
        assert bad_id not in err  # the message names the flag, not the value


def test_list_runs_prints_usage_on_missing_arguments() -> None:
    with pytest.raises(SystemExit) as excinfo:
        run_list_runs(SETTINGS, ["--org-id", ORG])
    assert excinfo.value.code == 2


def test_list_runs_reports_an_unreachable_database_by_class_only(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
        dsn = f"postgresql://flakepilot:demo@127.0.0.1:{port}/flakepilot"
    argv = ["--org-id", ORG, "--repo-id", REPO]
    assert run_list_runs(Settings(Secret(dsn)), argv) == 1
    err = capsys.readouterr().err
    assert "list-runs failed:" in err
    assert dsn not in err  # never a driver detail or a connection string


def test_list_runs_prints_one_line_per_run_with_the_run_id_first(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    wired = _wire_index(monkeypatch, (entry(), entry(run=RUN[:-1] + "2")))
    with wired:
        assert run_list_runs(SETTINGS, ["--org-id", ORG, "--repo-id", REPO]) == 0
    out = capsys.readouterr().out
    lines = [line for line in out.splitlines() if line.startswith("cccccccc")]
    # The store owns ordering (integration-tested); the command is a pass-through.
    assert [line.split()[0] for line in lines] == [RUN, RUN[:-1] + "2"]
    assert "revision=unchecked" in lines[0]  # the honest status is printed


def test_list_runs_json_carries_each_run_row_and_the_summary(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    wired = _wire_index(monkeypatch, (entry(),))
    with wired:
        assert (
            run_list_runs(
                SETTINGS,
                [
                    "--org-id",
                    ORG,
                    "--repo-id",
                    REPO,
                    "--format",
                    "json",
                    "--limit",
                    "5",
                ],
            )
            == 0
        )
    payload = json.loads(capsys.readouterr().out)
    assert payload["org_id"] == ORG and payload["repo_id"] == REPO
    [run] = payload["runs"]
    assert run["run_id"] == RUN and run["source"] == "ci_upload"
    assert run["revision_status"] == "unchecked"
    assert run["summary"]["tests"] == 2 and run["summary"]["attempts"] == 3


def test_list_runs_empty_pair_says_so_instead_of_printing_nothing(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    with _wire_index(monkeypatch, ()):
        assert run_list_runs(SETTINGS, ["--org-id", ORG, "--repo-id", REPO]) == 0
    assert "no stored runs" in capsys.readouterr().out

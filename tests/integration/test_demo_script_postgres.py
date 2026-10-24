"""The demo script (ops/demo/run-demo.sh) end to end over a throwaway database.

These tests run the real script with its real stack — alembic migrations, the
API, the worker — exactly the journey a developer performs from the README, so
`integration` gates them on FLAKEPILOT_TEST_DATABASE_URL like every other
PostgreSQL test. Everything the script ingests is the hand-written fixtures
under ops/demo/: no repository code is ever executed, and nothing here claims
code-host verification, revision ancestry, or any isolation evidence.

The two clean runs also exercise repeatability: the script re-runs against the
same database (fresh demo org each time) and demonstrates the same discovery,
selection and classification evidence twice.
"""

import json
import os
import re
import signal
import socket
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "ops" / "demo" / "run-demo.sh"

BASELINE_SHA = "2424242424242424242424242424242424242424"
CURRENT_SHA = "000102030405060708090a0b0c0d0e0f10111213"
ENVIRONMENT = "demo.local"


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest.fixture
def demo(
    tmp_path: Path, database_url: str, request: pytest.FixtureRequest
) -> dict[str, Any]:
    """One throwaway database plus the script's environment for one run."""
    out_dir = tmp_path / "demo-out"
    out_dir.mkdir()
    fixture_overrides: dict[str, Path] = getattr(request, "param", {})
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("FLAKEPILOT_", "DEMO_"))
    }
    env.update(
        FLAKEPILOT_DATABASE_URL=database_url,
        FLAKEPILOT_PORT=str(_free_port()),
        FLAKEPILOT_ARTIFACT_DIR=str(tmp_path / "artifacts"),
        DEMO_API_LOG=str(tmp_path / "api.log"),
        DEMO_WORKER_LOG=str(tmp_path / "worker.log"),
        DEMO_REPORT=str(out_dir / "report.html"),
        DEMO_OUTPUT_DIR=str(out_dir),
    )
    for key, path in fixture_overrides.items():
        env[key] = str(path)
    env.setdefault(
        "DEMO_BASELINE_FIXTURE",
        str(REPO_ROOT / "ops" / "demo" / "fixture-junit-baseline.xml"),
    )
    env.setdefault(
        "DEMO_CURRENT_FIXTURE", str(REPO_ROOT / "ops" / "demo" / "fixture-junit.xml")
    )
    return {"env": env, "out_dir": out_dir, "database_url": database_url}


def _run_script(env: dict[str, str], timeout: int = 420) -> tuple[int, str, str]:
    """Run the demo in its own process group so a timeout cannot leak the stack."""
    process = subprocess.Popen(
        [str(SCRIPT)],
        env=env,
        cwd=REPO_ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        out, err = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        os.killpg(os.getpgid(process.pid), signal.SIGTERM)
        out, err = process.communicate()
        pytest.fail(f"demo script did not finish within {timeout}s")
    return process.returncode, out, err


def _identity(out: str) -> tuple[str, str, str, str]:
    """The (org, repo, current, baseline) run IDs the script actually printed.

    The two echoed ingest summaries are JSON in ingest order: the baseline
    first (nothing failing), the current run second. The outcomes assert that
    order from the script's own output.
    """
    org = re.search(r"^org: ([0-9a-f-]{36})$", out, re.MULTILINE)
    repo = re.search(r"^repo: ([0-9a-f-]{36})\b", out, re.MULTILINE)
    summaries = re.findall(r"^ingest summary: (\{.*\})$", out, re.MULTILINE)
    assert org and repo and summaries, out[-2000:]
    payload = [json.loads(summary) for summary in summaries]
    assert len(payload) == 2, out[-2000:]
    baseline, current = payload
    assert baseline["outcomes"]["failed"] == 0, (
        "the baseline (run 1/2) must be all passing"
    )
    assert current["outcomes"]["failed"] == 1, (
        "the current run (2/2) is the failing fixture"
    )
    return (org.group(1), repo.group(1), current["run_id"], baseline["run_id"])


def _json(path: Path) -> dict[str, Any]:
    payload: Any = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(payload, dict)
    return payload


def _by_name(report: dict[str, Any]) -> dict[tuple[str, str], dict[str, Any]]:
    return {
        (result["classname"], result["name"]): result for result in report["results"]
    }


def _assert_run_report(report: dict[str, Any], demo_ids: dict[str, str]) -> None:
    """The rule set's verdicts on the demo fixtures, exactly as documented."""
    assert report["run_id"] == demo_ids["current"]
    assert report["commit_sha"] == CURRENT_SHA
    assert report["environment"] == ENVIRONMENT
    assert report["revision_status"] == "unchecked"
    assert report["summary"] == {  # tests by final outcome; attempts count all tries
        "tests": 3,
        "attempts": 5,
        "passed": 2,
        "failed": 1,
        "errored": 0,
        "skipped": 0,
    }
    by_name = _by_name(report)
    login = by_name[("demo.auth", "test_login_succeeds")]["classification"]
    assert login["failure_class"] == "flaky"
    assert login["rule_version"] == "ti-classification/1"
    assert login["target_sha"] == CURRENT_SHA
    assert login["evidence"]["environment"] == ENVIRONMENT
    assert login["evidence"]["target_runs"] == [demo_ids["current"]]

    cart = by_name[("demo.checkout", "test_cart_totals_round_to_cents")][
        "classification"
    ]
    assert cart["failure_class"] == "deterministic_regression"
    assert cart["evidence"]["baseline_sha"] == BASELINE_SHA
    assert cart["evidence"]["baseline_runs"] == [demo_ids["baseline"]]
    assert cart["evidence"]["target_runs"] == [demo_ids["current"]]
    assert cart["evidence"]["failed"] == 2  # failed then failed, the stored chain

    cookie = by_name[("demo.auth", "test_session_cookie_is_secure")]["classification"]
    assert cookie["failure_class"] == "inconclusive"
    assert cookie["reason"] == "insufficient_history"
    assert cookie["target_sha"] is None

    asserts = [(r["classname"], r["name"], r["attempts"]) for r in report["results"]]
    assert (  # the chains themselves, straight from the fixtures
        ("demo.auth", "test_login_succeeds", ["failed", "passed"]) in asserts
    )
    assert (
        "demo.checkout",
        "test_cart_totals_round_to_cents",
        ["failed", "failed"],
    ) in asserts
    assert (
        "demo.auth",
        "test_session_cookie_is_secure",
        ["passed"],
    ) in asserts


def _assert_outputs(demo_data: dict[str, Any], demo_ids: dict[str, str]) -> None:
    """Discovery artifact, JSON report, narrative and HTML report agree."""
    out_dir: Path = demo_data["out_dir"]
    listing = _json(out_dir / "runs.json")
    assert (
        listing["org_id"] == demo_ids["org"] and listing["repo_id"] == demo_ids["repo"]
    )
    assert [entry["run_id"] for entry in listing["runs"]] == [
        demo_ids["current"],
        demo_ids["baseline"],
    ], "the bounded listing must be newest first: the current run above the baseline"
    for entry in listing["runs"]:
        assert entry["environment"] == ENVIRONMENT
        assert entry["revision_status"] == "unchecked"
    assert (
        listing["runs"][0]["summary"]["failed"] == 1
    )  # the current run's final outcomes

    report = _json(out_dir / "run-report.json")
    _assert_run_report(report, demo_ids)

    narrative = (out_dir / "evidence-narrative.txt").read_text(encoding="utf-8")
    for fact in (
        demo_ids["current"],
        demo_ids["baseline"],
        BASELINE_SHA,
        CURRENT_SHA,
        "ti-classification/1",
        "flaky",
        "deterministic_regression",
        "inconclusive",
        "insufficient_history",
        "NOT proof of Git ancestry",
        "revision_status stays 'unchecked'",
        "no repository code was executed",
    ):
        assert fact in narrative, fact

    html = (out_dir / "report.html").read_text(encoding="utf-8")
    assert "unchecked" in html and "ti-classification/1" in html
    assert "deterministic_regression" in html
    assert demo_ids["current"] in html


def test_two_clean_runs_repeat_the_whole_journey(demo: dict[str, Any]) -> None:
    """Two clean runs: identical honest behavior, fresh org and run IDs each time."""
    identities = []
    out_dir = Path(demo["env"]["DEMO_OUTPUT_DIR"])
    for round_number in (1, 2):
        # Clear round 1's artifacts so round 2's assertions cannot read stale files.
        for name in (
            "runs.json",
            "run-report.json",
            "evidence-narrative.txt",
            "report.html",
        ):
            (out_dir / name).unlink(missing_ok=True)
        code, out, err = _run_script(demo["env"])
        assert code == 0, f"run {round_number} failed: exit {code}\n{err[-3000:]}"
        org, repo, current, _baseline_evidence = _identity(out)
        identities.append(
            {
                "org": org,
                "repo": repo,
                "current": current,
                "baseline": _baseline_evidence,
            }
        )
        _assert_outputs({"out_dir": out_dir}, identities[-1])
        assert "== discovering the stored runs" in out
        assert "selection confirmed" in out

    first, second = identities
    assert first["org"] != second["org"] and first["current"] != second["current"]
    # The second run discovered its own two runs, and only its own: the first
    # run's runs stay under the first org (org-scoped listing, nothing shared).


def test_a_foreign_org_cannot_see_the_demo_orgs_runs(demo: dict[str, Any]) -> None:
    """Operator reads keep their (org, repo) scope: nothing leaks across orgs."""
    code, out, err = _run_script(demo["env"])
    assert code == 0, err[-3000:]
    org, repo, current, _baseline_evidence = _identity(out)
    common = {
        "FLAKEPILOT_DATABASE_URL": demo["database_url"],
    }
    foreign_org = "99999999-9999-4999-8999-999999999999"

    listing = subprocess.run(
        [
            sys.executable,
            "-m",
            "flakepilot.interface.composition_root",
            "list-runs",
            "--org-id",
            foreign_org,
            "--repo-id",
            repo,
            "--format",
            "json",
        ],
        env={**os.environ, **common},
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        check=True,
    )
    payload = json.loads(listing.stdout)
    assert payload["runs"] == []  # another org's runs are simply not there

    wrong_repo = subprocess.run(
        [
            sys.executable,
            "-m",
            "flakepilot.interface.composition_root",
            "ci-run-report",
            "--org-id",
            org,
            "--repo-id",
            "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee",
            "--run-id",
            current,
        ],
        env={**os.environ, **common},
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        check=False,
    )
    assert wrong_repo.returncode == 1
    assert "no such run in this (org, repo) pair" in wrong_repo.stderr
    assert current not in wrong_repo.stderr


def test_a_malformed_current_fixture_stops_the_journey(demo: dict[str, Any]) -> None:
    """A bad fixture must fail loudly (the API refuses; the script propagates)."""
    malformed = demo["out_dir"].parent / "malformed.xml"
    malformed.write_text("this is <not junit at all", encoding="utf-8")
    demo["env"]["DEMO_CURRENT_FIXTURE"] = str(malformed)
    code, out, err = _run_script(demo["env"])
    assert code != 0
    assert "Demo complete" not in out
    assert "== discovering the stored runs" not in out  # nothing ran after the failure
    assert not (demo["out_dir"] / "report.html").exists()
    assert "curl" in err  # the API's refusal surfaced through curl, then set -e stopped


def test_a_missing_baseline_fixture_is_refused_before_the_stack_starts(
    demo: dict[str, Any],
) -> None:
    """The pre-flight refuses to boot anything without the demo's raw material."""
    demo["env"]["DEMO_BASELINE_FIXTURE"] = str(demo["out_dir"] / "does-not-exist.xml")
    code, out, err = _run_script(demo["env"])
    assert code != 0
    assert "demo fixture not readable" in err
    assert "migrating" not in out

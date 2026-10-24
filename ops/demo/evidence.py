"""Demo evidence helpers for run-demo.sh: selection checking and the narrative.

Two read-only subcommands over the operator CLI's own JSON outputs. Neither
recomputes any classification rule: `ti-classification/1` produced the verdicts
in the stored report, and this helper only checks that the demo's expected,
deterministic fixture outcomes are present and cites them (with the stored run
IDs, attempt chains, baseline evidence, environment and rule version) instead
of re-deriving or rewording anything.

- `select RUNS_JSON CURRENT_RUN_ID BASELINE_RUN_ID CURRENT_SHA BASELINE_SHA`
  re-derives the demo's selection from the actual `list-runs` output: the run
  to inspect is whatever the listing says it is. Fails loudly (exit 1) if the
  listing does not match the ingest responses, so a wrong or guessed run ID can
  never slip into the demo.

- `narrate REPORT_JSON CURRENT_RUN_ID BASELINE_RUN_ID BASELINE_SHA ORG REPO`
  prints the evidence narrative for the ingested fixtures from the stored
  report's own JSON, and fails (exit 1) if the rule set did not produce all
  three observed verdicts on this data.
"""

import json
import sys
from typing import Any

RULE_VERSION = "ti-classification/1"
EXPECTED_CLASSES = {"flaky", "deterministic_regression", "inconclusive"}


def _die(message: str) -> None:
    print(f"evidence.py: {message}", file=sys.stderr)
    sys.exit(1)


def _load(path: str) -> dict[str, Any]:
    try:
        with open(path, encoding="utf-8") as stream:
            payload: Any = json.load(stream)
    except (OSError, json.JSONDecodeError) as exc:
        _die(f"cannot read JSON at {path}: {exc}")
    if not isinstance(payload, dict):
        _die(f"unexpected JSON at {path}: expected an object")
    return payload


def select(argv: list[str]) -> None:
    """The demo's run selection, re-derived from the actual listing output."""
    if len(argv) != 5:
        _die(
            "select needs: RUNS_JSON CURRENT_RUN_ID BASELINE_RUN_ID CURRENT_SHA BASELINE_SHA"
        )
    path, current_run, baseline_run, current_sha, baseline_sha = argv
    listing = _load(path)
    runs = listing.get("runs")
    if not isinstance(runs, list):
        _die(f"{path}: no runs array")
    if len(runs) != 2:
        _die(
            f"{path}: expected exactly the two demo runs, got {len(runs)}"
            " (a listing page cannot be trusted for selection if the content is unexpected)"
        )
    listed_runs = [entry.get("run_id") for entry in runs]
    if listed_runs != [current_run, baseline_run]:
        _die(
            f"{path}: the listing does not match the ingest responses: "
            f"expected newest first {current_run}, {baseline_run}; got {listed_runs}"
        )
    shas = [entry.get("commit_sha") for entry in runs]
    if shas != [current_sha, baseline_sha]:
        _die(f"{path}: commit SHAs do not match the demo fixtures: {shas}")
    statuses = [entry.get("revision_status") for entry in runs]
    if any(status != "unchecked" for status in statuses):
        _die(f"{path}: revision_status is not 'unchecked' for every run: {statuses}")
    print(f"selected: {runs[0]['run_id']} (newest first in the listing)")
    print("selection confirmed: the discovered listing is the ingest responses' runs.")
    print(
        "every listed run is revision_status=unchecked; the listing was a bounded page."
    )


def narrate(argv: list[str]) -> None:
    """The evidence narrative, written only from the stored report's own facts."""
    if len(argv) != 6:
        _die(
            "narrate needs: REPORT_JSON CURRENT_RUN_ID BASELINE_RUN_ID BASELINE_SHA ORG REPO"
        )
    (path, current_run, baseline_run, baseline_sha, org_id, repo_id) = argv
    report = _load(path)
    if report.get("run_id") != current_run:
        _die(
            f"{path} is for run {report.get('run_id')}, not the selected {current_run}"
        )
    if report.get("org_id") != org_id or report.get("repo_id") != repo_id:
        _die(f"{path}: (org, repo) does not match the demo pair")
    if report.get("revision_status") != "unchecked":
        _die(f"{path}: unexpected revision status {report.get('revision_status')}")
    results = report.get("results")
    if not isinstance(results, list) or not results:
        _die(f"{path}: no classified results")

    named: dict[str, tuple[str, str]] = {}
    by_test: dict[tuple[str, str], dict[str, Any]] = {}
    for result in results:
        classification = result.get("classification")
        if classification is None:
            continue
        test = (result.get("classname", ""), result.get("name", ""))
        by_test[test] = result
        named.setdefault(classification.get("failure_class", ""), test)

    missing = EXPECTED_CLASSES.difference(named)
    if missing:
        _die(f"the rule set produced no {sorted(missing)} verdict on this demo data")
    regressed = by_test[named["deterministic_regression"]]
    evidence = regressed["classification"]["evidence"]
    if evidence.get("baseline_sha") != baseline_sha:
        _die(f"baseline_sha is {evidence.get('baseline_sha')}, not the baseline commit")
    if list(evidence.get("baseline_runs") or []) != [baseline_run]:
        _die(
            f"baseline_runs is {evidence.get('baseline_runs')}, not the stored baseline run"
        )

    def chain(result: dict[str, Any]) -> str:
        attempts = result.get("attempts")
        if not isinstance(attempts, list) or not attempts:
            _die(f"{path}: an attempt chain is missing for {result.get('name')}")
        return " then ".join(str(attempt) for attempt in attempts)

    lines = [
        "Evidence narrative",
        "------------------",
        "Everything below is synthetic demo data (hand-written fixtures under",
        "ops/demo/): no repository code was executed to produce, ingest, or verify",
        "any of it, and reproduction stayed closed.",
        "",
        f"Investigated pair: org {org_id}, repo {repo_id} (demo-scoped identifiers; nothing",
        "is registered at a code host). Every commit SHA here is what the fixture",
        "upload reported; every run's revision_status stays 'unchecked'.",
        "",
        f"Rule set: {RULE_VERSION} (deterministic, versioned; no confidence scores). The",
        "verdicts below are exactly what the rule set recorded for the selected run",
        f"{current_run}; this narrative cites them, it does not recompute them.",
        "",
        f"Stored runs (newest first, from list-runs): {current_run} (commit",
        f"{report.get('commit_sha')}), then earlier stored run {baseline_run} (commit",
        f"{baseline_sha}), both in environment {report.get('environment')}.",
        "",
    ]
    for test, result in sorted(by_test.items()):
        classification = result["classification"]
        evidence = classification.get("evidence", {})
        lines.append(f"- {test[0]}.{test[1]}: {classification['failure_class']}")
        lines.append(f"  stored attempt chain in run {current_run}: {chain(result)}")
        if classification.get("reason"):
            lines.append(f"  reason: {classification['reason']}")
        facts = ", ".join(
            f"{key}={evidence[key]}"
            for key in sorted(evidence)
            if key != "excluded_runs" and evidence[key]
        )
        if facts:
            lines.append(f"  recorded evidence: {facts}")
        if classification["failure_class"] == "deterministic_regression":
            lines.append(
                "  why regression: the rule set's baseline is an earlier stored run"
            )
            lines.append(
                f"  ({evidence.get('baseline_runs')}, commit {evidence.get('baseline_sha')})"
            )
            lines.append(
                "  that passed this test in the same environment, before the current"
            )
            lines.append("  failing run was ingested.")
    lines += [
        "",
        "The baseline run was uploaded before the current run by this demo's own",
        "setup (step 5 of the README journey). That ordering is a demo choice,",
        "NOT proof of Git ancestry; ancestry would need revision verification,",
        "which stays open (ADR-013).",
        "",
        "Verdict classes demonstrated on this data: flaky, deterministic_regression",
        "(with baseline evidence), inconclusive (no failing attempt anywhere for the",
        "third test, so the rules report insufficient history instead of a class).",
    ]
    print("\n".join(lines))


def main() -> None:
    if not sys.argv[1:]:
        _die("usage: evidence.py select ... | narrate ...")
    command, argv = sys.argv[1], sys.argv[2:]
    if command == "select":
        select(argv)
    elif command == "narrate":
        narrate(argv)
    else:
        _die(f"unknown subcommand {command!r}")


if __name__ == "__main__":
    main()

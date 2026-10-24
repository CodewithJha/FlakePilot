#!/bin/sh
# FlakePilot MVP demo: one complete shell run of the documented demo journey.
#
#   1. require an explicitly configured local PostgreSQL demo database
#      (the README's `createdb flakepilot_demo` step; nothing here reconfigures it)
#   2. migrate the demo database, start the API and worker that already exist
#   3. bootstrap an org with the operator CLI (the command the README documents) and
#      create the demo-scoped repository identity (checklist step 4 of the README
#      journey; nothing is registered at a code host, see the echo there)
#   4. issue a CI upload token with the operator CLI (scoped to that one pair)
#   5. POST two demo JUnit fixtures from ops/demo/ (clearly hand-written, no
#      repository code was ever executed to produce them): a passing baseline
#      on an earlier commit, then the flaky/failing run. Both stay fixtures.
#      Baseline-before-current is this demo's setup choice; it is NOT Git
#      ancestry proof.
#   6. discover the stored runs with `list-runs` (newest first, bounded page) and
#      select the run to inspect from that actual output — never a guessed ID
#   7. render the selected run as a read-only JSON report and a self-contained
#      HTML report with the operator CLI: attempt chains plus each test's
#      classification from the deterministic rule set ti-classification/1
#      (flaky / deterministic_regression with the baseline evidence / a truthful
#      inconclusive)
#   8. print an evidence narrative citing the stored run IDs, attempt chains,
#      baseline evidence, environment and rule version — written from the JSON
#      report the rule set produced, never recomputed here
#
# Outputs (all under $DEMO_OUTPUT_DIR unless overridden): runs.json, the JSON
# report, the evidence narrative, and the HTML report at $DEMO_REPORT.
#
# Prerequisites: PostgreSQL (already running) with a database created:
#
#   createdb flakepilot_demo          # or: psql -c 'CREATE DATABASE flakepilot_demo'
#   export FLAKEPILOT_DATABASE_URL='postgresql://postgres@localhost:5432/flakepilot_demo'
#
# Production untrusted-code execution stays disabled: this demo never runs
# repository code, and FLAKEPILOT_EXECUTION_MODE is left at the default
# `closed` mode throughout.

set -eu

: "${FLAKEPILOT_DATABASE_URL:?export FLAKEPILOT_DATABASE_URL first}"

DEMO_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$DEMO_DIR/../.." && pwd)"
BASELINE_FIXTURE="${DEMO_BASELINE_FIXTURE:-$DEMO_DIR/fixture-junit-baseline.xml}"
CURRENT_FIXTURE="${DEMO_CURRENT_FIXTURE:-$DEMO_DIR/fixture-junit.xml}"
API_LOG="${DEMO_API_LOG:-flakepilot-demo-api.log}"
WORKER_LOG="${DEMO_WORKER_LOG:-flakepilot-demo-worker.log}"
REPORT="${DEMO_REPORT:-flakepilot-demo-report.html}"
OUTPUT_DIR="${DEMO_OUTPUT_DIR:-$REPO_ROOT/flakepilot-demo-output}"
BASE="http://127.0.0.1:${FLAKEPILOT_PORT:-8000}"
DEMO_SHA="000102030405060708090a0b0c0d0e0f10111213"

# The fixtures are the demo's raw material: refuse to start without them
# instead of failing later with a parser error mid-journey.
for fixture in "$BASELINE_FIXTURE" "$CURRENT_FIXTURE"; do
  if [ ! -r "$fixture" ]; then
    echo "demo fixture not readable: $fixture" >&2
    exit 1
  fi
done

# Ingestion stores the raw report body as a plaintext file under this
# directory (no retention job deletes them; encryption is not wired).
DEMO_ARTIFACTS="${FLAKEPILOT_ARTIFACT_DIR:-${TMPDIR:-/tmp}/flakepilot-demo-artifacts}"
mkdir -p "$DEMO_ARTIFACTS" "$OUTPUT_DIR"
chmod 700 "$DEMO_ARTIFACTS" "$OUTPUT_DIR"
export FLAKEPILOT_ARTIFACT_DIR="$DEMO_ARTIFACTS"

cd "$REPO_ROOT"

stop() {
  [ -n "${WORKER_PID:-}" ] && kill "$WORKER_PID" 2>/dev/null
  [ -n "${API_PID:-}" ] && kill "$API_PID" 2>/dev/null
}
trap stop EXIT INT TERM

echo "== migrating (alembic upgrade head; the explicit step the README documents)"
uv run alembic upgrade head

echo "== starting API (logs: $API_LOG)"
: > "$API_LOG"
uv run python -m flakepilot.interface.composition_root >>"$API_LOG" 2>&1 &
API_PID=$!

echo "== waiting for readiness"
for _ in $(seq 1 30); do
  if curl -fsS "$BASE/health/ready" >/dev/null 2>&1; then break; fi
  if ! kill -0 "$API_PID" 2>/dev/null; then
    echo "API process exited early; last log lines:" >&2
    tail -5 "$API_LOG" >&2
    exit 1
  fi
  sleep 1
done
curl -fsS "$BASE/health/ready" >/dev/null || { echo "API never became ready" >&2; exit 1; }

echo "== starting worker (logs: $WORKER_LOG)"
: > "$WORKER_LOG"
uv run python -m flakepilot.interface.composition_root worker >>"$WORKER_LOG" 2>&1 &
WORKER_PID=$!

echo "== bootstrapping the demo org (admin GitHub id 424242 is local demo data)"
# Org names are unique per database; a fresh name keeps re-runs of this script
# working against the same database. `dropdb` + `createdb` gives a clean slate.
ORG_NAME="DemoOrg-$(date +%Y%m%d%H%M%S)"
ORG_ID=$(uv run python -m flakepilot.interface.composition_root bootstrap-org \
  --name "$ORG_NAME" --admin-github-id 424242 --admin-login demo-admin)
echo "org: $ORG_ID"

REPO_ID=$(uv run python -c "import uuid; print(uuid.uuid4())")
echo "repo: $REPO_ID (a demo-scoped identifier tied to this token; no repository was registered at a code host, so register-repo would fabricate one and is deliberately not called)"

echo "== issuing a CI upload token (shown once; only its SHA-256 is stored)"
TOKEN=$(uv run python -m flakepilot.interface.composition_root issue-ci-upload-token \
  --org-id "$ORG_ID" --repo-id "$REPO_ID" 2>/dev/null | tail -n 1)
echo "token prefix: $(printf '%s' "$TOKEN" | cut -c1-12)..."

echo "== ingesting the demo JUnit fixtures (POST /ingest/junit, the API's existing path)"
echo "   1/2: the passing baseline (hand-written fixture, earlier demo commit)"
BASELINE_SHA="2424242424242424242424242424242424242424"
BASELINE_RESPONSE=$(curl -fsS -X POST "$BASE/ingest/junit" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/xml" \
  -H "X-FlakePilot-CI-Source: demo-fixture" \
  -H "X-FlakePilot-CI-Run-Id: demo-run-baseline" \
  -H "X-FlakePilot-CI-Attempt: 1" \
  -H "X-FlakePilot-Job-Name: demo.test-suite" \
  -H "X-FlakePilot-Commit-Sha: $BASELINE_SHA" \
  -H "X-FlakePilot-Ref: refs/heads/demo" \
  -H "X-FlakePilot-Environment: demo.local" \
  -H "X-FlakePilot-Started-At: 2026-10-23T12:00:00+00:00" \
  -H "X-FlakePilot-Finished-At: 2026-10-23T12:05:00+00:00" \
  --data-binary "@$BASELINE_FIXTURE")
echo "ingest summary: $BASELINE_RESPONSE"
BASELINE_RUN_ID=$(printf '%s' "$BASELINE_RESPONSE" | python3 -c 'import json,sys; print(json.load(sys.stdin)["run_id"])')

echo "   2/2: the flaky/failing run (same tests plus one retry, later demo commit)"
CURRENT_RESPONSE=$(curl -fsS -X POST "$BASE/ingest/junit" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/xml" \
  -H "X-FlakePilot-CI-Source: demo-fixture" \
  -H "X-FlakePilot-CI-Run-Id: demo-run-1" \
  -H "X-FlakePilot-CI-Attempt: 1" \
  -H "X-FlakePilot-Job-Name: demo.test-suite" \
  -H "X-FlakePilot-Commit-Sha: $DEMO_SHA" \
  -H "X-FlakePilot-Ref: refs/heads/demo" \
  -H "X-FlakePilot-Environment: demo.local" \
  -H "X-FlakePilot-Started-At: 2026-10-23T12:10:00+00:00" \
  -H "X-FlakePilot-Finished-At: 2026-10-23T12:15:00+00:00" \
  --data-binary "@$CURRENT_FIXTURE")
echo "ingest summary: $CURRENT_RESPONSE"
RUN_ID=$(printf '%s' "$CURRENT_RESPONSE" | python3 -c 'import json,sys; print(json.load(sys.stdin)["run_id"])')
echo "run: $RUN_ID"

echo "== letting the worker deliver the outbox rows to the audit log"
sleep 3

echo "== discovering the stored runs (list-runs; newest first, bounded page)"
RUNS_JSON="$OUTPUT_DIR/runs.json"
uv run python -m flakepilot.interface.composition_root list-runs \
  --org-id "$ORG_ID" --repo-id "$REPO_ID" --limit 2 --format json > "$RUNS_JSON"
cat "$RUNS_JSON"

echo "== checking the discovery against the ingest responses, then selecting the run"
# `evidence.py select` re-derives the selection from the actual listing output:
# the run to inspect is whatever this listing says it is, and the only accepted
# identifiers are the run IDs the ingest responses and the listing both carry.
python3 ops/demo/evidence.py select "$RUNS_JSON" "$RUN_ID" "$BASELINE_RUN_ID" \
  "$DEMO_SHA" "$BASELINE_SHA"

echo "== rendering the selected run: read-only JSON and HTML reports (operator CLI)"
REPORT_JSON="$OUTPUT_DIR/run-report.json"
uv run python -m flakepilot.interface.composition_root ci-run-report \
  --org-id "$ORG_ID" --repo-id "$REPO_ID" --run-id "$RUN_ID" --format json > "$REPORT_JSON"
uv run python -m flakepilot.interface.composition_root ci-run-report \
  --org-id "$ORG_ID" --repo-id "$REPO_ID" --run-id "$RUN_ID" --format html --out "$REPORT"

echo "== evidence narrative (cites the stored run IDs, chains, baseline and rule version)"
NARRATIVE="$OUTPUT_DIR/evidence-narrative.txt"
python3 ops/demo/evidence.py narrate "$REPORT_JSON" "$RUN_ID" "$BASELINE_RUN_ID" \
  "$BASELINE_SHA" "$ORG_ID" "$REPO_ID" > "$NARRATIVE"
cat "$NARRATIVE"

echo
echo "Demo complete. Outputs:"
echo "  HTML report (open it):  $REPORT"
echo "  discovery listing:      $RUNS_JSON"
echo "  JSON report:            $REPORT_JSON"
echo "  evidence narrative:     $NARRATIVE"
echo "All of it is demo data: hand-written fixtures, no repository code ever"
echo "executed, every revision status left 'unchecked'. Baseline ordering here"
echo "was an upload decision, not Git ancestry."
echo "This run also never enabled untrusted-code execution: FLAKEPILOT_EXECUTION_MODE stayed"
echo "at the default closed mode throughout, and reproduction runs stay"
echo "fail-closed (POST /orgs/{org}/reproductions answers inconclusive without"
echo "session sign-in, which needs GitHub App credentials this demo does not use)."

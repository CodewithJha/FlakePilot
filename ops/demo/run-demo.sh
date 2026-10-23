#!/bin/sh
# FlakePilot MVP demo: one complete shell run of the documented demo journey.
#
#   1. migrate the demo database, start the API and worker that already exist
#   2. bootstrap an org with the operator CLI (the command the README documents)
#   3. issue a CI upload token with the operator CLI (new in this MVP)
#   4. POST the demo JUnit fixture from ops/demo/ (clearly hand-written, no
#      repository code was ever executed to produce it)
#   5. render the stored run as a read-only HTML report with the operator CLI
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

API_LOG="${DEMO_API_LOG:-flakepilot-demo-api.log}"
WORKER_LOG="${DEMO_WORKER_LOG:-flakepilot-demo-worker.log}"
REPORT="${DEMO_REPORT:-flakepilot-demo-report.html}"
BASE="http://127.0.0.1:${FLAKEPILOT_PORT:-8000}"
DEMO_SHA="000102030405060708090a0b0c0d0e0f10111213"

# Ingestion stores the raw report body as a plaintext file under this
# directory (no retention job deletes them; encryption is not wired).
DEMO_ARTIFACTS="${FLAKEPILOT_ARTIFACT_DIR:-${TMPDIR:-/tmp}/flakepilot-demo-artifacts}"
mkdir -p "$DEMO_ARTIFACTS"
chmod 700 "$DEMO_ARTIFACTS"
export FLAKEPILOT_ARTIFACT_DIR="$DEMO_ARTIFACTS"

cd "$(dirname "$0")/../.." || exit 1

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
echo "repo: $REPO_ID (a demo-scoped identifier; no repository was registered at a code host)"

echo "== issuing a CI upload token (shown once; only its SHA-256 is stored)"
TOKEN=$(uv run python -m flakepilot.interface.composition_root issue-ci-upload-token \
  --org-id "$ORG_ID" --repo-id "$REPO_ID" 2>/dev/null | tail -n 1)
echo "token prefix: ${TOKEN:0:12}..."

echo "== ingesting the demo JUnit fixture (POST /ingest/junit, the API's existing path)"
RESPONSE=$(curl -fsS -X POST "$BASE/ingest/junit" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/xml" \
  -H "X-FlakePilot-CI-Source: demo-fixture" \
  -H "X-FlakePilot-CI-Run-Id: demo-run-1" \
  -H "X-FlakePilot-CI-Attempt: 1" \
  -H "X-FlakePilot-Job-Name: demo.test-suite" \
  -H "X-FlakePilot-Commit-Sha: $DEMO_SHA" \
  -H "X-FlakePilot-Ref: refs/heads/demo" \
  -H "X-FlakePilot-Environment: demo.local" \
  -H "X-FlakePilot-Started-At: $(date -u +%Y-%m-%dT%H:%M:%S+00:00)" \
  -H "X-FlakePilot-Finished-At: $(date -u +%Y-%m-%dT%H:%M:%S+00:00)" \
  --data-binary @ops/demo/fixture-junit.xml)
RUN_ID=$(printf '%s' "$RESPONSE" | python3 -c 'import json,sys; print(json.load(sys.stdin)["run_id"])')
echo "run: $RUN_ID"
echo "ingest summary: $RESPONSE"

echo "== letting the worker deliver the outbox rows to the audit log"
sleep 3

echo "== rendering the stored run (read-only operator report)"
uv run python -m flakepilot.interface.composition_root ci-run-report \
  --org-id "$ORG_ID" --repo-id "$REPO_ID" --run-id "$RUN_ID" --out "$REPORT"
echo
echo "Demo complete. Open the report:"
echo "  open $REPORT"
echo "This demo never executed repository code: FLAKEPILOT_EXECUTION_MODE stayed"
echo "at the default closed mode throughout, and reproduction runs stay"
echo "fail-closed (POST /orgs/{org}/reproductions answers inconclusive without"
echo "session sign-in, which needs GitHub App credentials this demo does not use)."

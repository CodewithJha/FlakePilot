# FlakePilot — local-only release candidate

> **2026-10-25 update — the deployment boundary below has moved.** The owner
> approved Render hosting and made the D-HOST decisions, and since
> 2026-10-25 the **control plane** (API, worker, PostgreSQL 16 — not
> execution) runs on Render at `https://app.flakepilot.tech`; see
> [DEPLOYMENT-RUNBOOK](DEPLOYMENT-RUNBOOK.md) for the deployed state and
> smoke record, and the current sponsor evidence in
> [SPONSOR-IMPLEMENTATION-MAP](SPONSOR-IMPLEMENTATION-MAP.md). Everything
> this page says about execution posture is unchanged there: the worker runs
> `ClosedSandbox` / `UnavailableIsolation` / `control_plane_reaper()` /
> `RefusedHostHandoff` and no repository code executes in any configuration.
> The sections below describe the release candidate **as closed at
> `395982a`** (verified at `2508bd4`); they are kept as the boundary record
> that the deployment upgraded from, not re-edited. The upgrade criteria
> this page once listed for "hosted deployment" are now partially met and
> recorded in the runbook instead.

The release statement for what the evidence supports today: a **local-only
release candidate**. Its code evidence is taken from commit `2508bd4` (CI
run `40069275296` success there), and every command and test total below was
re-run at closure from a tree that differs from it only in this document;
the closing commit changes only documentation. This document states the
boundary, not more
than the boundary: every claim in it is backed by an implemented code path,
a test, or a command you can run on your machine. It replaces no existing
artifact — the operational day-to-day stays in the
[README](../../README.md), product scope in
[PRODUCT-SCOPE](../product/PRODUCT-SCOPE.md), and the still-open production
register in [RELEASE-READINESS-REGISTER](RELEASE-READINESS-REGISTER.md).

## What this release is

A reproducible single-host (developer laptop) application you run yourself:
FastAPI + PostgreSQL backend, an operator CLI (`python -m
flakepilot.interface.composition_root`), one operator demo journey, and no
deployment beyond that. Untrusted repository code is never executed in any
configuration the composition root produces: `_sandbox` returns
`ClosedSandbox`, the worker uses `control_plane_reaper()` +
`RefusedHostHandoff`, and `UnavailableIsolation` is the only production
`IsolationProvider` (every check false, ungoverned by environment variables).

## How to install and run it

Taken from the current [README](../../README.md); all commands were re-run
for this document at closure (`2508bd4`):

- Install: `uv sync --locked` (uv-managed CPython 3.12, see `.python-version`).
- Schema: `uv run alembic upgrade head` — migrations are an explicit step and
  never run at startup; `uv run alembic check` verifies models match
  migrations.
- Run the API: `uv run python -m flakepilot.interface.composition_root`
  (binds `127.0.0.1:8000` by default; exit code 3 on startup if PostgreSQL is
  unreachable — restart it or wait on the database; readiness clears itself
  when startup finishes recovering normal operation).
- Run the worker: `uv run python -m flakepilot.interface.composition_root
  worker` (stop with SIGINT/SIGTERM; it finishes its claimed batch; leases
  expire and other workers can re-claim their rows).
- Configuration: environment variables only, per [`.env.example`](../../.env.example);
  one invalid value stops startup naming the variable, never the value.
- The whole demo journey in one command (documented step-by-step in the
  README): `sh ops/demo/run-demo.sh` with a created database and
  `FLAKEPILOT_DATABASE_URL` exported.

## Tests and quality gates

```sh
uv lock --check && uv sync --locked
uv run pytest                                   # unit; +109 integration when
                                                # FLAKEPILOT_TEST_DATABASE_URL is set
uv run ruff format --check && uv run ruff check
uv run mypy
uv run lint-imports                             # 21 contracts
uv run pip-licenses --ignore-packages pathspec:1.1.1
UV_PROJECT_ENVIRONMENT=.venv-runtime uv sync --locked --no-dev
uv run pip-licenses --python .venv-runtime/bin/python
```

Verified at `2508bd4` (closure re-run): `pytest -q` → **742 passed, 109
skipped** (no test
database); with `FLAKEPILOT_TEST_DATABASE_URL` → **851 passed** (run twice);
CI run `40069275296` success. Integration tests create and drop throwaway
databases; the role needs `CREATEDB`. A green suite is not proof of
security, isolation, or encryption — the standing rule in
[AGENTS.md](../../AGENTS.md).

*Current counts, re-run at `ae2c6eb` (2026-10-25):* **1,237 passed, 160
skipped** without a test database; with one, **1,381 passed, 16 skipped** —
every skip is a credential-gated test in `tests/live/` (real providers,
each skipped with the name of the missing variable), not an omission. CI
run `40174033469` success.

## What the code paths implement (with access enforcement and tests)

- **Authentication** (`identity/application/iam.py`, ADR-011): GitHub App
  OAuth sign-in, opaque session cookie with `__Host-` prefix, HttpOnly,
  SameSite=Lax; state + PKCE bound per browser in a short-lived cookie;
  sessions rotated on sign-in and revoked on grant/membership/privilege
  changes, all audited in the same transaction.
- **Authorization / non-disclosure** (ADR-015/016): the org and repo in a URL
  are only lookup keys; access comes from IAM's records for the verified
  principal (`repo.view` required), with the code-host access verification on
  the session run views. Non-member, grant-less member, foreign-org resource,
  and unregistered repo are all the same 404 `not_found`. Fail-closed
  everywhere: no configured verifier → 404; not configured → 503.
- **Ingestion** (`POST /ingest/junit`, ADR-013): CI upload token (bearer,
  only the token's repo), metadata headers, ≤10 MiB body enforced by
  content-length pre-check and streaming truncation, idempotent duplicate
  handling (200 same key/content, 409 `idempotency_conflict`).
- **Classification** (`ti-classification/1`, ADR-013): deterministic,
  versioned rule set over the stored CI evidence; every verdict carries the
  evidence it came from (attempt chains, baseline `pass` in the same
  environment); revisions stay `unchecked` until real verification runs.
- **Run discovery and reporting**: session run views (`GET /orgs/{org}/runs/{run}`,
  `GET /orgs/{org}/repos/{repo}/runs`) and operator `list-runs` + `ci-run-report`
  (JSON or self-contained HTML); the pages carry
  `Content-Security-Policy: default-src 'none'; style-src 'unsafe-inline';
  frame-ancestors 'none'; base-uri 'none'` + `X-Frame-Options: DENY`, and
  every response carries `X-Content-Type-Options: nosniff` +
  `Referrer-Policy: no-referrer`.
- **Audit and outbox** (ADR-012): every mutation appends exactly one event in
  the same transaction; the relay claims rows with `FOR UPDATE SKIP LOCKED`
  and commits AUD's insert and `published_at` together; invalid envelopes are
  parked, never deleted (DURABLE-JOBS.md).

## What the demo fixtures demonstrate — and what they do not

`ops/demo/run-demo.sh` demonstrates the full journey with hand-written JUnit
fixtures (`fixture-junit-baseline.xml`, `fixture-junit.xml`): migration,
API + worker startup, org bootstrap, token issuance, two ingestions, run
discovery from real output (`list-runs`), JSON and HTML reports, and an
evidence narrative computed from the stored report. It demonstrates a flaky
verdict and a `deterministic_regression` verdict whose evidence is an earlier
stored run in the same environment.

It does **not** demonstrate: interactive sign-in (needs GitHub App
credentials; the session routes answer 503 `auth_not_configured` without
them), code-host verification (unbound demo repos: the view path stays
fail-closed), any execution of repository code (the default
`FLAKEPILOT_EXECUTION_MODE` stays `closed`; reproduction answers
`inconclusive` / `execution_closed`), or Git ancestry (baseline-before-current
is an upload choice, not ancestry proof). Nothing outside your machine is
touched.

## Available, unavailable, unverified

**Implemented and tested:** the paths in the previous two sections; clean
startup configuration validation; health probes (`/health/live` never checks
dependencies; `/health/ready` includes a bounded database check); request-ID
error contract; audit/outbox delivery with retry classing.

**Unavailable or disabled (by design, not defect):**
- Untrusted-code execution in production: disabled — no container in the
  default composition; evaluation mode is an explicit opt-in, is not a
  microVM, and does not change the production posture.
- Member/grant HTTP administration: operator CLI only (ADR-015); session
  granting is blocked pending decisions A/C of the grant-auth sheet.
- AI assist: `NullAiAssist` only (ADR-014); works with AI disabled (P6).
- Retention: policy is `disabled` only; nothing is ever deleted.
- Artifact encryption: plaintext `LocalFsArtifactStore`; `AesGcmSealer` and
  the sealed store exist, are unit-tested, but are not wired.

**Unverified (no evidence either way):**
- Live GitHub verification: *as of closure no App ID/private key existed and
  minting had never touched a provider.* Since then the owner created the
  production GitHub App and the deployment's sign-in and session-view
  verifier were **live-verified 2026-10-25** (per
  [AGENTS.md](../../AGENTS.md) and the runbook); the operator backfill's
  `actions:read` minting was live-checked 2026-10-25. `verify-github-app`
  itself has still not been run live — say so, do not claim it.
- Isolation: only `shared_kernel_container`-level evidence from
  non-eligible environments (laptop, hosted-runner probe). Not per-run
  microVM; satisfies no higher level.

## Operational limitations and failure behavior

- **No quotas or rate limits** on CPU, requests, or AI spend; body-size and
  upload-idempotency limits are the only enforced caps.
- **Parsing is synchronous** inside the upload request; no retention or stage
  job is registered in the worker.
- **TLS, request size and time limits are your job:** run it behind a
  reverse proxy; `FLAKEPILOT_HOST` defaults to the loopback IP and the
  app never binds 0.0.0.0 itself.
- **Startup failures are fail-closed:** an invalid settings value stops
  startup; PostgreSQL unreachable → exit 3; sign-in half-configured →
  startup failure; ingestion without `FLAKEPILOT_ARTIFACT_DIR` → 503
  `ingestion_not_configured`.
- **Recovery:** the worker is restart-safe (leases expire; claimed batches
  finish or their rows become re-claimable); outbox delivery is
  at-least-once with an idempotent consumer-side (the same event relaying
  again cannot double-insert). Lost artifacts or a destroyed database are
  unrecoverable — there is no backup/restore path in scope.

## The release boundary

**Local-only release candidate.** *(Historical, as closed at `395982a` —
see the 2026-10-25 update at the top of this page for what changed when the
owner approved Render hosting; execution posture unchanged.)* No hosted
deployment, no claim of live GitHub verification, no production-execution or
isolation claim of any kind. "Deployed" today only means: an operator runs
uvicorn on their own machine.

## What upgrading the claim would require

- **Hosted deployment**: the 10K owner decisions (hosting model, provider,
  region, sizing, budget — D-HOST-1..5), a deployment ADR, credentials, and
  a deployed, health-verified instance. See
  [RELEASE-READINESS-REGISTER](RELEASE-READINESS-REGISTER.md).
- **Live-verified GitHub path**: the owner creates the staging GitHub App
  (free, `Contents:read`, one staging repository), enrolls the credentials in
  a staging environment only, and a dated record shows the runbook's ordered
  checks passing against a contacted provider (Decision E in
  [GRANT-AUTHORIZATION-DECISION-SHEET](GRANT-AUTHORIZATION-DECISION-SHEET.md)).
- **Production-execution claim**: the full 10J chain — dedicated host
  (B1), selected per-run runtime (B2), measured host-side limits and cleanup
  (B3), trust-boundary verification (C1), independently reviewed evidence and
  a named reviewer's authorization (B4/B9) — plus external key management
  (B5), an approved retention policy (B6), and operations/monitoring (B8).
  Any single one of these missing is disqualifying, per
  [PRODUCTION-READINESS-ASSESSMENT](PRODUCTION-READINESS-ASSESSMENT.md).

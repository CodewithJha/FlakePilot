# Render deployment architecture

Companion to [ADR-017](../adr/ADR-017-sponsor-capability-phase.md) and sponsor
goal 10 (Render). Documentation of the deployment shape a Render-hosted
FlakePilot would take — two services, one database, an explicit migration
step, health checks, and an operator-owned artifact disk. **No deploy has
happened**: hosting is an owner decision behind the D-HOST gates
([INFRASTRUCTURE-DECISIONS](INFRASTRUCTURE-DECISIONS.md)), and every
provider-specific claim below is derived from the product's own verified
configuration surface, not from a live Render test. Nothing is purchased or
provisioned; the released local-only candidate posture is unchanged.

The deployable artifacts now exist in the repository root — `Dockerfile`,
`.dockerignore` and the `render.yaml` Blueprint — with the operator steps,
environment table, smoke tests and rollback in
[DEPLOYMENT-RUNBOOK](DEPLOYMENT-RUNBOOK.md). The image build itself is
**NOT VERIFIED** (no Docker daemon was available when they were written).

## The shape: two services, one system of record

```
                    ┌────────────────────┐
  browsers / CI ──▶ │  Render service A  │  uvicorn (API)
                    │  /health/* probes  │
                    └─────────┬──────────┘
                              │
                              ▼
                    ┌────────────────────┐
                    │ Render PostgreSQL  │   ← the single system of record
                    └─────────▲──────────┘
                              │
                    ┌─────────┴──────────┐
                    │  Render service B  │  worker (`... worker`)
                    │  poll / lease /    │   SIGTERM = graceful stop
                    │  outbox delivery   │
                    └────────────────────┘
```

- **Service A — the HTTP API.** Start command (inside the image, venv on
  `PATH`) `python -m flakepilot.interface.composition_root` (the module runs
  uvicorn per `__main__.py`). Serves `/health/live` and `/health/ready`
  (below), auth, ingestion, run views. Holds every secret: database URL,
  GitHub App credentials.
- **Service B — the leased worker.** Start command
  `python -m flakepilot.interface.composition_root worker`. Shares the
  database URL and nothing else: no session secret, no GitHub App private
  key ([EXECUTION-HOST](EXECUTION-HOST.md)'s boundary discipline — the
  worker has the job and outbox keys of the database, not the control
  plane's secrets). Worker tuning is
  `FLAKEPILOT_WORKER_POLL_SECONDS` (2), `..._BATCH_SIZE` (10),
  `..._LEASE_SECONDS` (30); the API process ignores them.
- **One PostgreSQL.** Both services point at the same
  `FLAKEPILOT_DATABASE_URL`. Schemas and migrations are unchanged
  (`iam`, `ti`, `diag`, `orch`, `aud`, `repo`, `exec`, `int_secrets`); a
  Render PostgreSQL add-on is just a URL. No second store is introduced.

## Migrations are an explicit step, never startup work

The product never migrates on boot — a documented invariant
([README](../../README.md): "migrations are an explicit step"). On Render the
migration step is the API service's `preDeployCommand`, which runs on separate
compute after the build and before the new build starts (a failure stops the
deploy). Pre-deploy commands are available on paid instance types only. The
image keeps `pyproject.toml` in its working directory because Alembic reads
`[tool.alembic]` from it:

```
alembic upgrade head
```

Only the API runs it: two concurrent upgrades could race on `alembic_version`.

Startup failure on an unmigrated database is by design: the API refuses to
come up ("database unavailable at startup") rather than half-initialize.
Schema changes follow the release-candidate discipline — migrate first,
deploy second — so a rolling deploy meets a schema that accepts the old and
new code.

## Health checks

Verified behavior of `interface/http/health.py`:

| Probe | Path | Semantics |
|---|---|---|
| Liveness | `GET /health/live` | Process can serve HTTP. Never checks dependencies — a database outage must not trigger restarts. |
| Readiness | `GET /health/ready` | 200 only when lifespan startup completed, shutdown has not begun, and the injected readiness probe (a bounded database check) succeeds; 503 otherwise. |

Render mapping: the health-check path is **`/health/live`** (changed from an
earlier `/health/ready` recommendation). Render uses its single HTTP health
check both to gate deploys and to supervise running instances, and it
*restarts* an instance after 60 seconds of failed checks — so a readiness
check would turn a database outage into an API restart loop, which is exactly
what the liveness/readiness split exists to prevent. Deploys stay gated on
the database anyway: uvicorn runs lifespan startup (the bounded `SELECT 1`)
before it binds the port, so a new instance cannot pass even a liveness check
until the database has answered. `/health/ready` remains for operators and
smoke tests. Bodies carry only a status field.

## Graceful shutdown and the worker drain

- The worker installs handlers for `SIGINT`/`SIGTERM` that set a stop flag
  (`worker.py`); the poll loop finishes its unit of work and exits between
  ticks. Render's default SIGTERM-then-kill shutdown therefore drains the
  worker without orphaning work.
- Correctness does not depend on the drain being clean: a job claimed when
  the process dies is retried after the lease expires (default 30 s),
  because claiming is leased and side effects ride the transactional outbox
  ([DURABLE-JOBS](DURABLE-JOBS.md), "Claiming, leases, and crashes",
  "Outbox delivery"). The failure mode of an abrupt stop is *delayed* work,
  never lost work — this is exercised by the jobs/worker PostgreSQL tests.
- Set Render's shutdown grace period above the worst expected tick
  (poll interval + batch work); with defaults, tens of seconds is ample.
  `render.yaml` sets `maxShutdownDelaySeconds: 60` on the worker. The API
  keeps Render's default (30 s): a service with a disk cannot change it.

## The artifact directory is an operator responsibility

`FLAKEPILOT_ARTIFACT_DIR` must be a **persistent** path: Render's default
filesystem is ephemeral and would silently discard raw reports on redeploy.
If ingestion is enabled, attach a Render Disk and mount it at the artifact
directory. The product's documented facts apply unchanged — artifacts are
plaintext files, not encrypted at rest, and never cleaned up
automatically; retention is a human, product/legal decision made outside
this repository ([AGENTS invariants](../../AGENTS.md);
[PRODUCTION-READINESS-ASSESSMENT](PRODUCTION-READINESS-ASSESSMENT.md)
"Artifacts are not encrypted at rest, and never cleaned up"). The disk is
The directory must be owned by the service user, not group- or
world-writable (settings validation enforces the checks it can see).

Correction from vendor documentation: a Render disk is reachable by **one**
service instance at runtime only — never by another service, the build, the
pre-deploy command, or one-off jobs — and it pins its service to one instance
without zero-downtime deploys. The worker and API therefore cannot share it.
The disk goes on the API, the only writer of artifacts today (JUnit
ingestion); in the closed execution posture the worker stores no evidence and
gets no artifact directory. Opening execution would need shared storage that
a Render disk cannot provide — an owner/ADR decision
([DEPLOYMENT-RUNBOOK](DEPLOYMENT-RUNBOOK.md), "The artifact disk, honestly").

## Secrets and environment

All configuration is environment-only (`Settings.from_environ`); nothing is
read from files or baked into images. On Render, every variable goes in the
service environment (encrypted at rest by the platform), including:

- `FLAKEPILOT_DATABASE_URL` (both services),
- `FLAKEPILOT_PUBLIC_ORIGIN` + GitHub App variables (API service; sign-in
  answers 503 while unset),
- the advisory-AI variables `FLAKEPILOT_AI_ENDPOINT`, `FLAKEPILOT_AI_MODEL`,
  `FLAKEPILOT_AI_API_KEY` — all optional; unset keeps the `NullAiAssist`
  no-provider posture (ADR-017 decision 3),
- `FLAKEPILOT_ARTIFACT_DIR` (API only, on the disk mount),
- `FLAKEPILOT_SENTRY_DSN` (both; optional, opt-in error reporting per
  [SENTRY-OBSERVABILITY-BOUNDARY](SENTRY-OBSERVABILITY-BOUNDARY.md)),
- `FLAKEPILOT_AUTH0_DOMAIN` + `FLAKEPILOT_AUTH0_AUDIENCE` (API; optional,
  parsed but not yet used by any route).

The full table, including placeholder names for the provider keys, is in
[DEPLOYMENT-RUNBOOK](DEPLOYMENT-RUNBOOK.md).

Values marked secret in [README](../../README.md)'s table are never logged
(`Secret` redaction), and `FLAKEPILOT_ALLOW_INSECURE_COOKIES` stays `false`
— Render serves TLS, so the `__Host-` cookie prefix and `Secure` flag stay
on (the next document's subject too).

## Smoke checklist (owner-run, only if hosting is ever approved)

The expanded, command-level version is in
[DEPLOYMENT-RUNBOOK](DEPLOYMENT-RUNBOOK.md); this list is its summary.

1. Both services start; `curl /health/live` and `/health/ready` answer.
2. Migrations ran once (`alembic upgrade head`), then `alembic check` agrees.
3. Sign-in round-trips against the GitHub App callback registered for the
   chosen origin (see [DOT-TECH-DOMAIN-STRATEGY](DOT-TECH-DOMAIN-STRATEGY.md)).
4. A CI upload lands: artifact file appears on the disk mount, run appears
   in `list-runs`.
5. Kill the worker service mid-batch: the claimed job completes after lease
   expiry on the replacement process (delayed, not lost).
6. The execution posture reads back unchanged: `FLAKEPILOT_EXECUTION_MODE`
   is `closed`, no repository code executed anywhere.

## What this document does not claim

- **No deploy, no spend, no account.** D-HOST-5's zero-budget constraint
  stands until the owner changes it; D-HOST-3's region question is
  unanswered and blocks provisioning; D-HOST-1/2 make hosting a recorded
  preference, not a decision.
- **No Render-specific behavior is verified.** Product behavior above is
  verified in source; Render platform behavior (disks, TLS, shutdown
  timing) is the vendor's documentation speaking, not independent proof.
- **No isolation claim.** A Render service runs `ClosedSandbox` — execution
  stays disabled; nothing here changes the evidence ladder or the
  fail-closed inventory ([EXECUTION](EXECUTION.md)).

Status: **architecture documented, Blueprint and image written (not built,
not deployed), deployment owner-gated**.
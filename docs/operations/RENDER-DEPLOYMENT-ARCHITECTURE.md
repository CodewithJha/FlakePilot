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

- **Service A — the HTTP API.** Start command
  `uv run python -m flakepilot.interface.composition_root` (the module runs
  uvicorn per `__main__.py`). Serves `/health/live` and `/health/ready`
  (below), auth, ingestion, run views. Holds every secret: database URL,
  GitHub App credentials.
- **Service B — the leased worker.** Start command
  `uv run python -m flakepilot.interface.composition_root worker`. Shares the
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
migration step is a one-off command run before (or alongside) the first
service start, e.g. from a shell/job context that has the database URL:

```
uv sync --locked && uv run alembic upgrade head
```

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

Render mapping: the health-check path is `/health/ready` (it implies a
live process); `/health/live` exists for orchestrators that separate the
concerns. Bodies carry only a status field.

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
also the one shared-mount consideration: the worker and API must agree on
the mount point, and the directory must be owned by the service user,
not group- or world-writable (settings validation enforces the checks it
can see).

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
- `FLAKEPILOT_ARTIFACT_DIR` (both services, at the disk mount).

Values marked secret in [README](../../README.md)'s table are never logged
(`Secret` redaction), and `FLAKEPILOT_ALLOW_INSECURE_COOKIES` stays `false`
— Render serves TLS, so the `__Host-` cookie prefix and `Secure` flag stay
on (the next document's subject too).

## Smoke checklist (owner-run, only if hosting is ever approved)

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

Status: **architecture documented, deployment owner-gated**.
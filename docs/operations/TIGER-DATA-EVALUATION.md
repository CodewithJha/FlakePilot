# Tiger Data (Tiger Cloud) evaluation

Companion to [ADR-017](../adr/ADR-017-sponsor-capability-phase.md) decision 5
and sponsor goal 6 (Tiger Data). Nothing is provisioned, no account exists,
and no connection to Tiger Cloud has been made. PostgreSQL stays the single
system of record ([ADR-005](../adr/ADR-005-system-of-record-database.md)).

## The key fact

Tiger Data (formerly Timescale) sells **Tiger Cloud**: managed PostgreSQL with
the TimescaleDB extension (hypertables, continuous aggregates, tiered
storage, HA and read replicas) — vendor docs:
<https://www.tigerdata.com/docs/docs/learn/tiger-cloud>. Because it *is*
PostgreSQL, FlakePilot would run on it with **zero adapter code**: the same
`FLAKEPILOT_DATABASE_URL`, the same per-context schemas and migrations. So the
goal splits into two separate questions: what time-series analytics the
product needs (answered in code below, on plain PostgreSQL), and who hosts the
primary database (an owner decision).

## What was built (plain PostgreSQL, works on Tiger Cloud unchanged)

| Capability | Where | Notes |
|---|---|---|
| Day/week failure and flaky trends | `TiStore.failure_trend` (`test_intelligence/infrastructure/store.py`) | Plain `date_trunc` over `timezone('UTC', received_at)` — no extension. One cell per (UTC bucket, environment): runs, test runs, failed test runs, flaky test runs. CI-upload runs only (FlakePilot's own sandbox reruns would bias the rates) |
| Regression vs previous window | `Reliability.trend` + `compare_windows` (`application/reliability.py`) | Reads `(end − window, end]` and the window before it; the failure and flaky directions are exact rate comparisons by cross-multiplication (`regressed`/`improved`/`unchanged`/`no_data`/`incomplete`). Arithmetic, not a significance test |
| Environment-specific reliability | `ReliabilityReport.environments` and `TrendReport.environments` | Per (test, environment) runs/failed/flaky, and per-environment window totals |
| Repeated-failure clusters | `ReliabilityReport.clusters` | Tests whose failing runs are exactly the same set of two or more runs — a shared-cause candidate, never a diagnosis |
| Existing per-test history | `Reliability.report`, `TiStore.repo_history` | Unchanged |

Bounds and scope: every query filters by `(org_id, repo_id)`; a window is
longer than zero and at most 366 days; each window read returns at most
`max_cells` (default 1000) cells and the report says `truncated` (directions
become `incomplete`) rather than comparing partial totals.

Tests: unit tests for composition, directions, bounds, truncation, and scope
pass-through (`tests/unit/test_intelligence/application/test_reliability.py`);
a PostgreSQL integration test over real `ti` rows
(`tests/integration/test_reliability_postgres.py::test_failure_trend_buckets_and_compares_real_rows`)
— written, **NOT VERIFIED** in this session because no PostgreSQL was
available here; CI runs it.

Not wired: no HTTP route or operator command exposes trends yet (a
composition-root change, outside this work).

## Should Tiger Cloud host the primary database?

**Owner decision; no technical blocker either way.** The code is identical on
Render PostgreSQL (the default in
[RENDER-DEPLOYMENT-ARCHITECTURE](RENDER-DEPLOYMENT-ARCHITECTURE.md)) and on
Tiger Cloud.

| Factor | Render PostgreSQL | Tiger Cloud |
|---|---|---|
| Network path | Same provider as the API/worker | Cross-provider hop from Render; region must be matched by hand |
| Accounts/billing | One vendor | A second vendor account and invoice |
| Time-series extras | None needed today | Hypertables, continuous aggregates, tiered storage available later |
| Published price (vendor page, read 2026-10-24) | Not re-verified here | Performance plan from $30/month compute + $0.177/GB-month storage; Scale from $36/month + $0.212/GB-month; HA replicas, I/O boost, tiered storage are extra; 30-day trial ([pricing](https://www.tigerdata.com/pricing)) |

Recommendation: keep the database co-located with the application host
(Render PostgreSQL) unless the owner wants Tiger Cloud's operational features
or a hypertable trigger below fires. Before choosing Tiger Cloud, verify on a
throwaway service (never the integration-test URL against real data): the
PostgreSQL major version against CI's 16.x, TLS with asyncpg, the role
privileges the migrations need, and connection limits for API + worker.

## What would justify hypertables or continuous aggregates

Not now: there is no load evidence, and converting the current schema is a
redesign, not a flag. TimescaleDB requires every unique index on a hypertable
to include the time partitioning column, does not support foreign keys between
two hypertables, and needs a non-null time column
([limitations](https://github.com/timescale/docs/blob/latest/use-timescale/limitations.md)).
`ti.test_results` has no time column (receipt time lives on `ti.runs`), its
primary key is `(run_id, test_id, attempt)`, and it references `ti.runs` by
`(org_id, repo_id, id)` — so a hypertable would mean denormalizing
`received_at` into results and reworking keys and tenant foreign keys.

Revisit triggers, in order (each needs written evidence):

1. **Measured trend latency above an agreed objective** after the plain fix:
   a btree index on `ti.runs (org_id, repo_id, received_at)` (one migration;
   not added now because nothing measured needs it).
2. **A measured plan** showing the trend or history scans dominate and no
   index or query change fixes them at real table sizes.
3. **A hot read path** (a dashboard route polling trends) where a continuous
   aggregate would replace repeated scans — no such route exists.
4. **A human-approved retention decision** for `ti` history: chunk dropping
   is retention, which this repository never decides on its own
   (AGENTS.md security invariants).
5. **The owner selects Tiger Cloud** as the host (hypertables are only
   available with the extension).

Status: trend analytics **tested with mocks**, with a PostgreSQL integration
test written — it becomes **Integration-tested** only when CI passes it; Tiger
Cloud hosting: **owner decision, not selected**; hypertables: **not
justified**.

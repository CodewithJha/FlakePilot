# Tiger Data (Tiger Cloud) evaluation

Companion to [ADR-017](../adr/ADR-017-sponsor-capability-phase.md) decision 5
and sponsor goal 6 (Tiger Data). PostgreSQL stays the single system of record
([ADR-005](../adr/ADR-005-system-of-record-database.md)). The owner created
one empty Tiger Cloud service for FlakePilot; the evidence from it is below.
Nothing is deployed there.

## Evidence on a real Tiger Cloud service (2026-10-25)

Run from the developer machine against the owner's new, empty service. The
URL came only from the environment and was never printed or logged.

| Check | Result |
|---|---|
| Server | PostgreSQL **18.6** with TimescaleDB 2.31.0. CI and local tests use 16.x, so the major version differs |
| TLS | On (`pg_stat_ssl.ssl = true`) |
| URL shape | The service URL carries `?sslmode=require`. asyncpg refuses `sslmode` itself, so `create_engine` now moves a libpq `sslmode` into asyncpg's `ssl` argument with the same mode (commit `280c3bb`; unknown modes are refused). **VERIFIED** 2026-10-25: the unmodified service URL connected through `create_engine` with `pg_stat_ssl.ssl = true`. libpq's `require` encrypts without verifying the server certificate; `sslmode=verify-full` is accepted too |
| Role | `tsdbadmin`: not a superuser (`rolcreatedb` and `rolcreaterole` are true). `CREATE DATABASE` for any name but `tsdb` is refused (`tsdb_admin: database … is not an allowed database name`, hint `tsdb_admin.allowed_databases`) |
| `tsdb_admin.allowed_databases` | Value `tsdb`; `pg_settings.context` is **`superuser`**. `SET` by `tsdbadmin` answers `permission denied to set parameter`, and `has_parameter_privilege` is false for both `SET` and `ALTER SYSTEM`. The owner's admin role therefore cannot allow another database; only Tiger (support, or a superuser-level control we do not have) can |
| `alembic upgrade head` | Passed, from empty to `iam_0003`, including `ti_0002`. All eight context schemas were created in database `tsdb` |
| `alembic check` | `No new upgrade operations detected` |
| `reliability-trend` | Ran through the operator CLI for an empty (org, repo) pair: `no_data`, `truncated: false`. This shows the `date_trunc`/`timezone` SQL runs there; it measures no trend |
| Full integration suite | **BLOCKED**. All 135 integration tests error in setup, because each creates a throwaway database next to the URL, and the service refuses `CREATE DATABASE`. No test ran, and no test database was created |

What the run left behind: database `tsdb` holds FlakePilot's schemas at
`iam_0003` (25 tables, including `alembic_version`) and no rows. No other
database exists (`postgres`, `tsadmin` and `tsdb` only). That `iam_0003` was
applied from the then-uncommitted migrations; the committed chain now adds
`ti_0003` (a check constraint reserving the backfill CI source), so `alembic
upgrade head` there would add one constraint. It was not run, and no schema
was dropped.

**Full integration suite on Tiger: BLOCKED (2026-10-25, re-checked).** The
suite creates a throwaway database per test or module, and the service
allows only `tsdb`. `tsdb_admin.allowed_databases` is a superuser-context
setting that the owner's `tsdbadmin` role cannot change (see the table).
Unblocking needs either Tiger to allow extra database names on the service,
or a harness mode that runs inside one database (a change to
`tests/support.py`, not made).

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
— passes on PostgreSQL 16 locally and in CI.

Wired as the `reliability-trend` operator command (JSON); there is no HTTP
route for trends.

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
or a hypertable trigger below fires. The 2026-10-25 run (above) settled three
of the pre-checks: the major version is 18, not 16; TLS works with
`ssl=require` but not with `sslmode`; and the migrations need no more than
`tsdbadmin`. Still unchecked: connection limits for the API and the worker,
and the full suite on PostgreSQL 18.

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

Status: trend analytics unit-tested and real-PG-tested on PostgreSQL 16
(locally and in CI). Trends now read CI time, so a backfilled run counts on
its original day. On Tiger Cloud, migrations and `alembic check` were
**VERIFIED** (PostgreSQL 18.6); the integration suite is **BLOCKED** (no
`CREATE DATABASE`). Tiger Cloud hosting remains an **owner decision, not
selected**; hypertables: **not justified**.

## Independent re-verification (2026-10-25, later the same day)

Run from the developer machine against the same owner service (`flakepilot`,
`ngdaky3jfu`); the Tiger CLI was the only new tooling. The connection URL
came from the owner's keyring into a gitignored `.env` and was never printed.

| Check | Result |
|---|---|
| Connection facts (read-only) | PostgreSQL **18.6** + TimescaleDB **2.31.0**, TLS 1.3, `max_connections` 105, `tsdbadmin` not superuser, database `tsdb` — matches the earlier evidence |
| Stale schema closed | The service sat at `iam_0003` (as documented above). `alembic upgrade head` applied the two additive pending migrations — `ti_0003` (the backfill-CI-source check constraint) and `ops_0001` (`ops.verification_runs`) — leaving `alembic current` = `ops_0001` and `alembic check` clean. Non-destructive; the service held no rows |
| **Trend on real rows: PostgreSQL 18/TimescaleDB ≡ PostgreSQL 16** | A tagged, synthetic `(org, repo)` pair seeded **4 runs through `SqlTiStore`'s real write path** (`ensure_identities` → `claim_upload` → `create_run` → `add_results`, a receipt-time update placing runs on three consecutive days, one run flaky: FAILED→PASSED). `Reliability.trend` on the service returned **byte-identical** output (buckets, per-environment totals, `failure_direction`/`flaky_direction` = `improved`/`improved`, `truncated: false`) to the same seed computed on PostgreSQL 16 locally. The shipped operator CLI (`python -m flakepilot.interface.composition_root reliability-trend …`) then returned exit 0 with the same numbers on the service |
| Reversibility | Every probe row deleted by `(org_id, repo_id)`; all five touched `ti` tables (`runs`, `uploads`, `test_results`, `test_identities`, `outbox`) verified back to zero for the probe pair |
| Unchanged | The full integration suite on the service stays **blocked** (`tsdb_admin.allowed_databases`; no `CREATE DATABASE` — workaround options unchanged). Turf: hypertables/continuous aggregates stay **not justified**; Tiger Cloud hosting stays an owner decision, still not made. Production remains Render PostgreSQL |

Conclusion: the last technical pre-decision check ("does the analytics SQL
compute real trends correctly on the service, not just run?") is now settled
with a positive, reproducible answer. What separates Tiger Cloud from being
production's database is purely the owner's hosting decision.

# Temporal durable workflow evaluation

Companion to [ADR-017](../adr/ADR-017-sponsor-capability-phase.md) decision 1
and sponsor goal 5 (Temporal). The durable workflow authority stays the
repository's own outbox and jobs
([DURABLE-JOBS](DURABLE-JOBS.md), [ADR-006](../adr/ADR-006-durable-jobs-in-the-database.md),
[ADR-012](../adr/ADR-012-event-envelope-and-outbox.md)).

## Decision update (2026-10-25): implemented as an opt-in proof of concept

The owner approved a bounded Temporal role that does not compete with the
outbox/jobs authority: Temporal schedules, retries and cancels **one**
investigation, while FlakePilot's lock and outbox stay authoritative. It is
never wired into the default worker. The SDK first failed the runtime
licence gate (`protobuf` reports `3-Clause BSD License`, not the gate's
exact spelling); the owner added that alias to `allow-only` (2026-10-25) and
`temporalio` 1.34.0 was added.

What is implemented (commit `3872cd2`):

- **Workflow** (`composition_root/temporal_workflow.py`, free of FlakePilot
  imports). `InvestigationWorkflow` with workflow ID `investigation-{key}`
  and reuse policy *reject duplicate*. It runs one activity with a
  start-to-close timeout of 120 s and a heartbeat timeout of 30 s (2-30 s,
  carried in the input). Retry: at most 3 attempts; only
  `database_unavailable` and `investigation_in_progress` retry, and
  `invalid_request`, `not_found`, `unauthorized` and `investigation_failed`
  are final. Cancelling the workflow cancels the activity (delivered through
  its heartbeat) and waits for it to stop.
- **Activity** (`composition_root/temporal_investigation.py`). It takes a
  session-level `pg_try_advisory_lock` on the key, on its own autocommit
  connection that is invalidated when the activity ends, so a killed
  worker's lock goes with its connection. A held lock is
  `investigation_in_progress` (retryable). It then returns the recorded
  outcome, without running anything, if `diag.outbox` has the
  `diag.investigation.recorded` row whose correlation ID is the key.
  Otherwise it runs the existing `Investigations.investigate` as an
  explicitly named org member (IAM resolves the access, REPO's verifier the
  code-host standing), with the key as the `Origin` correlation ID, and
  heartbeats meanwhile. An advisory-model call made before a kill may be
  repeated (at-least-once); its request row is recorded each time.
- **Commands** (operator only): `temporal-worker`,
  `temporal-run-investigation --org-id --run-id --member-github-id [--key]`,
  `temporal-cancel-investigation --key`. `FLAKEPILOT_TEMPORAL_ADDRESS` and
  `_NAMESPACE` go together; `_API_KEY` (Temporal Cloud) turns TLS on and is
  refused without an address. The API process and the default `worker`
  never import Temporal (a unit test checks the loaded modules). Temporal's
  history holds IDs, enum codes and a stage count only.

Evidence (2026-10-25):

- **VERIFIED, CI:** `tests/integration/test_temporal_activity_postgres.py`
  runs the activity through `temporalio.testing.ActivityEnvironment` on real
  PostgreSQL around the real investigation of a seeded run: one key runs once
  and a repeat replays the recorded outcome; a held lock is retryable and
  every run releases its lock; failures map to the reason codes without
  leaking text; a malformed request is final.
- **VERIFIED, real Temporal server (local dev), not Temporal Cloud:**
  `tests/live/test_live_temporal_local.py` with
  `WorkflowEnvironment.start_local()` (Temporal CLI 1.9.1, Server 1.32.0,
  downloaded at test time; CI skips it). One workflow recorded exactly one
  investigation and a duplicate start was rejected. A cancelled workflow
  failed as `cancelled`, recorded nothing, and left no advisory lock. A
  worker process holding the activity (and the key's lock) was SIGKILLed;
  a replacement worker ran attempt 2 and exactly one
  `diag.investigation.recorded` row exists.
- **Stand-in, not evidence:** the code-host standing in these tests comes
  from a `verified_read` stand-in.
- **BLOCKED:** Temporal Cloud. The trial requires a payment card, so no
  `FLAKEPILOT_TEMPORAL_*` Cloud credentials exist and nothing ran against
  Cloud. Nothing is deployed.

## Temporal Cloud access re-check (2026-10-25, sprint prompt)

Re-checked against temporal.io the same day, at the owner's request to
investigate a free/no-cost path without adding any payment method:

- Self-serve signup asks for a credit card (the pricing FAQ's own
  "Why is a credit card required?" entry confirms it is standard).
- The promotional no-card paths are closed to this owner as of today: the
  AWS Marketplace pay-as-you-go trial (3 months, USD 1,000 credits, no card)
  required signup by a past deadline and an AWS account; the startup-credits
  program (USD 2,400) is an application, not self-serve.
- No card may be added and no cloud marketplace account may be used without
  an explicit owner decision, per the sprint's cost policy.

**Decision: stays blocked by billing/owner decision** (`blocked_by_billing`).
The code path is Cloud-ready and needs no change: `Client.connect` already
takes `FLAKEPILOT_TEMPORAL_ADDRESS`/`_NAMESPACE`/`_API_KEY` (api key turns TLS
on), accepted for any host:port. For the owner's future run,
`tests/live/test_live_temporal_cloud.py` (added 2026-10-25, **NOT VERIFIED** —
never executed) exercises the real workflow and activity against a real Cloud
namespace: one recorded investigation with a rejected duplicate, and a
cancelled workflow that records nothing. Until then the outbox/jobs authority
stays the production path, exactly as ADR-017 decision 1 approved.

## Earlier evaluation (2026-10-24, superseded as to the decision)

## What the goal asked

"Sponsored durable workflow: a durable investigation workflow with Triton
ML/LLM calls gated by investigation maturity, priority, AI policy, and
sponsorship tiers. Persist the audit record. Replayable. Restartable." —
expressed as the sponsor goal in
[SPONSOR-INTEGRATION-MATRIX](SPONSOR-INTEGRATION-MATRIX.md).

## What the product already does (the authority's evidence)

- **Durable, restartable work.** Jobs are database rows with states, leases,
  and dead-lettering; the worker process is replaceable at any time and a
  crash after claim is a lease expiry, not lost work (DURABLE-JOBS,
  "Claiming, leases, and crashes"; exercised by
  `tests/integration/test_jobs_postgres.py`, `test_worker_postgres.py`).
  The lease is renewed while a handler runs, and a stale owner can neither
  acknowledge nor renew (`test_handler_runs_once_and_renews_its_lease`,
  `test_stale_owner_cannot_ack_or_renew`). Claims use
  `FOR UPDATE SKIP LOCKED`, so several worker processes can already share the
  queue safely; the documented deployment runs one worker service
  ([RENDER-DEPLOYMENT-ARCHITECTURE](RENDER-DEPLOYMENT-ARCHITECTURE.md)).
- **Transactional side effects.** The outbox writes domain events in the same
  transaction as the state change they describe; delivery completes later —
  at-least-once, dedupable by event ID (`test_outbox_postgres.py`).
- **The investigation workflow itself** (ADR-017 decision 1, goal 2) is a
  bounded, typed, in-process stage pipeline: collect → research → hypothesize
  → prioritize, then one provenance event. Longest call is one bounded
  provider request; total runtime is seconds, not hours. Its durable outcome
  is the provenance event in `diag.outbox` (goal 15), not an executing
  process. It is a library use case today: nothing in the composition root
  runs it or enqueues it as a job (re-checked at `3b93cb6`).

## What Temporal would add, and why it is not justified now

| Temporal provides | The product's need | Verdict |
|---|---|---|
| Durable state machines surviving process death | Bounded second-scale stages; durability is already database rows | Covered by outbox/jobs |
| Timers, signals, human-in-the-loop waits | Every stage is either immediate or a single called provider; no multi-day waits, no signals | Not needed |
| Scalable parallel task scheduling | `SKIP LOCKED` claims already allow several workers; one runs today | Not needed |
| Retry policies and timeouts per activity | Stage budgets and fail-closed skips are code-owned and tested | Covered |
| Replayable workflow history | Provenance is event-envelope rows, never rewritten (goal 15); audit is derivable per the AUD rule | Covered differently |

The two substantive costs of adopting Temporal:

1. **A second workflow authority.** Jobs/outbox and Temporal would both own
   idempotency, retry, and dedup semantics for overlapping work. One of them
   must be authoritative, and every correctness argument would then need the
   interaction rules between them — a permanent architectural tax justified
   only by capabilities the product lacks. It has none for the current scope.
2. **A new trust boundary and system of record.** Temporal requires its own
   (or a third-party-hosted) durable store, an SDK dependency (Decision 10's
   vendor gate: terms, data flow, licence purity, transmitted-field
   verification), and worker-fleet operations outside the single-PostgreSQL
   posture ADR-005 records.

## Conditions to revisit

Any one of these, with written evidence, reopens the evaluation:

1. A product-controlled requirement for **long-running (hours-to-days)
   durable workflows** where the intermediate state cannot be expressed as
   existing tables + outbox events (e.g. multi-phase verification with human
   waits across restarts). Wiring the investigation as a job does not by
   itself qualify: a seconds-long job is what the jobs table already does.
2. A measured outbox/jobs SLA miss the current authority cannot correct
   (throughput, fairness, or retry semantics — measured, not anticipated).
3. A multi-worker execution-host fleet where per-host scheduling races make
   database-lease claims insufficient on real evidence.
4. Owner authorization of the vendor dependency per Decision 10's gate list —
   a precondition for any adoption in every branch.

Condition 4 (owner authorization of the vendor dependency) now holds; see the
decision update at the top. The outbox/jobs authority stays regardless.
Status: **implemented as an opt-in proof of concept**, real-PG-tested in CI,
verified against a real local Temporal dev server; Temporal Cloud
**BLOCKED** (no credentials); not deployed.

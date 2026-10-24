# Temporal durable workflow evaluation

Companion to [ADR-017](../adr/ADR-017-sponsor-capability-phase.md) decision 1
and sponsor goal 5 (Temporal). Read-only evaluation; nothing is installed,
purchased, or wired. The live product posture is unchanged: the durable
workflow authority is the repository's own outbox/jobs
([DURABLE-JOBS](DURABLE-JOBS.md), [ADR-006](../adr/ADR-006-durable-jobs-in-the-database.md),
[ADR-012](../adr/ADR-012-event-envelope-and-outbox.md)).

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

None hold today. Status: **not justified**; the outbox/jobs authority stays.

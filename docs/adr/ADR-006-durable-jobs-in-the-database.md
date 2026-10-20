# ADR-006: Durable jobs, timers, outbox dispatch, and coordination in the system-of-record database

| | |
|---|---|
| Status | **Accepted** by the project owner, 2026-10-20 (FQ-1). It picks one of the two options [SYSTEM-ARCHITECTURE §6](../architecture/SYSTEM-ARCHITECTURE.md#6-sync-vs-async-and-durable-execution) already says would meet the requirements. It depends on [ADR-005](ADR-005-system-of-record-database.md) (Accepted, PostgreSQL) |
| Related | [SYSTEM-ARCHITECTURE §5, §6, §10](../architecture/SYSTEM-ARCHITECTURE.md#5-cross-cutting-runtime-mechanisms), [ADR-001](ADR-001-modular-monolith.md), [ADR-004](ADR-004-control-plane-and-execution-plane.md) |

## Context

SYSTEM-ARCHITECTURE §6 requires durable execution for:
- The investigation lifecycle, which spans days.
- The verification phases.
- Application (writing the approved branch and PR).
- Timers: approval TTL, retention, and lease expiry.
- The outbox relay.

Requirements for any solution:
- Step state is durable before it is acknowledged.
- Steps run at least once and are idempotent.
- Timers are durable.
- Cancellation reaches running steps.
- Operators can see stuck work.
- No correctness depends on in-memory state.

§6 says that "a database-backed job table with leases or a dedicated workflow engine could both satisfy this". §5 adds leases, bounded retries, cancellation at step boundaries, and at-least-once event delivery deduplicated by event ID. Event dispatch (in-process vs broker) and the workflow runtime are both deferred (§10). ADR-001 allows several process roles from the same build.

## Decision

**Binding** (architecture level):
1. **Durable work is stored as job rows in the system-of-record database** (ADR-005):
   - Workers claim a job with a conditional update or row lock and hold a renewable lease.
   - Timers are jobs scheduled for a future time.
   - Cancellation is a flag that steps check at their boundaries (SYSTEM-ARCHITECTURE §5).
   - When a lease expires, the job is re-run idempotently up to a bounded count.
2. **Enqueuing a job is part of the same transaction as the state change that causes it.** For example, ORCH's state transition and its stage-job row commit together. That avoids the dual-write problem.
3. **Outbox relay**: a worker reads committed outbox rows in order and delivers them **in-process** to subscribers allowed by the matrix, plus AUD. Subscribers dedupe by event ID. **No message broker** until a measured need justifies one, and adding one would need a new ADR.
4. **No separate cache and no separate coordination service.** Locks, leases, idempotency, and deduplication use database constraints and row locks. No documented requirement calls for a cache.
5. **No external workflow engine in v1.** Revisit only with measured evidence that the job table can't meet the §6 requirements or the PRODUCT-SCOPE §10 duration and latency targets once they are calibrated. That needs a new ADR, and no thresholds are assumed here.
6. **Job and event payloads carry only IDs and references.** No secret values and no raw untrusted content, the same rule as events (SYSTEM-ARCHITECTURE §5).
7. **Workers are process roles of the same build** (ADR-001). They orchestrate and call ports. **They never run repo or test code.** That happens only in sandboxes, through EXEC (ADR-004).
8. **Ownership is unchanged.** ORCH owns stage jobs. Other contexts own their internal jobs (TI normalization, REPO validation, and so on). The job mechanism itself is shared-kernel infrastructure with no business rules.
9. **Mechanism on PostgreSQL** (ADR-005):
   - A worker claims due jobs with row locks that skip rows already locked (`FOR UPDATE SKIP LOCKED`). It sets the lease in the same short transaction.
   - **Polling is the source of truth** for picking up work.
   - PostgreSQL notifications may be used only as a wake-up hint, never for correctness.

**Implementation details for later design** (these don't need an owner decision):
- Whether to use a small in-house module or a library. Python ([ADR-007](ADR-007-implementation-stack-and-licensing.md)); it must meet decisions 1–9 and the permissive-licence policy.
- Polling interval, lease length, and retry tuning, within the §5 bounds.

## Rationale

- Atomic enqueue-with-state and the audit outbox both come for free from the single transactional store (ADR-005). An external engine or broker would still need an outbox to avoid dual writes.
- It adds nothing new to deploy, back up, or secure, which keeps self-hosting simple (PRODUCT-SCOPE A5).
- Stuck-work visibility (PRODUCT-SCOPE Workflow J) becomes a database query.

## Alternatives considered

| Alternative | Why rejected now |
|---|---|
| Dedicated workflow engine | It's a valid option (§6), but it adds a service and its own datastore to run and secure. Enqueuing with a state change still needs an outbox. No measured need yet |
| Message broker for events and jobs | Extra infrastructure, and it brings back the dual-write problem unless it's fed from the outbox anyway. No throughput requirement is documented |
| In-memory queue or scheduler | Violates "no correctness that depends on in-memory state" (§6) |
| Ad hoc cron plus status columns | No leases, no claim-once semantics, no cancellation contract. It would fail the §5 and §6 requirements |
| Separate cache or lock service | No documented need. Database constraints and locks already cover idempotency and leases |

## Consequences

- **Positive:** the only infrastructure is the database. Every durable step, timer, and event is visible and transactional. It works the same in a single-node self-hosted install.
- **Negative:** job, outbox, and business load share one database. Throughput is bounded by it, which is accepted until measured otherwise. Polling adds some pickup latency (not quantified; it's tuned against the §10 targets).
- **Reliability:** lease expiry and idempotent steps recover from worker crashes (PRODUCT-SCOPE §10 recovery metric). Sandbox teardown on a control-plane crash still relies on the sandbox port's self-expiry guarantee (ADR-004).
- **Follow-ups:** decide between an in-house module and a library during implementation design. Re-evaluate this ADR only with measurements.

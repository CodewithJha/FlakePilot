# Durable jobs and outbox delivery

Stage 6. The rules are [ADR-006](../adr/ADR-006-durable-jobs-in-the-database.md) and [ADR-012](../adr/ADR-012-event-envelope-and-outbox.md). This page is how the code behaves.

## Run the worker

Migrations are an explicit step. The API process does not start the worker and its readiness check is unchanged.

```sh
uv run alembic upgrade head
uv run python -m flakepilot.interface.composition_root worker
```

`SIGINT` or `SIGTERM` finishes the batch already claimed, then the process exits. A database outage is logged by exception class and the next poll tries again. The process does not log the database URL, job payloads, or exception messages.

The registered product job is `browser_reproduction`. Its payload is an execution id. The process also drains `iam.outbox`, `ti.outbox`, `diag.outbox`, and `exec.outbox`. Cleanup of sessions, revoked tokens, artifacts, and raw AI output is not registered. `LISTEN/NOTIFY` is not used. Audit and outbox rows are not deleted.

## Configuration

Empty means the default. Values outside the range fail startup and the message names the variable, not the value.

| Variable | Default | Range | Meaning |
|---|---|---|---|
| `FLAKEPILOT_WORKER_POLL_SECONDS` | 2 | 1–300 | Seconds between polls when the batch was empty or a tick failed |
| `FLAKEPILOT_WORKER_BATCH_SIZE` | 10 | 1–100 | Maximum jobs claimed per tick, and maximum outbox rows finished per outbox per tick |
| `FLAKEPILOT_WORKER_LEASE_SECONDS` | 30 | 5–3600 | Lease length. The worker renews it before the handler and again while the handler runs. The gap is one third of the lease, at least one second. A renewal the store rejects is not completed |

The worker is one process and one task. It does not start a pool of tasks.

## Job states

`orch.jobs`. States are `scheduled`, `running`, `completed`, `failed`, and `cancelled`. The last three are terminal. Rows are not deleted.

| From | To | When |
|---|---|---|
| (none) | `scheduled` | `enqueue`. The type must be in the worker's handler registry. The payload is version 1 and holds only ids, enums, booleans, and small integers. Anything else raises and writes nothing |
| `scheduled` | `running` | Claim, when `run_at` is due. `attempts` increases by one. `lease_owner` and `lease_expires_at` are set from the caller's clock |
| `running` | `running` | The same owner renews before `lease_expires_at`. Another owner gets false |
| `running` | `completed` | The handler returned and cancellation was not requested. Only the current owner, and only while the lease is unexpired |
| `running` | `scheduled` | The handler raised a retryable failure and `attempts` is still below `max_attempts` (default 5, at most 20). `run_at` becomes the clock plus the backoff. The lease is cleared |
| `running` | `failed` | A permanent failure, or `attempts` has reached `max_attempts` |
| `scheduled` or expired `running` | `cancelled` | `cancel_requested` is set. The handler is not started. A flag set while the handler is running is honored after the handler returns, not by killing it |
| expired `running` | `failed` | The lease expired and `attempts` is already at the maximum. `last_error` is `exhausted` |
| `scheduled` | `failed` | The stored type or payload does not match the registry. The handler is not called |

Backoff after attempt 1 is 1 second, then 2, 4, 8, …, capped at 60. Tests move the clock. They do not sleep.

## Claiming, leases, and crashes

A claim is one transaction: `SELECT … FOR UPDATE SKIP LOCKED` on rows that are due (`state = scheduled` and `run_at` at or before the clock) or expired (`state = running` and `lease_expires_at` at or before the clock), ordered by `run_at`, `id`, limited to the batch. The due index is `ix_orch_jobs_due`. The expired index is `ix_orch_jobs_expired`.

The lease is committed before the handler runs, so another worker will not take the row until the lease expires. Completion, renewal, retry, and cancel-ack all require the same `lease_owner` and an unexpired lease. A stale owner gets false and changes nothing.

If the process dies after the claim commits, the row stays `running` until `lease_expires_at`. The next claim runs the handler again. Handlers must tolerate that. This is at-least-once. It is not exactly-once.

The lease clock is the time the worker passes in, not `PostgreSQL now()`. Production uses the system clock in UTC. Two workers with skewed clocks can disagree by that skew. Job timestamps and outbox `occurred_at` are different clocks: outbox `occurred_at` and `published_at` use the database clock.

`last_error` is a short code (`handler_error`, `unknown_handler`, `exhausted`, `invalid_payload`, …). A free-text reason is stored as `invalid_error`. Logs include the code and the job id, not the payload.

An untrusted request cannot choose a handler or a tenant. The handler is the function registered for `job_type`. The payload is data. There is no HTTP route for enqueue, cancel, or the worker. The worker does not grant permissions, call a model, or run repository code.

## Outbox delivery

`iam.outbox`, `ti.outbox`, and `diag.outbox` keep their envelopes. The relay, in the same worker process, claims one unpublished row at a time with `FOR UPDATE SKIP LOCKED` (`published_at` and `dead_at` both null, ordered by `seq`). The partial index is `ix_*_outbox_unpublished` on `seq` with that predicate.

For a valid envelope the relay inserts `aud.events` and sets `published_at` in that same transaction. `aud.events.event_id` is the primary key. A second insert of the same id does nothing (`ON CONFLICT DO NOTHING`). The audit row keeps `org_id` and the payload as stored. There is no update and no delete, and there is no query route.

An envelope that fails the shared-kernel checks (including a payload string over 100 characters, or a value that is not an id, enum, boolean, or integer) is not inserted. The outbox row gets `dead_at`, `last_error = invalid_envelope`, and a higher `attempts`. The row stays. It is not published and it is not claimed again.

**Deliver versus mark.** For AUD, the insert and `published_at` commit together. A crash before commit leaves the outbox row unpublished and no audit row (or the audit row from a previous attempt, which the retry does not duplicate). A side effect performed outside that transaction can run twice. The relay has no such side effect. Other matrix subscribers are not called.

Polling is the source of truth. `LISTEN/NOTIFY` is not implemented. A row committed by another session is found on the next poll with no notification. A poll during the writer's still-open transaction does not take the row (`SKIP LOCKED`). A second poll after a successful publish finds nothing new. A missed or duplicate wake-up would not change this, because the wake-up is not required.

`int_secrets.outbox` does not exist. The secret store still has no caller.

## Migrations and retention

Head is `aud_0001` (after `orch_0001`). `orch_0001` creates schema `orch`. `aud_0001` creates schema `aud` and adds `attempts`, `last_error`, and `dead_at` to the three outboxes. Downgrade drops those columns and drops the schema. Published revisions are not edited.

Nothing in this stage deletes rows. Session cleanup, revoked-token cleanup, artifact orphan cleanup, raw AI output deletion, and outbox pruning (only after every subscriber, including AUD, has consumed the row) stay deferred. Parked outbox rows and audit rows are kept.

## What this does not do

- It does not run investigation stages, ingestion parsing, or retention jobs.
- It does not notify other contexts. Only AUD receives envelopes.
- It does not prune the outbox or the audit log.
- It does not expose an audit query.
- It does not use a broker, Redis, Kafka, or a workflow engine.
- It does not claim exactly-once delivery.

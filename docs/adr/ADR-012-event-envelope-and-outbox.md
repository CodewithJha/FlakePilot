# ADR-012: Event envelope, per-context outbox, and IAM event names

| | |
|---|---|
| Status | **Accepted** by the project owner, 2026-10-21 (approach: option a). Items marked **provisional** are calibrated later |
| Related | [SYSTEM-ARCHITECTURE §5, §7](../architecture/SYSTEM-ARCHITECTURE.md#5-cross-cutting-runtime-mechanisms), [PRODUCT-SCOPE §7](../product/PRODUCT-SCOPE.md#7-audit-requirements-consolidated), [DEPENDENCY-RULES §2, §6](../architecture/DEPENDENCY-RULES.md#6-shared-kernel), [BOUNDED-CONTEXTS](../architecture/BOUNDED-CONTEXTS.md), [ADR-005](ADR-005-system-of-record-database.md), [ADR-006](ADR-006-durable-jobs-in-the-database.md), [ADR-011](ADR-011-authentication-sessions-and-tokens.md), [IAM-DATA-MODEL](../architecture/IAM-DATA-MODEL.md) |

## Context

- SYSTEM-ARCHITECTURE §5: every state change publishes an envelope (event ID, org ID, repo ID, actor, action, target, before and after, reason, correlation ID, timestamp) to the outbox **in the same transaction**. A failed outbox write aborts the change (fail closed). AUD appends every envelope to the audit log.
- PRODUCT-SCOPE §7 adds **the request origin** to the audit record. Membership, grants, ingestion (CI upload) tokens and secrets must be audited.
- ADR-011 deferred audit storage "to the first state-changing use case". Stage 3 now has several: sign-in, session rotation, sign-out, CI upload token creation and revocation, and operator org bootstrap.
- The envelope and the outbox contract are shared-kernel items. DEPENDENCY-RULES §6 requires an ADR to change the shared kernel; this is that ADR.
- The relay and `aud.events` now exist (ADR-006 Stage 6). They deliver `iam`, `ti`, and `diag` only. `int_secrets.outbox` is still absent.

## Decision

| # | Topic | Decision |
|---|---|---|
| 1 | Envelope | A pure shared-kernel value type (`flakepilot.shared_kernel.events`): no I/O, no clock, no vendor types. Fields are listed below |
| 2 | Outbox | **One outbox table per context, in that context's own schema** (ADR-005 decision 2). It is written by the context's store **in the same transaction** as the change, after the change it describes. If the insert fails, the whole change rolls back. The first one is `iam.outbox` |
| 3 | Relay and audit log | The worker delivers `iam.outbox`, `ti.outbox`, and `diag.outbox` to `aud.events`. Delivery is at least once. AUD dedupes by `event_id`. `published_at` is set in the same transaction as the insert. Invalid envelopes set `dead_at` and stay in the outbox. There is no audit query. Other subscribers are not called. Outbox pruning is still deferred |
| 4 | Event names | `<context>.<entity>.<verb>`, lowercase. The context prefix is required because BOUNDED-CONTEXTS lists the same unprefixed name under several publishers (for example `integration.config_changed`). IAM's names are listed below |
| 5 | What events never contain | Secret values, credentials (session IDs, CI upload tokens), **hashes of credentials** (session `id_hash`, `token_hash`), token display prefixes, raw untrusted content, IP addresses, and raw headers. Payloads hold only IDs, enum values, booleans and counts |
| 6 | Order | `iam.outbox.seq` (an identity column) records insertion order, including within one transaction. How the relay handles commit order across concurrent transactions is a relay design detail |
| 7 | Retention (**provisional**) | Audit records: 1 year (PRODUCT-SCOPE Workflow J). Actor IDs, and logins referenced through user IDs, are personal data under the retention policy. Outbox rows may be pruned only after every subscriber, AUD included, has consumed them (RELIABILITY-AND-SLOS F-3a) |

### Envelope fields

| Field | Type | Notes |
|---|---|---|
| `event_id` | UUID (uuid4) | Primary key; AUD dedupes by it |
| `name` | text | The event name (PRODUCT-SCOPE "action") |
| `schema_version` | integer | `1` |
| `occurred_at` | timestamptz | Not carried in the value type. The outbox stamps it with the database transaction time (`now()`), the same clock as the business rows' timestamps |
| `org_id` | UUID, nullable | Null for user-scoped events (sessions and sign-in are per user, not tenant-owned) |
| `repo_id` | UUID, nullable | Set when the change concerns one repo |
| `actor_type`, `actor_id` | `user` / `ci_token` (with an ID) · `operator_cli` / `system` (no ID) | PRODUCT-SCOPE §7: user ID, token ID, or system. `operator_cli` is the operator running a CLI command such as `bootstrap-org` |
| `channel`, `correlation_id` | `http` (correlation = the request ID returned in `X-Request-ID`) · `cli` (one fresh ID per run) | The request origin (PRODUCT-SCOPE §7). The correlation ID ties together every event of one request or run (SYSTEM-ARCHITECTURE §5). More channels (for example jobs) are added with their runtime |
| `subject_type`, `subject_id` | text, UUID | The target (PRODUCT-SCOPE "target type and ID") |
| `before`, `after` | text, nullable | State values where the subject has a state (for example `active` → `revoked`) |
| `reason` | text, nullable | For example the session revoke reason |
| `payload` | JSON object | IDs, enum values, booleans and counts only; keys are short snake_case names |

### IAM events

These concrete names cover only IAM's existing state changes. They map onto the families BOUNDED-CONTEXTS lists for IAM (`ingestion_token.*`, `member.*`). BC-Q3 (PATCH) and BC-Q4 (TI) stay open.

| Event | Emitted by | `org_id` | Subject | before → after, reason | Payload |
|---|---|---|---|---|---|
| `iam.user.signed_in` | Sign-in completed (user created or login refreshed) | null | user | — | `user_created` |
| `iam.session.created` | Sign-in | null | user | → `active` | — |
| `iam.session.revoked` | Rotation on sign-in (`rotated`), sign-out (`signed_out`), privilege change (`privilege_change`) | null | the session's owner (user) | `active` → `revoked`, reason | `sessions` (count) |
| `iam.ci_upload_token.created` | Token issued | org | ci_upload_token | → `active` | — |
| `iam.ci_upload_token.revoked` | Token revoked | org | ci_upload_token | `active` → `revoked` | — |
| `iam.org.bootstrapped` | Operator `bootstrap-org`: the org, its admin user (found or created) and the admin membership, in one transaction | org | org | — | `admin_user_id`, `admin_user_created` |

- Sessions are referenced by their owner only. A session has no ID other than its hash, and hashes are excluded (decision 5).
- Not events: session last-seen touches (operational) and failed sign-ins (no state change; they are logged with a reason code).
- Membership and grant changes beyond bootstrap get their names with their use cases (`iam.membership.*`, `iam.grant.*`). Each also revokes the user's sessions (`iam.session.revoked`, `privilege_change`) in the same transaction (IAM-DATA-MODEL).

### Secret writes (contract only, not wired)

The encrypted secret store has no production caller yet. When it gets one, `put` and `delete` append `int.secret.written` and `int.secret.deleted` to `int_secrets.outbox`, in the same transaction as the secret row. The payload holds the scope (`org_id`, `repo_id`) and the secret **name** only, never the value (PRODUCT-SCOPE §7: "secrets (names only)"). The owning domain's use case supplies the actor and origin.

## Alternatives considered

| Alternative | Why not |
|---|---|
| One shared outbox table for all contexts | Simpler relay, but it breaks ADR-005's one namespace per context |
| Writing straight to an audit-log table (no outbox) | DEPENDENCY-RULES §2 rule 5: writing to the audit log is never a direct call |
| Accepting Stage 3 without audit | Security-relevant changes would leave no durable record |
| Session hash or token display prefix as the event subject | Derived from a credential; the owner user ID and the token ID identify them without it |

## Consequences

- **Positive:** every IAM, TI, and diagnosis state change is durably recorded atomically with the change, and fails closed. The relay copies those envelopes to `aud.events` without the writers calling AUD.
- **Negative:** there is still no audit query or export. Outbox rows are not pruned. A crash before the relay commits delays the audit row; it does not drop it.
- **Follow-ups:** outbox pruning after AUD (and any later subscriber) has consumed the row; an authorized audit query; `int_secrets.outbox` when the secret store gets a caller; calibrate retention.

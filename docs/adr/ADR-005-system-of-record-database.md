# ADR-005: One transactional relational database as the control-plane system of record

| | |
|---|---|
| Status | **Accepted** by the project owner, 2026-10-20, with the conditions recorded below. Database product: **PostgreSQL** (owner-confirmed, OQ-4/OQ-16). The remaining open items are implementation details for later design |
| Related | [SYSTEM-ARCHITECTURE §5, §7, §10](../architecture/SYSTEM-ARCHITECTURE.md#10-unresolved-and-deferred-decisions), [DEPENDENCY-RULES §1–2](../architecture/DEPENDENCY-RULES.md#2-cross-domain-rules), [ADR-003](ADR-003-provider-adapters.md), [ADR-006](ADR-006-durable-jobs-in-the-database.md), [ADR-007](ADR-007-implementation-stack-and-licensing.md), [ADR-009](ADR-009-initial-provider-adapters.md) |

## Context

The control plane needs durable state for all 11 contexts. Requirements already in the docs:
- **Audit is committed atomically with every state change.** The event goes to the outbox in the same transaction, and an outbox write failure aborts the action (SYSTEM-ARCHITECTURE §5; PRODUCT-SCOPE §7, Workflow J).
- **One context per transaction.** No distributed transactions. Cross-context effects go through the outbox or synchronous queries (DEPENDENCY-RULES §2 rule 3; ADR-001 decision 4).
- **Compare-and-set lifecycle transitions** (SYSTEM-ARCHITECTURE §8 I). **Idempotency keys** that reject or return duplicates (SYSTEM-ARCHITECTURE §5; PRODUCT-SCOPE Workflow B).
- **Records that never change once written**: evidence versions, patch versions, decision and approval records, criteria snapshots. **Append-only audit.** **Retention deletion** (PRODUCT-SCOPE §12, "Data model and storage").
- **Each context owns its data exclusively.** No reading another context's persistence, no joins across contexts, references by ID (DEPENDENCY-RULES §2 rule 1; SYSTEM-ARCHITECTURE §7.3).
- **Tenant scoping on every record**, plus a data-access guard (SYSTEM-ARCHITECTURE §7.1).
- **No secret values** outside the secret store and adapters (SYSTEM-ARCHITECTURE §7.4; ADR-003 decision 4). **Artifact bytes** go through the artifact port (SYSTEM-ARCHITECTURE §4).
- ADR-001's rationale already assumes "one process and one persistence boundary". The product must be self-hostable (PRODUCT-SCOPE A5) and portable across hosting vendors ([ADR-008](ADR-008-deployment-and-trust-model.md)).

## Owner acceptance conditions (verbatim, 2026-10-20)

> "Accept the decision that PostgreSQL is the authoritative relational store for FlakePilot's core state. Conditions: Bounded contexts retain ownership of their domain records. Cross-context access follows documented interfaces and dependency rules. Schema changes use controlled migrations. Tenant isolation, audit integrity, and transaction boundaries are explicit. Artifact binaries and other large evidence objects need not be stored directly in relational tables."

| Condition | Where this ADR meets it |
|---|---|
| Bounded contexts retain ownership of their domain records | Decision 2 |
| Cross-context access follows documented interfaces and dependency rules | Decision 2, which now names [BOUNDED-CONTEXTS](../architecture/BOUNDED-CONTEXTS.md) and [DEPENDENCY-RULES §3.1](../architecture/DEPENDENCY-RULES.md#31-matrix-rows-depend-on-columns) explicitly |
| Schema changes use controlled migrations | Decision 7 (**added**; this was previously provisional) |
| Tenant isolation, audit integrity, and transaction boundaries are explicit | Decisions 3, 4, and 5 |
| Artifact binaries and large evidence objects need not be in relational tables | Decision 6. This ADR goes further and keeps them **out** of the tables (artifact port, SYSTEM-ARCHITECTURE §4). That satisfies the condition |

## Decision

**Binding:**
1. **PostgreSQL** is the one transactional relational database, and the system of record for all control-plane state of every context, including the outbox and the audit log. FlakePilot relies only on **core PostgreSQL features**, so self-hosted and managed instances both work and no vendor extension is required ([ADR-008](ADR-008-deployment-and-trust-model.md) portability).
2. **Each context owns a separate namespace** (PostgreSQL schema per context, provisional; see decision list below). Code in one context reads and writes only its own namespace. Cross-context access goes **only** through the documented application APIs and events in [BOUNDED-CONTEXTS](../architecture/BOUNDED-CONTEXTS.md), within the [DEPENDENCY-RULES §3.1](../architecture/DEPENDENCY-RULES.md#31-matrix-rows-depend-on-columns) matrix. **No cross-context joins, reads, or foreign keys**; references are opaque IDs. The automated boundary checks cover data-access code too (DEPENDENCY-RULES §7).
3. **Transaction boundaries are explicit.** One context per transaction. Outbox rows, and job rows (ADR-006), are written in the same transaction as the state change they describe. Both are shared-kernel infrastructure: the owning context writes them, and they hold no business state.
4. **Audit integrity.** Immutable records are insert-only, and corrections create new versions. The audit log is append-only. Retention deletion is the only removal path, and it's audited.
5. **Tenant isolation.** Every tenant-owned row carries the org ID (plus the repo ID where it applies). The data-access guard rejects unscoped queries (SYSTEM-ARCHITECTURE §7.1). This applies **in self-hosted deployments too** ([ADR-008](ADR-008-deployment-and-trust-model.md)).
6. **No artifact bytes and no secret values in context-owned tables.** These tables hold only artifact references with their hashes, and secret references. Encrypted secret values may live in **the INT secret adapter's own namespace** in the same database ([ADR-009](ADR-009-initial-provider-adapters.md)). That namespace is read and written **only by that adapter**. No context, event, audit record, or log ever sees a value (SYSTEM-ARCHITECTURE §7.4).
7. **Controlled schema migrations.** They are versioned, ordered, owned per context, reviewed, and kept in the repo. They are applied as an explicit step before new code serves traffic. Ad hoc schema changes are not allowed.

**Migration tool and persistence library (owner-confirmed, FQ-6):** SQLAlchemy plus Alembic, used only in infrastructure-layer persistence adapters (DEPENDENCY-RULES §1; [ADR-007](ADR-007-implementation-stack-and-licensing.md)).

**Implementation details for later design** (these don't need an owner decision):
- **Namespace mechanism**: a PostgreSQL schema per context (proposed), or a table prefix.
- **Database-level defense in depth**: a database role per context, row-level security for tenant scoping, revoking UPDATE/DELETE on append-only tables. This is optional on top of the application-layer guards.

**Unchanged and still deferred:** physical tenant isolation (SYSTEM-ARCHITECTURE §10); data-model details (BC-Q5, BC-Q6, BC-Q7 in [BOUNDED-CONTEXTS §6](../architecture/BOUNDED-CONTEXTS.md#6-open-questions)).

## Rationale

- Audit atomicity, idempotency keys, and compare-and-set transitions all come from one transaction in one store. Any split would bring back the distributed-consistency problem that ADR-001 rejected.
- One store is the simplest thing to self-host and operate, and backup and restore cover all state at once.
- Namespacing plus boundary checks keep the context ownership and AI-isolation rules (DEPENDENCY-RULES §5) enforceable even though the tables share one database.
- PostgreSQL provides what ADR-005 and ADR-006 need: transactions, unique constraints, conditional updates, and row locks with skip-locked semantics. It is permissively licensed (the PostgreSQL License, consistent with [ADR-007](ADR-007-implementation-stack-and-licensing.md)) and runs anywhere.

## Alternatives considered

| Alternative | Why rejected |
|---|---|
| One database per context | Each context would still need its own outbox, and operations and backup would multiply for no documented need. It also goes against ADR-001's single persistence boundary. It could be revisited if a context is ever extracted (ADR-001 decision 5) |
| Document or key-value store as the system of record | It would have to provide the same transactions and unique constraints, and no documented requirement favors schemaless data |
| Event store with state rebuilt by event sourcing | Not required. Audit is already built from envelopes (SYSTEM-ARCHITECTURE §5). It adds complexity to every context |
| Artifact bytes stored in the database | The artifact port already exists for immutable blobs with scoped, short-lived read handles (SYSTEM-ARCHITECTURE §4). Large recordings would bloat the system of record and its backups |
| Shared tables across contexts | Breaks exclusive data ownership (DEPENDENCY-RULES §2) and would let a context reach DIAG data |
| MySQL-family or SQLite (OQ-4 options) | The owner chose PostgreSQL. SQLite would have limited concurrent writers across process roles (ADR-001) |

## Consequences

- **Positive:** audit fails closed by construction. Idempotency and compare-and-set transitions come straight from database constraints. There's one dependency to run and back up.
- **Negative:** the database is a single failure domain for the control plane, consistent with ADR-001's accepted trade-off. Namespace discipline has to be enforced by tooling. Cross-context references can't use foreign-key integrity, so deletion is guarded in the application (BC-Q5). Database backups contain encrypted secret values; the key stays outside the database (ADR-009).
- **Security:** secrets and artifact bytes stay out of business tables. Tenant scoping is a required guard. Optional database-level defenses can be added without changing the architecture.
- **Follow-ups:** decide the namespace mechanism and the optional database-level defenses during implementation design.

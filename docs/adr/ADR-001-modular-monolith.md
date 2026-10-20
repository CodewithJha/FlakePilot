# ADR-001: Modular monolith with strict domain boundaries

| | |
|---|---|
| Status | **Accepted**, 2026-10-20. Restates confirmed product principle P5 ([PRODUCT-SCOPE §1](../product/PRODUCT-SCOPE.md)) and the Stage 1.2 requirement "no microservices" |
| Related | [SYSTEM-ARCHITECTURE §1–2](../architecture/SYSTEM-ARCHITECTURE.md), [DOMAIN-BOUNDARIES](../architecture/DOMAIN-BOUNDARIES.md), [DEPENDENCY-RULES](../architecture/DEPENDENCY-RULES.md), [ADR-002](ADR-002-domain-layering.md) |

## Context

FlakePilot has 11 product domains (PRODUCT-SCOPE §8) with tight workflow coupling. One investigation touches TI, EXEC, DIAG, PATCH, VER, APPR, and ORCH. Requirements that cut across domains: audit committed atomically with every state change (§7), idempotent retries, and approval rules that depend on several domains' current state. There's no team, scale, or deployment evidence yet that would justify distributing these. Microservices are an accepted non-goal until there's demonstrated need (§9).

## Decision

1. Build the control plane as **one deployable modular monolith**. The 11 domains are modules with exclusive data ownership and public application APIs. Boundaries are enforced by the rules in [DEPENDENCY-RULES](../architecture/DEPENDENCY-RULES.md) and checked automatically.
2. The same build **may run in several process roles** (request-serving and background workers). Roles share code and boundaries. They are not services and get no separate APIs.
3. Untrusted execution is **outside** the monolith, on a sandbox provider behind a port ([ADR-004](ADR-004-control-plane-and-execution-plane.md)). That is an isolation boundary, not a service decomposition.
4. Domains communicate in-process through application APIs and transactional-outbox events. One domain per transaction. No distributed transactions.
5. Extracting a domain into a separate service needs a new ADR with **measured** evidence: an independent scaling need, a fault or security isolation need that the module boundary can't meet, or an independent team ownership need. The extraction must keep the dependency graph.

## Rationale

- Atomic state-plus-audit, compare-and-set lifecycle transitions, and approval rechecks are simple inside one process and one persistence boundary. Across services they'd need sagas and distributed consistency with no matching benefit.
- Strict module boundaries keep later extraction possible at low cost: data ownership, API-only access, an acyclic graph, events through an outbox.
- One deployable simplifies self-hosting (PRODUCT-SCOPE A5) and operations for small teams.

## Alternatives considered

| Alternative | Why rejected |
|---|---|
| Microservices per domain | Accepted non-goal (§9). Network, consistency, and operational cost with no demonstrated need. Audit atomicity and approval rechecks become distributed problems |
| Serverless function per workflow stage | Same consistency problems. Durable-workflow and lifecycle needs ([SYSTEM-ARCHITECTURE §6](../architecture/SYSTEM-ARCHITECTURE.md#6-sync-vs-async-and-durable-execution)) still need a coordinator. Ties the design to a hosting model, which is deferred |
| Monolith without enforced boundaries | Boundaries erode, AI isolation (DIAG invariant) can't be enforced structurally, and later extraction gets expensive |
| Separate "execution service" owned by FlakePilot | Unnecessary. Execution is delegated to a sandbox provider behind a port. FlakePilot's execution logic (EXEC) stays a control-plane module |

## Consequences

- **Positive:** simple consistency. One build to test, ship, and self-host. Boundaries enforced by tooling rather than by the network.
- **Negative:** one runtime failure domain for the control plane. Scaling is per process role, not per domain. Boundary discipline depends on automated checks being in place from the first code (deferred tooling, required now).
- **Follow-ups:** choose the language/framework with boundary-check tooling. Choose the durable job runtime and the persistence technology (all deferred, [SYSTEM-ARCHITECTURE §10](../architecture/SYSTEM-ARCHITECTURE.md#10-unresolved-and-deferred-decisions)).

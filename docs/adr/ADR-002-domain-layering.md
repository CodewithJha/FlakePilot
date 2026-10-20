# ADR-002: Per-domain layering and cross-domain dependency rules

| | |
|---|---|
| Status | **Accepted** by the project owner, 2026-10-20. The direction is mandated by the Stage 1.2 requirement "domain logic must not depend on infra vendors, web frameworks, or AI providers" |
| Related | [DEPENDENCY-RULES](../architecture/DEPENDENCY-RULES.md) (the authoritative rules), [DOMAIN-BOUNDARIES](../architecture/DOMAIN-BOUNDARIES.md), [ADR-001](ADR-001-modular-monolith.md), [ADR-003](ADR-003-provider-adapters.md) |

## Context

Several things in the product depend on rules that stay pure and testable:
- Classification rules, verification outcome computation, staleness rules, and patch validation must be deterministic and recomputable (PRODUCT-SCOPE Workflows C and G).
- Providers must be replaceable (P4, P6).
- AI must be structurally unable to influence classification or verification (P1).

Without explicit layering and a fixed domain graph, vendor types and cross-domain shortcuts leak into exactly these rules.

## Decision

1. **Four layers.** Each domain has **domain**, **application**, and **infrastructure** layers. One system-wide **interface** layer sits above them. Direction: interface → application → domain, with infrastructure → application/domain (dependency inversion). Domain depends only on the shared kernel.
2. **The authorization enforcement point is the application layer** of each domain, calling IAM's decision point. The interface layer only authenticates.
3. **Cross-domain access** is only through public application APIs and publisher event contracts, following the fixed, acyclic **dependency matrix** (36 edges). No shared tables. One domain per transaction.
4. **Cycle breaking** uses inverted ports (currently only `CodeHostAccessVerifier`: defined by IAM, implemented by REPO) and the event envelope for audit.
5. **The diagnosis isolation invariant**: no path from TI, VER, or APPR to DIAG. AI output enters other domains only as a PATCH candidate diff.
6. **A minimal shared kernel** (identifiers, actor, correlation ID, event envelope, errors, clock). No business rules.
7. **Automated enforcement** in the build. Changing the matrix requires an ADR.

Rules in detail: [DEPENDENCY-RULES](../architecture/DEPENDENCY-RULES.md). It isn't repeated here.

## Rationale

- Pure domain layers make outcome computations reproducible from stored inputs. The product requires this ("every outcome can be recomputed").
- A fixed matrix turns "AI can't affect verification" from a convention into a property the tooling checks.
- Enforcement in the application layer covers every entry path, including workers and event handlers, not just HTTP.
- ORCH depends on everything and nothing depends on ORCH. That keeps workflow coordination in one place and keeps domains reusable.

## Alternatives considered

| Alternative | Why rejected |
|---|---|
| Global technical tiers (all controllers, all services, all repositories) | No domain ownership. Cross-domain coupling is invisible. Can't express the DIAG invariant |
| Vertical slices with no domain layer | Rules end up mixed with I/O. Recomputing outcomes from stored inputs becomes hard |
| Enforcement only in the interface layer | Worker and event paths would bypass authorization. Re-authorization at apply time (Workflow H) would have no natural home |
| Shared database with cross-domain joins | Breaks data ownership and later extraction. Lets any domain read DIAG data |
| Choreography only (no orchestrator) | The lifecycle (PRODUCT-SCOPE Workflow I) would be spread across subscribers. Harder to guarantee "exactly one state, only diagrammed transitions" |
| Allowing PATCH → DIAG (PATCH calls the AI itself) | Would put DIAG on VER's and APPR's dependency path and break the invariant check. ORCH carries candidate text instead |

## Consequences

- **Positive:** testable, deterministic core rules. Mechanically checked AI isolation. Providers swappable without touching domain code.
- **Negative:** some indirection: ORCH relays candidate text, and approval requests carry the creator ID. Interface-layer composition is needed for screens that mix domains. Boundary tooling must exist before feature code.
- **Follow-ups:** pick boundary-check tooling with the language choice (deferred).

# ADR-003: Provider ports and adapters owned by External integrations

| | |
|---|---|
| Status | **Accepted** by the project owner, 2026-10-20. The direction is mandated by PRODUCT-SCOPE P4 and P6 and by the Stage 1.2 requirement for explicit ports |
| Related | [SYSTEM-ARCHITECTURE §4](../architecture/SYSTEM-ARCHITECTURE.md#4-ports-architectural-interfaces) (port definitions), [DOMAIN-BOUNDARIES](../architecture/DOMAIN-BOUNDARIES.md) (INT), [ADR-002](ADR-002-domain-layering.md) |

## Context

FlakePilot depends on seven kinds of external providers: AI, code host, CI/report sources, sandbox, artifact storage, secret storage, and identity. Product constraints:
- Every provider is replaceable (P4).
- Core features work with AI disabled and without any sponsor integration (P6).
- Providers must offer least privilege, short-lived credentials, and no merge capability (§4).
- Every provider choice is deferred (§12).

PRODUCT-SCOPE §8 originally worded this inconsistently. It was aligned with this ADR on 2026-10-20. **This ADR is authoritative on port ownership.**

## Decision

1. **INT owns every port contract and every vendor adapter.** INT is a leaf (depends on no domain), contains no business rules, has no user-facing use cases, and is never called by the interface layer.
2. **Consuming domains own semantics and data:**
   - DIAG owns request building, data policy, and output validation.
   - REPO owns permission semantics and repo config.
   - EXEC owns run records.
   - TI owns normalized results.
   - Each domain owns the records it stores in artifact storage.
   - Integration *configuration* use cases (provider choice, credential entry) live in the consuming domain, which authorizes through IAM and gives INT only the credential.
3. **The contracts are vendor-neutral, and each has a required guarantee list** (SYSTEM-ARCHITECTURE §4). Some guarantees are **structural**: the code-host contract has no merge operation and no push to existing branches, and the secret contract never returns values to a domain or application layer.
4. **Secret values are resolved only inside adapters** at the point of use. Domain and application layers handle references only.
5. **Every port gets a conformance suite** (run against every adapter) and an in-memory **fake**. A new adapter is accepted only if it passes the suite.
6. **Typed error set** shared by all adapters. Retry and `needs_input` / `failed` mapping per [SYSTEM-ARCHITECTURE §5](../architecture/SYSTEM-ARCHITECTURE.md#5-cross-cutting-runtime-mechanisms).
7. **No dynamic third-party plugin loading** in v1. Adapters are first-party code selected by configuration.
8. **AI disabled is a policy state** (DIAG reports `unavailable`), not a fake adapter in production.

## Rationale

- One owner for all contracts matches the product scope's "adapter interfaces" assignment, and avoids duplicating multi-consumer contracts such as artifact storage.
- A leaf INT can't create cycles. Its lack of business rules keeps providers swappable without moving domain logic.
- Structural guarantees (missing operations) are stronger than policy checks against prompt injection or bugs.
- Conformance suites turn the "replaceable" principle into something testable.

## Alternatives considered

| Alternative | Why rejected |
|---|---|
| Domains call vendor SDKs directly | Breaks P4 and ADR-002. Vendor types leak into rules. Can't swap or fake providers |
| Each consuming domain defines its own port contracts | Purer dependency inversion, but multi-consumer contracts (artifact storage) would be duplicated or have unclear ownership. Conflicts with the product scope's assignment of adapter interfaces to INT |
| Generic plugin system with runtime-loaded third-party adapters | Security risk (untrusted code in the trusted control plane) and YAGNI for v1. Can be revisited by ADR |
| One generic "provider" abstraction for everything | Too vague to state guarantees such as isolation, no merge, or write-only secrets |
| INT also owns integration configuration UX | INT would need IAM authorization (not a leaf any more) and would take over domain decisions (the AI data policy belongs to DIAG) |

## Consequences

- **Positive:** providers are swappable and testable through fakes. Least-privilege and no-merge are enforced by contract shape. No vendor lock-in decided at Stage 1.2.
- **Negative:** INT holds many adapters, so it needs internal organization per port. Contracts must be designed to the strictest guarantee even if a vendor offers more. Some vendor features go unused.
- **Follow-ups:** each deferred provider choice must show its adapter meets the port's guarantee list.

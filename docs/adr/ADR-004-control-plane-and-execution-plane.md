# ADR-004: Separate trusted control plane from untrusted execution plane

| | |
|---|---|
| Status | **Accepted** by the project owner, 2026-10-20. The direction is mandated by PRODUCT-SCOPE §4 and the non-goal "executing untrusted code without isolation" |
| Related | [SYSTEM-ARCHITECTURE §3](../architecture/SYSTEM-ARCHITECTURE.md#3-control-plane-vs-execution-plane) (the authoritative boundary rules), [SYSTEM-ARCHITECTURE §4](../architecture/SYSTEM-ARCHITECTURE.md#4-ports-architectural-interfaces) (sandbox port), [ADR-003](ADR-003-provider-adapters.md) |

## Context

FlakePilot runs code it doesn't control: repo code, tests, dependencies, and AI-written patches. That code may be malicious, may try to read credentials or reach internal networks, and may write prompt-injection payloads into logs and artifacts that later reach the AI. The product requires:
- Per-run isolation, default-deny egress, no credentials in sandboxes, size-limited export (§4).
- Outcomes computed from execution evidence (P2).
- AI output never treated as fact (P1).

## Decision

1. **Two planes.**
   - The **control plane** (the monolith, its persistence, its adapters) is trusted and never runs repo code: no scripts, hooks, builds, or linters.
   - The **execution plane** (ephemeral sandboxes from a provider behind the sandbox port) is untrusted.
2. **The control plane starts every interaction**: provision, execute, collect, teardown. The execution plane has no route to the control plane or cloud metadata endpoints. Any push-based artifact channel must be write-only, scoped to one run, size-limited, and short-lived.
3. **Only these go into the sandbox:**
   - A run spec whose commands come only from the versioned repo config.
   - A source snapshot fetched by the control plane, with no credentials.
   - A validated patch diff.
   - The listed test-environment secrets, resolved inside the adapter.
   - The egress allowlist, enforced outside the sandbox.

   **Never:** code-host tokens, AI keys, storage credentials, control-plane credentials.
4. **Everything coming out is untrusted input:**
   - Size-capped, hashed into a manifest, redacted in the export adapter.
   - Parsed by hardened parsers.
   - Never executed in the control plane.
   - Never rendered in a way that could run script in FlakePilot's origin.
5. **Results are claims.** TI and VER re-parse raw outputs and compute every outcome in the control plane.
6. **Prompt-injection containment.** Artifact and repo content reaches DIAG only as delimited data. DIAG output reaches other domains only as a PATCH candidate that must pass validation, and executes only in the sandbox. That is enforced by the diagnosis isolation invariant ([DEPENDENCY-RULES §5](../architecture/DEPENDENCY-RULES.md#5-diagnosis-isolation-invariant)).
7. **Sandbox adapters must meet the port's guarantee list**, including self-expiring teardown and separating test results from environment errors. The sandbox technology is **deferred**.

## Rationale

- Separating planes is the only way to give untrusted code no reach into credentials or tenants while keeping FlakePilot's own state trusted.
- Starting every interaction from the control plane removes a whole class of attacks: no listener for the sandbox to abuse.
- Recomputing outcomes from raw outputs means a compromised sandbox can at most produce bad evidence. It can't directly fake an outcome summary that FlakePilot trusts. Residual risk: forged raw outputs. This is mitigated by baseline-vs-patched comparison and the false-positive guardrail (PRODUCT-SCOPE R2, R3), not eliminated.

## Alternatives considered

| Alternative | Why rejected |
|---|---|
| Run tests in control-plane worker processes or containers sharing the host | Untrusted code next to credentials and tenant data. Breaks §4 |
| Run reproduction and verification in the user's own CI | Needs write and trigger permissions on the code host (against least privilege). Gives up control of repeat counts, environment, and artifacts. Results arrive through a channel FlakePilot can't constrain |
| Give the sandbox a short-lived code-host token to clone the repo itself | Puts a credential in the untrusted plane. The control plane fetches the snapshot instead |
| Trust the sandbox's own pass/fail summary | A compromised or buggy sandbox could fake outcomes directly. Breaks P2 |
| Run on developers' local machines | Isolation, repeatability, and audit not guaranteed |

## Consequences

- **Positive:** credentials and tenant data out of reach of executed code. Outcomes can be recomputed from stored raw outputs. Prompt injection is contained by structure, not by model behavior.
- **Negative:** source snapshots and artifacts move through the control plane (bandwidth and storage cost). Some tests can't run without services that are excluded by default egress, which shows up as `unsupported` (PRODUCT-SCOPE R5). Depends on the chosen sandbox technology meeting strict guarantees.
- **Follow-ups (deferred):** sandbox technology. Artifact transfer mode (pull vs scoped push). Safe rendering of HTML/DOM artifacts. Redaction approach.

## Stage 10 implementation note

The handoff the control plane may send is `PlaneRequest` in `execution/domain/plane.py`. It has no credential field. `UnavailableExecutionPlane` is the only adapter and it returns `execution_host_unavailable`. The worker does not construct `ContainerSandbox`. A dedicated host is still required before that adapter can be replaced. See [EXECUTION](../operations/EXECUTION.md).

## Stage 7 implementation note

The sandbox technology is no longer entirely deferred, and it does not meet decision 7 for production. The default adapter returns `execution_closed` and starts nothing. An explicit evaluation adapter builds a `docker` or `podman` argument list (network none, dropped capabilities, read-only root, non-root user, resource limits, digest-pinned image, one approved command). That client is not a microVM, has no TTL reaper, and was not executed in CI. The control plane classifies the outcome. A sandbox failure is not a test failure. Details and the remaining gaps are in [EXECUTION](../operations/EXECUTION.md).

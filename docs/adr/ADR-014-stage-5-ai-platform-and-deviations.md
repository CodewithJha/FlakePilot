# ADR-014: Stage 5 AI platform scope and deviations

| | |
|---|---|
| Status | **Accepted** with the implementation of the assist use case, 2026-10-21. AI stays disabled unless an org admin sets a policy |
| Related | [PRODUCT-SCOPE P1, P6, D6, Workflows E and J](../product/PRODUCT-SCOPE.md), [BOUNDED-CONTEXTS › DIAG](../architecture/BOUNDED-CONTEXTS.md), [ADR-003](ADR-003-provider-adapters.md), [ADR-006](ADR-006-durable-jobs-in-the-database.md), [ADR-009](ADR-009-initial-provider-adapters.md), [ADR-012](ADR-012-event-envelope-and-outbox.md), [ADR-013](ADR-013-stage-4-ingestion-scope-and-deviations.md) |

## Context

DIAG is the only path to an AI provider. The port, a null adapter, and a deterministic fake exist so the core can run with AI disabled (P6). No evidence bundles, IAM quotas, or job runtime exist yet. This ADR records what Stage 5 actually does.

## Behaviour

- Default policy, including a missing `diag.ai_policies` row: `disabled`, and raw output is not retained.
- Only an org admin of that org can change the policy. `repo.investigate` is required to assist. Code-host access is supplied by the caller; this stage does not construct it.
- A disabled policy does not call the port. The outcome is `unavailable` / `policy_disabled`.
- The composition root wires `NullAiAssist` only. An enabled policy with that adapter is `unavailable` / `no_provider`. `FakeAiAssist` is for tests and the offline evaluation harness.
- Output is schema-validated. Failure stores a reason code. Citations match a caller-supplied set of evidence IDs exactly: known ids are `evidence_linked`, anything else is `unverified`. Confidence is discarded. `contradicted` is not produced.
- Raw output is stored only when the policy snapshot has `retain_raw` and the text is within 64 KiB. Events carry counts, the policy, the provider, and a SHA-256 of the request. They do not carry instructions, model text, or hypothesis summaries.
- There is no HTTP route.

## Deviations

| # | Deviation | What restores the documented behaviour |
|---|---|---|
| 1 | **No AI spend quota.** IAM has no quota store. The use case does not check a quota and does not emit `quota.denied`. This is not enforcement | IAM quotas (`quota.denied`) |
| 2 | **No HTTP route** for policy or assist. Both are application methods | A later interface, with code-host access from `CodeHostAccessVerifier` |
| 3 | **No evidence bundles.** The citation check uses a caller-supplied ID set. Unknown or absent citations are `unverified`. `contradicted` is not produced | Workflows C and E, when evidence is sealed and can be compared |
| 4 | **The request row and the outbox rows are written in one transaction after the in-process port call.** `NullAiAssist` and `FakeAiAssist` have no network side effect. A future network adapter must record intent before the call | That adapter, recording intent first |
| 5 | **The outbox relay is still deferred** ([ADR-006](ADR-006-durable-jobs-in-the-database.md), [ADR-012](ADR-012-event-envelope-and-outbox.md)). `diag.outbox` is not delivered | The relay and AUD |
| 6 | **No raw-output retention deletion job** | The ADR-006 retention job |
| 7 | **Diagnosis runs in the use case**, not as an ORCH durable stage job | ORCH's diagnosis stage |

## Events

`diag.ai_policy.updated`, `diag.ai_request.sent` (only when the port is called), `diag.diagnosis.recorded`.

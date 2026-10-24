# ADR-017: Sponsor-capability phase — investigation workflow, advisory intelligence, and provider ports

| | |
|---|---|
| Status | **Accepted** (post-release development phase, 2026-10-24, owner-authorized). Deterministic classification stays authoritative; advisory AI stays inside the ADR-009/014 policy; untrusted-code execution stays disabled (`ClosedSandbox` posture unchanged) |
| Related | [ADR-003](ADR-003-provider-adapters.md), [ADR-005](ADR-005-system-of-record-database.md), [ADR-009](ADR-009-initial-provider-adapters.md), [ADR-012](ADR-012-event-envelope-and-outbox.md), [ADR-013](ADR-013-stage-4-ingestion-scope-and-deviations.md), [ADR-014](ADR-014-stage-5-ai-platform-and-deviations.md), [SPONSOR-IMPLEMENTATION-MAP](../operations/SPONSOR-IMPLEMENTATION-MAP.md), [SPONSOR-INTEGRATION-MATRIX](../operations/SPONSOR-INTEGRATION-MATRIX.md), [RELEASE-CANDIDATE](../operations/RELEASE-CANDIDATE.md) |

## Context

The local-only release candidate was closed at `395982a` (tag
`local-only-release-candidate`; CI run `40070363798` success). The owner then
authorized a separately scoped development phase to give each of the 16
sponsor goals a real capability where the evidence supports one, with these
boundaries unchanged: no spending, no accounts, no credentials, no
infrastructure, no domains, and no execution of untrusted code.

The released architecture already contains the hard parts the sponsor
capabilities need: `ports.py` (ADR-003) for vendor-neutral contracts,
`AiAssistPort` with the fail-closed `Assist` use case (ADR-009/014), the
deterministic `ti-classification/1` rule set as the sole classifier, the
append-only event envelope/outbox (ADR-012), and IAM with explicit grants.
What is missing is the connective tissue: no wrapper turns stored evidence,
research, advisory model output, and history into one investigated report,
and no provider adapter can actually answer at all (production has always
wired `NullAiAssist`).

This ADR decides how the new capabilities enter that architecture without
duplicating orchestration, storage, or authorization authorities.

## Decisions

1. **One workflow authority stays: the durable outbox/jobs (ADR-012).**
   The investigation workflow is a bounded, typed, local orchestrator in
   DIA's application layer — stages, budgets, and stopping conditions are
   code, not an external runtime. A second authority (Temporal or an agent
   runtime) would own the same invariants with no ownership advantage;
   the conditions under which that changes are recorded in
   [TEMPORAL-DURABLE-WORKFLOW-EVALUATION](../operations/TEMPORAL-DURABLE-WORKFLOW-EVALUATION.md).
   Mastra's goal (agent-shaped investigation) is met by this local workflow;
   introducing the Mastra runtime would add a second, weaker trust boundary
   in front of evidence with no capability this design lacks.
2. **Advisory intelligence enters only through `AiAssistPort` + `Assist`.**
   Hypotheses, summaries, and failure explanations come from the existing
   assist pipeline: org-admin policy gate, canonical payload, citation
   checking against caller-supplied evidence, bounded claims, provenance of
   provider and model. Deterministic classification is never overridden;
   model claims are advisory and evidence-linked or unverified — never
   authorization.
3. **The local/offline model path is an operator-supplied endpoint.**
   Adapters speak an operator-configured local HTTP endpoint
   (e.g. a self-hosted Gemma inference server the operator runs); the
   repository downloads nothing, ships no weights, and records the model
   identity the endpoint reports. Without configuration the port stays
   `NullAiAssist`-shaped: `unavailable`, fail-safe, zero provider contact.
4. **Untrusted external content is data, never instructions.**
   Research excerpts, browser-derived evidence, and model text alike enter
   workflows as untrusted data parts or citations. They can never carry
   budget authority, grant rights, or replace deterministic verdicts.
   Retrieved sources keep URL + retrieval timestamp + excerpt provenance;
   budgets, deduplication, and source validation are enforced by the port
   contract, not by callee promises.
5. **History analytics and prioritization are deterministic and evidenced.**
   Reliability trends and failure prioritization compute over stored
   attempt history with no leakage (only history at or before a cutoff).
   A probabilistic model (TabPFN/Tinker) is optional, must be evaluated
   against the deterministic baseline on real stored history, and may not
   claim accuracy, calibration, or confidence without recorded evaluation.
6. **Agent identity is delegated, scoped, and audited — never standing.**
   An agent principal is a named, short-lived delegate derived from a
   verified principal, carrying a bounded scope subset, with every use
   attributed through audit. An agent cannot grant, cannot mutate
   authorization, cannot execute anything, and cannot outlive its scope.
   Whether Auth0 can later *issue* such identities changes only the issuer;
   it changes no authorization rule. No HTTP routes exist for agents.
7. **Provenance is the existing event envelope, in the same transaction.**
   Investigation lifecycle provenance (stage, deterministic verdicts, model/
   provider identity, budgets, stopping condition) is appended to AUD via
   the existing outbox relay, never rewritten, and never carries secret
   values or raw untrusted text. Externally verified attestations (Entire
   or similar) remain a later, clearly separate stage.
8. **Browser evidence is a port with recorded fixtures; live browsing stays
   off.** The default composition records no page and runs no repository
   code. A live provider (Browserbase or the operator's own browser) enters
   only after owner-approved credentials and the same provenance and
   untrusted-content rules.
9. **PostgreSQL remains the single system of record (ADR-005).** MongoDB
   Atlas stays evaluated-not-selected until the documented concrete
   advantage exists; reports and provenance are composed values and audit
   events, which PostgreSQL already stores with the right integrity
   guarantees.
10. **No new vendor dependencies this phase.** Provider adapters use the
    repository's stdlib `Transport` pattern (matching ADR-003's
    `github.py`), with injectable transports for deterministic tests. A
    vendor SDK enters only after its terms, data flow, purity of licences,
    and the exact transmitted fields are verified — and only when a real
    provider call is actually authorized and lab-approved.

## Status vocabulary and the sponsor goals

The matrix ladder is unchanged. This phase moves locally implemented and
tested capabilities from **Planned/Not-justified** to **Tested with mocks**
(or **Implemented, untested** where an adapter exists without tests of its
failure modes); **Integration-tested** and beyond always requires either real
tables in PostgreSQL (family of existing integration tests) or a real
provider interaction. Nothing becomes **Live-verified** or
**Deployed and verified** without a real external event, which stays an
owner-gated step for every sponsor.

## Consequences

- The investigation product becomes a cohesive pipeline whose every stage
  has a provenance record and whose optional providers (model, research,
  speech, browser) are each individually disableable — the released
  fail-safe behavior when they are all absent.
- No duplicated orchestration, storage, or authorization authority enters
  the repository in this phase. Vendor SDK/HTTP integrations have a
  documented verification checklist instead of a package entry in
  `uv.lock`.
- The execution posture and all safety invariants from the released
  audit are untouched; this ADR adds reading and reasoning code paths
  only.

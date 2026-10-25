# Mastra evaluation

Companion to [ADR-017](../adr/ADR-017-sponsor-capability-phase.md) decision 1
and sponsor goal 2 (Mastra).

## Decision (2026-10-25): adopted as an external client, not a workflow authority

The owner asked for a real Mastra implementation that holds no authority.
Mastra is adopted **only as an external client** of FlakePilot, in
[`sidecars/mastra/`](../../sidecars/mastra/README.md). It is not adopted as a
workflow engine inside the product. The 2026-10-24 objections below still
hold for that second role, and this design avoids every one of them:

- **No second workflow authority.** The sidecar's workflow (`obtain-token →
  investigate → validate → summarize`) makes exactly one HTTP call to
  FlakePilot per invocation and never retries. Retries, leases, provenance, and
  every stage budget stay in the Python pipeline and the outbox/jobs.
- **No second trust boundary on the evidence path.** The sidecar has no
  database access and no FlakePilot credential. It holds one short-lived
  Auth0 client-credentials token for its own M2M client. FlakePilot verifies
  that token (`Auth0TokenVerifier`) and maps its subject to an admin-made
  binding (`iam.agent_bindings`). It then re-derives the agent's grants from
  the delegating admin's live grants and verified code-host standing, narrows
  them by the token, and runs its own `authorize()` and AI-policy gates. See
  `POST /agent/orgs/{org}/runs/{run}/investigation` in
  [AUTH0-AGENT-ROUTE](AUTH0-AGENT-ROUTE.md).
- **Bounded supply chain.** Node ≥ 22.18, a pinned `package-lock.json` (149
  packages), and two direct dependencies: `@mastra/core` 1.79.0 and `zod`
  4.6.7. Only the Apache-2.0 entry point `@mastra/core/workflows` is imported.
  The package's `ee/` content (Mastra Enterprise License) is installed but
  never imported, and a sidecar test enforces this. Every package in the tree
  is permissive; the README lists the licences. Mastra's product telemetry
  (PostHog) is disabled with `MASTRA_TELEMETRY_DISABLED=1`, and no `Mastra`
  instance is constructed. Nothing in the Python package or its licence gate
  changes.
- **Advisory output.** The sidecar's report summarizes the JSON FlakePilot
  returned. Verdicts are FlakePilot's deterministic rule set, model output
  arrives as counts, and the report says `"authority": "flakepilot"`.

### Evidence (2026-10-25)

- **VERIFIED:** `npm ci && npm test` in `sidecars/mastra`: `tsc --noEmit`, then
  8 `node:test` cases against a mocked Auth0 token endpoint and a mocked
  FlakePilot, all passed. The cases cover the happy path with one token and
  one investigation call, a FlakePilot refusal (401/403/404/503 each called
  once, never retried), a token refusal before any FlakePilot call, a schema
  violation, a timeout, invalid input, config refusals that never echo
  values, and the `ee/` import guard.
- **VERIFIED, local:** a live run (`tests/live/test_live_mastra.py`). The real
  `create_app` was served by uvicorn on loopback against a throwaway
  PostgreSQL 16 database, with hosted Gemma and SerpApi configured.
  `node src/main.ts` obtained a real token from the development Auth0 tenant
  and called the agent route once. The answer was HTTP 200, `complete`:
  collect, research (5 SerpApi sources) and prioritize produced; browse
  skipped (`provider_absent`); hypothesize failed with `invalid_hypothesis`.
  The schema was validated and the report object built. Provenance:
  `diag.investigation.recorded` has the delegating admin as actor and
  `via_agent=mastra_sidecar`. Provider attempts: Gemini 1 (HTTP 200), SerpApi
  1 (HTTP 200). This run is the implementing agent's record; it was not
  repeated in the integration review. The `invalid_hypothesis` came from a
  prompt that did not state the output shape; commit `fd71d89` fixed the
  prompt, and one live investigation after the fix produced a hypothesis
  (1 evidence-linked claim; see SPONSOR-IMPLEMENTATION-MAP row 1). The Mastra
  path itself has not been re-run since the fix.
- **Stand-in, not evidence:** the delegator's code-host standing in the live
  run came from a `verified_read` stand-in, because the seeded repository is
  not a real GitHub repository. Code-host verification by the GitHub App is
  **NOT VERIFIED** on this path.
- Nothing is deployed. No sidecar process runs in `render.yaml`.

## What Mastra is (verified 2026-10-24)

- An open-source **TypeScript** framework for agents and workflows. Its
  workflows are typed steps (`createStep`/`createWorkflow`) with `.then`,
  `.branch`, `.parallel`, suspend/resume, and streaming. It runs on any
  Node.js-compatible host, either embedded in a Node app or as a Hono-based
  server exposing REST routes such as
  `POST /api/workflows/:workflowId/start-async`.
- Licence: Apache-2.0 for the core; anything under an `ee/` directory is
  source-available under the Mastra Enterprise License and needs a commercial
  licence for production use.
- There is no Python runtime. A Python service can use Mastra only by calling a
  separately deployed Node server over HTTP.

Sources: <https://mastra.ai/>, <https://mastra.ai/docs/workflows/overview>,
<https://mastra.ai/docs/server/overview>, <https://mastra.ai/reference/server/routes>,
<https://mastra.ai/docs/license>.

## What the product already has

The investigation workflow (goal 2; integration-tested over real tables) is a
typed, bounded, in-process stage pipeline: `collect → research → hypothesize →
prioritize`, then exactly one `diag.investigation.recorded` provenance event in
the same transaction discipline as every diagnosis event. Stage budgets
(`InvestigationBudget`) are frozen, validated values. A stage that cannot run
is recorded `skipped` with a short reason; an unexpected error is recorded
`failed` and never crashes the run. Authorization runs at the entry
(`repo.investigate`) and again inside the assist policy gate. The durable
authority for retries and leases is the existing outbox/jobs (ADR-012,
[DURABLE-JOBS](DURABLE-JOBS.md)).

## Comparison

| Mastra provides | What FlakePilot needs | Verdict |
|---|---|---|
| Typed steps and control flow | Four fixed stages and one provenance record; Python types already check them (mypy strict) | Covered |
| Suspend/resume, human-in-the-loop waits | No stage waits for a human; approvals live in `approval`, not in investigation | Not needed |
| Agent loops with tools | Forbidden by design: advisory models get no tools and no side effects (ADR-009, `AiAssistPort`) | Conflicts with the trust model |
| Memory and threads | Evidence is the stored run and history in PostgreSQL; model "memory" would be a second, unaudited store | Conflicts with ADR-005 |
| Workflow runners (built-in, Inngest) | Retries, leases, and dead-letters are already the outbox/jobs | Covered; a second authority |
| Observability | Structured JSON logs and request ids; provenance events | Covered |

## What adopting it would cost

1. **A second workflow authority.** A Node sidecar running Mastra workflows
   would own step retries and state alongside the Python outbox/jobs. Every
   correctness argument (idempotency, at-least-once delivery, provenance in
   the same transaction) would need interaction rules between the two. This
   is the same objection recorded for Temporal in
   [TEMPORAL-DURABLE-WORKFLOW-EVALUATION](TEMPORAL-DURABLE-WORKFLOW-EVALUATION.md).
2. **A second runtime and trust boundary.** A Node process on the evidence path
   would need its own credentials to reach evidence, or would receive evidence
   over HTTP from the control plane. Either way, the entry `authorize()` and the
   assist policy gate would have to be re-implemented or trusted across a
   process boundary. Today they are one call stack, tested together.
3. **Toolchain and supply chain.** Node, npm, a lockfile, and licence gating
   for a second ecosystem (ADR-010), plus the `ee/` licence boundary to police.
   ADR-017 decision 10 forbids new vendor dependencies this phase.
4. **No capability gain.** Nothing in the current scope needs what Mastra adds
   over the existing pipeline.

## Conditions to revisit

Any one of these, with written evidence, reopens the evaluation:

- The product adds a TypeScript/Node service of its own for another reason, so
  the runtime cost is already paid.
- Investigations need multi-step, tool-using agent loops, and the trust model
  (ADR-008/009) is formally amended to allow model-chosen tools.
- Investigations need long human-in-the-loop waits that outbox/jobs cannot
  express. The Temporal evaluation's conditions would then also apply.

## Status

Goal 2: **Integration-tested** (Python workflow core, unchanged). Mastra as
a workflow authority inside the product: **not adopted** (the 2026-10-24
analysis above). Mastra as an external, authority-free client:
**implemented** in `sidecars/mastra/`, **mock-tested**, and **live-verified
locally** on 2026-10-25 through the Auth0 agent route. It is not deployed.

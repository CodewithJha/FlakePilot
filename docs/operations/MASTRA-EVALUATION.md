# Mastra evaluation

Companion to [ADR-017](../adr/ADR-017-sponsor-capability-phase.md) decision 1
and sponsor goal 2 (Mastra). This is a read-only re-evaluation: nothing is
installed or wired. The decision stands: **Mastra is not adopted.** The goal
(agent-shaped, bounded investigation) is met by the Python workflow in
`diagnosis/application/investigation.py`.

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

Goal 2: **Integration-tested** (Python workflow core, unchanged by this
evaluation). Mastra runtime: **not adopted**. Nothing was implemented for
Mastra, because nothing justified it.

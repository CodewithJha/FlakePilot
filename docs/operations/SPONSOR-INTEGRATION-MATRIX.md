# Sponsor integration matrix — the 16 strategic goals

One evidence-backed tracking matrix for the sponsor goals, in the milestone
order the sponsors agreed. It replaces no existing artifact: ADR-003 (provider
adapters), PRODUCT-SCOPE P1–P6 (sponsor independence), and the
[stage-10K infrastructure registers](OWNER-DECISION-SHEET.md) remain the
governing documents. Statuses were verified against the repository at commit
`3d5b6b3` on 2026-10-24 by inspecting source, tests, `uv.lock` (44 packages,
no sponsor dependency), docs, and CI — a dependency declaration, a placeholder,
or related local functionality is **not** an integration, and nothing is back-
dated. One status per goal, from this fixed ladder:

**Planned → Implemented-untested → Tested-with-fakes → Integration-tested →
Live-verified → Deployed-and-verified**, plus **Blocked** (external
prerequisite missing) and **Not-justified** (wrong stage).

Standing posture (unchanged, enforced by ADR-008/ADR-013/ADR-014 and these
statuses): production untrusted-code execution stays disabled
(`ClosedSandbox`, `UnavailableIsolation`, `control_plane_reaper`,
`RefusedHostHandoff`); core features work with AI disabled (P6); nothing was
purchased; no account was created for any sponsor.

## The matrix

| # | Goal | Intended use | Status | Evidence (files / tests / CI) | Missing prerequisite | Next actionable step | Justified this stage? |
|---|---|---|---|---|---|---|---|
| 1 | **Gemma** (open-weight reasoning engine) | The `AiAssistPort` implementation behind `Assist` (redaction, allow-list policy, untrusted parts) | **Not-justified** | Port and fail-closed application core exist and are tested with fakes: `diagnosis/application/assist.py`, `integrations/infrastructure/ai.py` (`NullAiAssist`), `FakeAiAssist`; only `NullAiAssist` is wired (`interface/composition_root/__init__.py`); no model dependency in `uv.lock` | An owner decision on self-hosting vs a provider, a serving endpoint, and review of the P6 independence rule | ADR defining the assist provider adapter + fail-closed policy mapping to the existing `assist_policy` gate | No — core works AI-disabled; a P1-holder decision is required first |
| 2 | **Mastra** (agent orchestration) | Orchestrating diagnosis/repair agent flows across contexts | **Not-justified** | No occurrence in source, docs or lockfile (`grep`: none); orchestration today is the durable outbox worker (`orchestration/application/worker.py`, `docs/operations/DURABLE-JOBS.md`) | Frame: a decision whether an external agent runtime belongs inside the control plane (ADR-008 trust boundary) | Owner decision + ADR before any adapter work | No — wrong trust boundary for an unreviewed external agent runtime |
| 3 | **Browserbase** (reproducible browser runs) | Capture reproducible web-app reproduction evidence | **Not-justified** | No occurrence anywhere in the repo; artifacts are `LocalFsArtifactStore` plaintext; the execution plane is disabled by design (`UnavailableExecutionPlane`) | The execution-host milestones (10K: D-HOST-1..5) plus credentials/API decision | Nothing until an execution host exists | No — downstream of the execution host |
| 4 | **MongoDB Atlas** (investigation records) | Store investigation/assist records | **Not-justified** | System of record is PostgreSQL with per-context schemas and Alembic migrations (ADR-005); no Mongo occurrence anywhere | An owner data-layer decision superseding ADR-005 | None — do not add a second source of truth | No — contradicts ADR-005's single-source-of-record decision |
| 5 | **Temporal** (durable investigations) | Durable investigation workflows | **Not-justified** | Durable jobs are the database-backed outbox/lease worker, tested (`test_jobs_postgres.py`, `test_worker_postgres.py`, DURABLE-JOBS.md); no Temporal occurrence | An owner decision that the outbox is insufficient | Measure outbox usage first; a Temporal ADR only if it fails | No — the shipped mechanism solves the stated problem |
| 6 | **Tiger Data** (test reliability over time) | Historical flake/reliability analytics | **Not-justified** | Test history is stored in PostgreSQL (`ti.observations`) with retention owned by disabled policy (`integrations/application/retention.py`); no occurrence | Long-horizon analytics need accumulation policy + owner decision | Accumulate history; evaluate after 10K monitoring milestone | No |
| 7 | **Sentry** (agent tracing, observability) | Trace assist/agent flows | **Not-justified** | Observability is structured JSON logs with request IDs (`interface/http/request_context.py`, `flakepilot.http` logger); no sponsor SDK in the lockfile | Monitoring decision in the 10K plan (owner budget/region) | 10K monitoring work-package first | No |
| 8 | **SerpApi** (technical research) | Assist-time technical research for diagnosis | **Not-justified** | No occurrence; AI assist is disabled at the policy (`assist_policy`, `NullAiAssist`) | Anything depends on goal 1; also egress rules (ADR-008 default-deny) | Nothing until goal 1's ADR | No |
| 9 | **Auth0 for AI Agents** (identity/authorization) | Token exchange and agent-scoped authorization | **Not-justified** | IAM is an owned bounded context: sessions via GitHub App sign-in (ADR-009/ADR-011), grants via `decide()` intersection + `CodeHostAccessVerifier` (ADR-016); all tested incl. Postgres integration | A trust-model decision to externalize identity | Only after decision A/C of the grant-auth sheet | No — replacing a working verified-owned IAM is not justified by any test here |
| 10 | **Render** (hosted application) | Host the API for the sponsors' demo | **Planned** | Deployment today is "operator runs uvicorn locally" (`README.md`, BOUNDED-CONTEXTS infra section); no deploy scripts/hosts | Owner decision on hosting + credentials + 10K budget | A deployment ADR (host, database, artifact storage) | Later — after 10K infra decisions |
| 11 | **DigitalOcean** (isolated repair workers) | Hosts for execution-plane workers | **Planned** | 10K analysis names DigitalOcean among candidate hosts (`INFRASTRUCTURE-OPTIONS-ANALYSIS.md`); no account, no KVM instance anywhere; evidence levels stay `simulate < microVM` (`IsolationChecks` all false) | D-HOST-1..5 owner decisions (budget, region, isolation runtime) | Execute 10K WP-1 (host provisioning, read-only assessment first) | Yes — this is the execution host milestone's dependency, in pure assessment mode |
| 12 | **TabPFN** (failure prioritization) | Prioritize unstable tests for members | **Not-justified** | Classification is the deterministic `ti-classification/1` rule set (ADR-013), tested over real tables; no ML/no occurrence | Product decision that prioritization is a feature | Manual triage evidence reviews first | No |
| 13 | **Tinker** (specialized failure classification) | Fine-tuned classifier | **Not-justified** | Same as 12; `_UnusedIdentityProvider`-style fakes don't exist for any external classifier; no occurrence | Goals 12's decision, plus labeled-data volume | None this stage | No |
| 14 | **ElevenLabs** (spoken repair brief) | Voice output of repair reports | **Not-justified** | No occurrence; reporting today is operator JSON/HTML (`ci-run-report`) and server-rendered run view | No audio playback surface exists | Nothing until later | No |
| 15 | **Entire** (investigation provenance) | End-to-end provenance of an investigation | **Planned** | Provenance primitives exist and are tested: ADR-012 event envelope/outbox, `aud.events` relay (`aud_0003` admits `repo` channel), CI evidence uploaded with commit metadata (still `unchecked` re ancestry) | A design for linking evidence→verdict→report chains into one provenance record | Extend `aud` relay consumers when execution exists (10K) | Yes — provenance is already in the roadmap and grows with existing work |
| 16 | **`.TECH` domain** | Deployment domain (public origin, cookies) | **Planned** | Public origin is configurable (`settings.public_origin`, session cookies `__Host-`-prefixed); no domain purchased, nothing external touched | Owner approval for spending + a DNS decision | Buy after deployment ADR (goal 10) | No — nothing to deploy to yet |

## Sequencing recommendation (by dependency and evidence)

1. **Same stage as now**: 15 (provenance grows from the implemented outbox/audit
   work), 11 (assessment/decision work only), and mechanizing the staging GitHub
   App runbook — already done this sprint (`verify-github-app`).
2. **After the 10K owner decisions**: 10, 16 (deployment target fixes the domain
   decision), then 7 (monitoring), then 3/11 execution reality.
3. **Only after goal 1's ADR**: 8 and anything assist-adjacent (2 remains
   blocked on the trust-boundary question regardless).
4. **Not recommended to schedule**: 4, 5, 9 (all replace working, tested owned
   mechanisms without evidence of a gap) — revisit only with a concrete
   failure of the owned mechanism.

## Honesty notes

- **No sponsor endpoint exists anywhere in `main`**: no sponsor package in
  `uv.lock`, no sponsor SDK import, no sponsor configuration variable, no
  sponsor-branded test. Every status above follows from that plus the
  functionality that *does* exist.
- Live-external milestones achieved so far across the product: none — the
  GitHub App path is real and fail-closed but unverified live (ADR-016
  deviation 3; staging prerequisites listed in the grant-authorization sheet).
- This matrix updates per milestone; statuses change only when new repository
  evidence appears, never by assertion.
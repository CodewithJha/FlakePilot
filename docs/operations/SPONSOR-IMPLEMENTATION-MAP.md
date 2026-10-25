# Sponsor implementation map — capability, components, verification

Companion to [ADR-017](../adr/ADR-017-sponsor-capability-phase.md) and the
[frozen matrix](SPONSOR-INTEGRATION-MATRIX.md). One row per original sponsor
goal: what it is in the product today, what implements it, whether the running
app uses it, the evidence, and the status. Statuses change only with new
evidence.

Standing rules for every row: deterministic `ti-classification/1` stays the
authoritative classifier; model and provider output is advisory data that
never grants permissions, replaces verdicts, or executes anything; every
external provider is optional — with all of them absent, the product remains
the released local-only candidate and makes no hosted call.

Status vocabulary, lowest to highest, each implying the ones before it only
where stated:

- **implemented** — the code exists.
- **wired** — the composition root uses it (opt-in where a key is needed).
- **mock-tested** — unit tests over injected transports, recorded provider
  answers, or fakes.
- **real-PG-tested** — integration tests over real PostgreSQL tables (run
  locally on PostgreSQL 16 and in CI).
- **live-verified** — exercised against the real provider. Rows 1, 7, 8 and
  14 have this for the adapter, from a developer machine on 2026-10-24; on
  2026-10-25 rows 1 (the hypothesize stage), 4 (Atlas), 9 (the agent route)
  and 12 (the predictor on synthetic rows; the backfill) did too (see
  [the record below](#live-verification-record-2026-10-24)). Row 5 ran
  against a real *local* Temporal dev server, which is not the hosted
  service; nothing else.
- **deployed** — running on real infrastructure: since 2026-10-25 the
  control plane runs on Render at `https://app.flakepilot.tech`
  ([deployment record](#deployment-record-2026-10-25)). Rows 1, 2, 8, 10, 14
  and 16 were exercised there; rows 7 and 9 are configured there but not
  exercised; execution stays closed.
- **blocked** — needs a key, infrastructure, or an owner decision that is not
  available.
- **not justified** — evaluated and not adopted, with revisit conditions.

## The map

| # | Goal | Capability today | Components | Wired? | Evidence | Status |
|---|---|---|---|---|---|---|
| 1 | **Gemma** | Advisory hypotheses through the `Assist` policy gate, from an operator's local endpoint or hosted Gemma on the Gemini API. Intent (request row + `diag.ai_request.sent`) commits before any model call | `integrations/infrastructure/gemma.py`, `hosted_http.py`, `model_endpoint.py`; `diagnosis/application/assist.py` | Yes, opt-in: `FLAKEPILOT_GEMINI_API_KEY`+`_MODEL` or `FLAKEPILOT_AI_ENDPOINT`; used by the investigation's hypothesize stage once an org enables AI policy | Adapter: unit tests over recorded answers (auth, quota, retries, fences, truncation, key never leaks). Assist ordering: unit + real-PG test with a raising/hanging provider | implemented, wired, mock-tested, real-PG-tested (assist); adapter **live-verified** 2026-10-24 on `gemma-4-31b-it` (schema-valid answer, invalid key, timeout); the hypothesize stage is **live-verified** 2026-10-25 through one real investigation on a throwaway database (`tests/live/test_live_investigation_gemma.py`, `gemma-4-26b-a4b-it` unless overridden): `produced`, 1 evidence-linked claim, 0 unverified, after the prompt was made to state the exact JSON shape (commit `fd71d89`; earlier real answers failed as `invalid_hypothesis`). One Gemini call, no SerpApi call. **Deployed** 2026-10-25: with the owner's approval of the Gemini data flow (Gemini API Gemma is free-tier only; free-tier prompts may be used by Google), `set-ai-policy --level evidence_only` was run against the deployed database, and one investigation from the signed-in browser on `app.flakepilot.tech` returned 200 `complete` with hypothesize `produced` (2 evidence-linked claims, 0 unverified, 0 contradicted) |
| 2 | **Mastra** | Bounded investigation workflow in Python (collect → research → browse → hypothesize → prioritize → one provenance row), exposed to session users and, since 2026-10-25, to bound agents; plus `sidecars/mastra/`, a Node Mastra workflow (`@mastra/core` 1.79.0, `workflows` entry point only) that calls the agent route as an authority-free client | `diagnosis/application/investigation.py`; `interface/composition_root/investigations.py`, `agent_investigations.py`; `interface/http/investigations.py`, `agent_investigations.py`; `sidecars/mastra/` | Python workflow: yes. Sidecar: a separate process nothing deploys | Workflow: unit + real-PG. Sidecar: `npm ci && npm test` (tsc + 8 `node:test` cases over mocked Auth0 and FlakePilot), **VERIFIED** in the integration review | Python workflow implemented, wired, real-PG-tested, **deployed** (the session route returned 200 `complete` on `app.flakepilot.tech` on 2026-10-25); Mastra sidecar implemented, mock-tested, not deployed; a local live run through the agent route is the implementing agent's record (hypothesize then failed `invalid_hypothesis`, since fixed) and was **not repeated** after the prompt fix; Mastra as an in-product workflow authority **not justified** ([MASTRA-EVALUATION](MASTRA-EVALUATION.md)) |
| 3 | **Browserbase** | Browser-evidence port (exact-host allowlist of public names, redirects refused, budgets) and a Browserbase Fetch adapter; the browse stage captures allowlisted research sources as hashed evidence whose text never reaches the model | `integrations/application/browser.py`, `integrations/infrastructure/browserbase.py`, browse stage in `investigation.py` | Yes, opt-in: `FLAKEPILOT_BROWSERBASE_API_KEY`+`FLAKEPILOT_BROWSER_ALLOWLIST` | Unit over recorded Fetch answers; browse stage unit tests (allowlist, skips, crash); real-PG investigation test covers the absent-provider skip | implemented, wired, mock-tested; **blocked** live: no key ([BROWSERBASE-EVIDENCE](BROWSERBASE-EVIDENCE.md)) |
| 4 | **MongoDB Atlas** | Opt-in, write-only export of classified run reports (IDs, counts, verdicts with evidence; no test names, upload metadata or artifact keys), upsert-only key `org:repo:run:sha256(document)`, audited per report; PostgreSQL stays the system of record and Atlas is never read back | `integrations/infrastructure/atlas.py`, `test_intelligence/application/report_export.py`, `composition_root/atlas_export.py` | Yes, opt-in: `FLAKEPILOT_ATLAS_URI`+`_DATABASE`, operator command `export-reports-atlas` only (API process and worker never import the driver) | Unit (fake collection, TLS-weakening URIs refused, short reason codes, no names exported) + real-PG (six backfilled runs exported twice: 6 inserted, 6 unchanged, 12 events). Live 2026-10-25: adapter round trip (`tests/live/test_live_atlas.py`) and the operator command on one real backfilled run (inserted, then unchanged, then deleted) | implemented, wired (operator only), mock-tested, real-PG-tested, **live-verified** 2026-10-25 against the owner's cluster; not deployed. Licence gate unblocked by the owner adding `ISC` ([MONGODB-ATLAS-EVALUATION](MONGODB-ATLAS-EVALUATION.md)) |
| 5 | **Temporal** | Durability stays the outbox/jobs. Opt-in proof of concept: one Temporal workflow runs one bounded investigation through an activity; FlakePilot's advisory lock and existing `diag.investigation.recorded` row stay authoritative; retries only for transient reason codes; cancellation propagates | `composition_root/temporal_workflow.py`, `temporal_investigation.py` | Opt-in operator commands only (`temporal-worker`, `temporal-run-investigation`, `temporal-cancel-investigation`; `FLAKEPILOT_TEMPORAL_*`); never the default worker or the API process | CI: the activity on real PG via `ActivityEnvironment` (run once, replay, lock contention retryable, reason codes, locks released). **Real Temporal server (local dev), not Temporal Cloud** (`tests/live/test_live_temporal_local.py`, Temporal CLI 1.9.1 / Server 1.32.0): one workflow, duplicate start rejected; cancellation records nothing; a worker SIGKILLed mid-activity, then replaced, yields exactly one recorded investigation | implemented, opt-in, real-PG-tested, verified against a real local Temporal dev server; Temporal Cloud **blocked** (the trial needs a payment card; no credentials); code-host standing from a stand-in in those tests; not deployed ([TEMPORAL-DURABLE-WORKFLOW-EVALUATION](TEMPORAL-DURABLE-WORKFLOW-EVALUATION.md)) |
| 6 | **Tiger Data** | Reliability analytics on plain PostgreSQL: per-test history, per-environment counts, failure clusters, day/week failure and flaky trends against the previous window; `reliability-trend` operator command | `test_intelligence/application/reliability.py`, `TiStore.failure_trend`, `composition_root/analytics.py` | Yes (operator command; the investigation's prioritize stage uses the history report) | Unit + real-PG tests (buckets, scoping, previous-window comparison, command output) | implemented, wired, real-PG-tested; on the owner's Tiger Cloud service (2026-10-25, PostgreSQL 18.6): `alembic upgrade head` + `check` and `reliability-trend` (empty pair) **VERIFIED**; the unmodified `?sslmode=require` URL now connects (`create_engine` translates it; TLS confirmed); full integration suite **blocked**: the service allows only database `tsdb`, and `tsdb_admin.allowed_databases` is a superuser-context setting the owner's `tsdbadmin` role cannot set (`permission denied`); `tsdb` is at `iam_0003`, so `ti_0003` is pending there (not applied); hosting there stays an owner decision ([TIGER-DATA-EVALUATION](TIGER-DATA-EVALUATION.md)) |
| 7 | **Sentry** | Standard-library reporter: allow-listed events (exception class, frames, message template, request-id/route tags), API process only | `interface/composition_root/error_reporting.py`, `integrations/infrastructure/sentry.py` | Yes, opt-in: `FLAKEPILOT_SENTRY_DSN` (not the worker) | Unit tests capture the outbound envelope and assert seeded secrets never appear | implemented, wired, mock-tested; envelope transport **live-verified** 2026-10-24: one synthetic allow-listed event accepted (`tests/live/test_live_sentry.py`); configured on the deployment (startup log `error_reporting=sentry`), no event from it observed ([SENTRY-OBSERVABILITY-BOUNDARY](SENTRY-OBSERVABILITY-BOUNDARY.md)) |
| 8 | **SerpApi** | Research stage: one Google search per investigation (query = failing test names), https sources only, deduplicated, budgeted, capped | `integrations/infrastructure/serpapi.py`; fixture `research.py` | Yes, opt-in: `FLAKEPILOT_SERPAPI_API_KEY` | Unit over a recorded SerpApi response (budget, dedup, quota vs rate limit, key never leaks) | implemented, wired, mock-tested; **live-verified** 2026-10-24: adapter search, invalid key, and the research stage of the live investigation route; **deployed**: the research stage produced 5 sources in both investigations run on `app.flakepilot.tech` on 2026-10-25 |
| 9 | **Auth0 for AI Agents** | Delegated agent principals with attributed provenance; an RS256 JWKS verifier whose tokens only narrow; admin-made bindings (`bind-agent`/`unbind-agent`, subject stored as SHA-256, ≤8 h, `repo.view`/`repo.investigate` only); `POST /agent/orgs/{org}/runs/{run}/investigation` for bound agents, bearer only | `identity/application/agents.py`, `agent_tokens.py`, `agent_bindings.py`; `integrations/infrastructure/auth0.py`; `composition_root/agent_investigations.py`, `agent_binding_commands.py`; `interface/http/agent_investigations.py`; migration `iam_0003` | Yes, opt-in: the route is mounted only when `FLAKEPILOT_AUTH0_DOMAIN`+`_AUDIENCE` are set (`provider-status` reports `auth0`); bindings by operator command | Unit (binding rules, every refusal, non-disclosure parity) + real-PG (storage, audit, revocation, schema caps, CLI, provenance attribution). Live 2026-10-25, re-run in the integration review (`tests/live/test_live_auth0_route.py`): a real client-credentials token through the route in-process on a throwaway database → 200 `complete`, a tampered copy → 401, after `unbind` → 404 | implemented, wired (opt-in), mock-tested, real-PG-tested, **live-verified** locally 2026-10-25; the delegator's code-host standing came from a `verified_read` stand-in, so the GitHub App verifier on this path is **NOT VERIFIED**; the route is mounted on the deployment (startup log `agent_token_issuer=auth0`) but **not exercised there** ([AUTH0-AGENT-ROUTE](AUTH0-AGENT-ROUTE.md)) |
| 10 | **Render** | The control plane hosted on Render: one digest-pinned image for API and worker; API + worker + PostgreSQL 16 (no public connections) in `singapore`, alembic pre-deploy, liveness health check, 1 GB artifact disk, auto-deploy after CI passes; execution closed | `Dockerfile`, `.dockerignore`, `render.yaml`, [DEPLOYMENT-RUNBOOK](DEPLOYMENT-RUNBOOK.md) | Yes: `flakepilot-api` (`0.5c-512mb`), `flakepilot-worker` (`0.5c-512mb`, database URL only), `flakepilot-db` (`0.1c-256mb`); ≈ USD 20.25/month at list prices | Render builds and deploys (resources created through the Render API, so Blueprint sync is not exercised); smoke tests 2026-10-25: health, migrations, fail-closed sign-in, HTTPS ingestion with replay and conflict, reports, worker outbox delivery; the first deploy exposed two `render.yaml` defects (quoted `dockerCommand`; artifact-dir validation in pre-deploy), both fixed | implemented, **deployed** 2026-10-25 |
| 11 | **DigitalOcean** | Execution-isolation host assessment; droplet ceiling `dedicated_host_container` | [DIGITALOCEAN-ISOLATION-ASSESSMENT](DIGITALOCEAN-ISOLATION-ASSESSMENT.md) | — | Assessment only | **blocked**: owner infrastructure and security reviewer |
| 12 | **TabPFN** | Deterministic prioritization baseline; a leakage-safe evaluation harness (temporal split, past-only features, precision@k, AUC, Brier, base rate); a TabPFN predictor that sends only numeric rows and labels; `evaluate-prioritization` command; `backfill-ci-history` imports GitHub Actions JUnit artifacts with their original CI time, and the evaluation and trends read that time | `prioritization.py`, `evaluation.py`, `backfill.py`, `test_intelligence/infrastructure/tabpfn.py`, `integrations/infrastructure/github_actions.py`, `composition_root/analytics.py`, `composition_root/operator.py` | Yes, opt-in: `FLAKEPILOT_TABPFN_API_KEY` **and** `--with-tabpfn`; otherwise the candidate is reported `blocked`. Backfill: operator only, needs the GitHub App (actions:read) | Baseline: unit + real-PG. Harness: unit (leakage, metrics). Predictor: unit over recorded API flow. Command: real-PG test. Backfill: unit (zip safety, redirect, idempotency) + real-PG (provenance, re-import no-op, CI-time split) | implemented, wired (operator only), mock-tested, real-PG-tested (baseline, command, backfill); hosted predictor **live-verified** 2026-10-25 on synthetic rows only: one fit+predict, invalid key → `provider_unauthorized` (`tests/live/test_live_tabpfn.py`); FlakePilot's CI uploads `junit-gates` since commit `4c1bf81`, and the backfill is **live-verified** 2026-10-25 end to end on a throwaway database: 1 run imported (CI run 40170825392, 1397 tests, original completion time kept), re-import a duplicate (`tests/live/test_live_backfill.py` passed); the evaluation needs three windows of at least a day, so it becomes possible after about three days of CI runs and has **not run**; selection **not justified** until a recorded evaluation beats the baseline ([TABPFN-PRIORITIZATION-EVALUATION](TABPFN-PRIORITIZATION-EVALUATION.md)) |
| 13 | **Tinker** | Verdict authority stays `ti-classification/1`; no fine-tuning pipeline or adapter in the product | — (one experiment outside the repository) | — | 2026-10-25, live on Tinker with **synthetic** data labelled by `ti-classification/1`: Qwen3-8B rank-8 LoRA, 270 train / 50 test, 5 classes; majority 0.20 accuracy / 0.067 macro-F1, zero-shot 0.28 / 0.177, LoRA 0.70 / 0.705 (the rule set is 1.0 by construction); ≈ USD 0.15 estimated, billing not yet confirmed | evaluated live (synthetic only); **not adopted** as authority; product use **not justified** ([TINKER-EVALUATION](TINKER-EVALUATION.md)) |
| 14 | **ElevenLabs** | Explicit-request spoken brief: `POST …/investigation/brief` composes a ≤1200-byte counts-only brief and returns MP3 unstored; never automatic | `diagnosis/application/brief.py`, `integrations/infrastructure/elevenlabs.py`, the brief route | Yes, opt-in: `FLAKEPILOT_ELEVENLABS_API_KEY`+`_VOICE_ID` (503 `speech_not_configured` otherwise) | Unit: brief content (no repository text), MP3 validation, retries, route authorization and refusal codes | implemented, wired, mock-tested; **live-verified** 2026-10-24: adapter speech, invalid key, and `POST …/investigation/brief` returning a decodable MP3; **deployed**: the brief route returned 200 `audio/mpeg` (326,678 bytes) on `app.flakepilot.tech` on 2026-10-25 |
| 15 | **Entire** | Local provenance: one `diag.investigation.recorded` per investigation (stopping condition, stage/source/page/link counts, agent attribution) | `investigation.py` `_provenance`, diag outbox | Yes | Unit + real-PG (one row per outcome, refusals included) | local provenance implemented, wired, real-PG-tested, part of the deployed investigation route (the provenance rows were not inspected on the deployment); Entire attestation **not justified**: no runtime API, and its commit trailers conflict with AGENTS.md commit rules ([ENTIRE-PROVENANCE](ENTIRE-PROVENANCE.md)) |
| 16 | **`.TECH` domain** | `flakepilot.tech` registered 2026-10-25; `app.flakepilot.tech` serves the deployment; the origin stays configurable | [DOT-TECH-DOMAIN-STRATEGY](DOT-TECH-DOMAIN-STRATEGY.md), `FLAKEPILOT_PUBLIC_ORIGIN` in `render.yaml` | Yes: the public origin, GitHub App callback and homepage | RDAP registration; `CNAME app → flakepilot-api.onrender.com`; Render domain verified; Google Trust Services certificate valid to 2027-01-23; the owner signed in with GitHub through it | **deployed** 2026-10-25 |

## Live verification record (2026-10-24)

Run from the owner's development machine with keys the owner authorized from
another of their projects; no key, account id, or provider text is recorded
here. Prompts, queries and fixtures were synthetic. Nothing was deployed.

| Provider | Requests and outcomes |
|---|---|
| Gemma (Gemini API) | `GET v1beta/models`: `gemma-4-31b-it` and `gemma-4-26b-a4b-it` listed with `generateContent`. `generateContent` on `gemma-4-31b-it` through `GemmaAssist` (`tests/live`): answer parsed and schema-valid. Invalid key: HTTP 400 `API_KEY_INVALID` → `provider_auth_failed`. 1-second timeout → `provider_timeout`. `gemma-4-26b-a4b-it` was not called |
| SerpApi | `GET search.json` (`engine=google`) through `SerpApiResearch` (`tests/live`): 1–3 deduplicated https sources. Invalid key: HTTP 401 → `provider_auth_failed`. Timeout not exercised live (search quota) |
| ElevenLabs | `GET v1/voices` to pick a premade voice. `POST v1/text-to-speech/{voice}` with `eleven_flash_v2_5`, `mp3_44100_64` through `ElevenLabsSpeech` (`tests/live`): `audio/mpeg`. Invalid key: HTTP 401 → `unauthorized`. Timeout not exercised live |
| Sentry | One envelope `POST` of a synthetic event (template "flakepilot live verification", fake ids) built by `SentryHandler`: HTTP 200 with the same event id echoed back |
| Auth0 (2026-10-25) | One client-credentials `POST /oauth/token` on a development tenant for an M2M application granted `repo.view repo.investigate`; the token verified through `Auth0TokenVerifier` (tenant JWKS) with exactly those scopes and narrowed by `agent_tokens.narrow` without widening (`tests/live`). The same token was refused by a verifier for another audience and with one signature character changed. (At the time no route accepted agent tokens; see the Auth0 route row below) |
| Gemma investigation (2026-10-25) | One investigation of a seeded flaky run on a throwaway PostgreSQL 16 database, as the org admin, org AI policy `evidence_only` (`tests/live/test_live_investigation_gemma.py`): collect, hypothesize and prioritize `produced`; research and browse skipped (`provider_absent`); assist `produced`, 1 evidence-linked claim, 0 unverified. One Gemini request; no SerpApi request |
| Auth0 route (2026-10-25) | `tests/live/test_live_auth0_route.py`, re-run in the integration review: a real client-credentials token through `POST /agent/orgs/{org}/runs/{run}/investigation` in-process → 200 `complete`; tampered → 401; after `unbind-agent` → 404. Code-host standing from a stand-in |
| MongoDB Atlas (2026-10-25) | From the owner's allow-listed IP: `tests/live/test_live_atlas.py` (synthetic report inserted, re-upsert unchanged, read back, deleted) and `export-reports-atlas` on one real backfilled run (inserted, then unchanged; the document then deleted) |
| Temporal, local dev server (2026-10-25) | `tests/live/test_live_temporal_local.py` with `WorkflowEnvironment.start_local()` (Temporal CLI 1.9.1, Server 1.32.0): one workflow and a rejected duplicate; a cancelled workflow; a worker SIGKILLed mid-activity, attempt 2 on a replacement, exactly one recorded investigation. Not Temporal Cloud |
| TabPFN (2026-10-25) | The documented `/tabpfn/*` flow through `TabPfnPredictor` and `hosted_http.call` (`tests/live`): one fit (20 synthetic rows, two features) and one predict (5 rows), a probability in [0, 1] per row, the two separable groups ordered correctly. Invalid key: HTTP 401 → `provider_unauthorized`. Synthetic rows prove the plumbing, not prioritization quality; `evaluate-prioritization` was not run (no stored multi-window history exists) |

End to end, on a throwaway PostgreSQL 16 with the migrations applied:
`provider-status`, `POST /ingest/junit` (the synthetic demo fixtures),
`list-runs` and `ci-run-report` ran against the shipped composition. The
shipped HTTP investigation stops at 503 `auth_not_configured`: sign-in needs a
GitHub App, and the code-host verifier needs its installation credentials.
The routes were then served over real HTTP by the real `create_app` with two
stand-ins only: a synthetic GitHub sign-in identity and a code-host verifier
answering `verified_read`. The org AI policy was set in-process because no
route or command sets it.

- `POST /orgs/{org}/runs/{run}/investigation` returned 200, `complete`:
  collect, research (5 https sources from SerpApi) and prioritize produced;
  browse skipped (`provider_absent`); hypothesize skipped with
  `provider_http_error` (Gemma answered 408 or 5xx after the bounded retries;
  the cause was not investigated, to stay within quota).
- `POST …/investigation/brief` (ElevenLabs only configured) returned 200
  `audio/mpeg`, `Cache-Control: no-store`, a mono 44.1 kHz 64 kbps MP3 that
  `afinfo` and `ffprobe` decode, 92.6 s long. The brief ends with the report
  URL, and its two UUIDs are read out character by character.

### GitHub App, Tiger Cloud, Atlas (2026-10-25)

- **GitHub App** (installed only on `CodewithJha/FlakePilot`; actions,
  contents and metadata read). Installation tokens were minted for
  contents:read (one `GitHubCodeHost.confirm_commit` of the pushed `main`
  SHA) and for actions:read (`backfill-ci-history`'s listing of 50
  completed runs and their artifacts: 0 artifacts at the time). After
  FlakePilot's CI began uploading `junit-gates`, `backfill-ci-history`
  minted actions:read again, downloaded one real artifact through the
  storage redirect and imported it (row 12). The first live attempt
  failed and exposed two minter defects, now fixed. GitHub answers token
  creation with 201, which the minter refused because it expected 200, and
  tokens are about 390 characters, against a 255-character shape cap. The
  session-view verifier was first exercised live on the deployment on
  2026-10-25 (below); `verify-github-app` was not run.
- **Tiger Cloud**: migrations and `reliability-trend` ran, and the
  `sslmode=require` URL now connects through `create_engine`; the full suite
  cannot run there (row 6).
- **Atlas**: see row 4 and the table above.

## Deployment record (2026-10-25)

Render, region `singapore`; details, defects and rollback in
[DEPLOYMENT-RUNBOOK](DEPLOYMENT-RUNBOOK.md#deployed-state-2026-10-25). No key,
connection string, client id or account id is recorded here.

- **Deployed:** `flakepilot-api` (GitHub App, Gemini, SerpApi, ElevenLabs,
  Sentry and Auth0 configured; Browserbase and TabPFN not),
  `flakepilot-worker` (database URL, log level and closed execution mode
  only), `flakepilot-db` (no public connections). Execution stays closed.
- **Domain:** `https://app.flakepilot.tech` (row 16); sign-in starts at
  `/auth/github/login`.
- **Sign-in and run view:** the owner signed in with GitHub and the run view
  of a stored run loaded, the first live check of the session-view code-host
  verifier.
- **Investigation, before AI policy:** 200 `complete`; research 5 sources,
  browse `provider_absent`, hypothesize skipped `policy_disabled`.
- **Spoken brief:** 200 `audio/mpeg`, 326,678 bytes.
- **Investigation, after `set-ai-policy --level evidence_only`:** run once
  from the signed-in browser (same-origin `fetch`): 200 `complete`; collect,
  research (5 sources), hypothesize and prioritize `produced`, browse
  `provider_absent`; assist `produced`, 2 linked, 0 unverified,
  0 contradicted; about 111 s. The adapter does not log retries, so the number
  of Gemini attempts behind it (1 to 3) is not recorded.

## What the owner must supply before anything else moves to live-verified

- Keys (each enables only its own provider; see README "Hosted providers"):
  `FLAKEPILOT_GEMINI_API_KEY` + `FLAKEPILOT_GEMINI_MODEL`,
  `FLAKEPILOT_SERPAPI_API_KEY`,
  `FLAKEPILOT_BROWSERBASE_API_KEY` + `FLAKEPILOT_BROWSER_ALLOWLIST`,
  `FLAKEPILOT_ELEVENLABS_API_KEY` + `FLAKEPILOT_ELEVENLABS_VOICE_ID`
  (optional `FLAKEPILOT_ELEVENLABS_MODEL`),
  `FLAKEPILOT_TABPFN_API_KEY`, `FLAKEPILOT_SENTRY_DSN`,
  `FLAKEPILOT_AUTH0_DOMAIN` + `FLAKEPILOT_AUTH0_AUDIENCE` (the agent route),
  `FLAKEPILOT_ATLAS_URI` + `FLAKEPILOT_ATLAS_DATABASE`,
  `FLAKEPILOT_TEMPORAL_ADDRESS` + `_NAMESPACE` (+ `_API_KEY` for Cloud).
  The live tests read separate `FLAKEPILOT_LIVE_*` variables
  (`tests/live/`) so a test run never picks up a deployment key. Still
  missing for live verification: a Browserbase key and Temporal Cloud
  credentials (the trial needs a payment card).
- Data-flow consent per provider (what leaves the process is listed in the
  README table), including the Gemini free-tier decision.
- Isolation infrastructure with a named reviewer (goal 11). Hosting and the
  domain (goals 10, 16) were decided and deployed on 2026-10-25.

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
  14 have this for the adapter, from a developer machine on 2026-10-24, and
  rows 9 (the token verifier) and 12 (the predictor) on 2026-10-25 (see
  [the record below](#live-verification-record-2026-10-24)); nothing else.
- **deployed** — running on real infrastructure. **No row has this.**
- **blocked** — needs a key, infrastructure, or an owner decision that is not
  available.
- **not justified** — evaluated and not adopted, with revisit conditions.

## The map

| # | Goal | Capability today | Components | Wired? | Evidence | Status |
|---|---|---|---|---|---|---|
| 1 | **Gemma** | Advisory hypotheses through the `Assist` policy gate, from an operator's local endpoint or hosted Gemma on the Gemini API. Intent (request row + `diag.ai_request.sent`) commits before any model call | `integrations/infrastructure/gemma.py`, `hosted_http.py`, `model_endpoint.py`; `diagnosis/application/assist.py` | Yes, opt-in: `FLAKEPILOT_GEMINI_API_KEY`+`_MODEL` or `FLAKEPILOT_AI_ENDPOINT`; used by the investigation's hypothesize stage once an org enables AI policy | Adapter: unit tests over recorded answers (auth, quota, retries, fences, truncation, key never leaks). Assist ordering: unit + real-PG test with a raising/hanging provider | implemented, wired, mock-tested, real-PG-tested (assist); adapter **live-verified** 2026-10-24 on `gemma-4-31b-it` (schema-valid answer, invalid key, timeout); the hypothesize stage inside the live route is **NOT VERIFIED** (the provider answered a retryable HTTP error). Production use still needs the owner's data-flow decision (Gemini API Gemma is free-tier only; free-tier prompts may be used by Google) |
| 2 | **Mastra** | Bounded investigation workflow: collect → research → browse → hypothesize → prioritize → one provenance row, with budgets, recorded skips, and stopping conditions; exposed as `POST /orgs/{org}/runs/{run}/investigation` | `diagnosis/application/investigation.py`; `interface/composition_root/investigations.py`; `interface/http/investigations.py` | Yes | Workflow: unit + real-PG integration (stage choreography, provenance rows, refusals recorded). Route: unit tests for session, CSRF, `repo.investigate`, code-host standing, non-disclosure | implemented, wired, mock-tested (route), real-PG-tested (workflow); Mastra runtime **not justified** ([MASTRA-EVALUATION](MASTRA-EVALUATION.md)) |
| 3 | **Browserbase** | Browser-evidence port (exact-host allowlist of public names, redirects refused, budgets) and a Browserbase Fetch adapter; the browse stage captures allowlisted research sources as hashed evidence whose text never reaches the model | `integrations/application/browser.py`, `integrations/infrastructure/browserbase.py`, browse stage in `investigation.py` | Yes, opt-in: `FLAKEPILOT_BROWSERBASE_API_KEY`+`FLAKEPILOT_BROWSER_ALLOWLIST` | Unit over recorded Fetch answers; browse stage unit tests (allowlist, skips, crash); real-PG investigation test covers the absent-provider skip | implemented, wired, mock-tested; **blocked** live: no key ([BROWSERBASE-EVIDENCE](BROWSERBASE-EVIDENCE.md)) |
| 4 | **MongoDB Atlas** | No second store; investigation outputs are composed values plus outbox provenance | ADR-005 posture | — | Evaluation doc | **not justified** ([MONGODB-ATLAS-EVALUATION](MONGODB-ATLAS-EVALUATION.md)) |
| 5 | **Temporal** | Durability stays the outbox/jobs (leases, retries, dead-letter) | `orchestration` | — | Existing integration suite | **not justified** ([TEMPORAL-DURABLE-WORKFLOW-EVALUATION](TEMPORAL-DURABLE-WORKFLOW-EVALUATION.md)) |
| 6 | **Tiger Data** | Reliability analytics on plain PostgreSQL: per-test history, per-environment counts, failure clusters, day/week failure and flaky trends against the previous window; `reliability-trend` operator command | `test_intelligence/application/reliability.py`, `TiStore.failure_trend`, `composition_root/analytics.py` | Yes (operator command; the investigation's prioritize stage uses the history report) | Unit + real-PG tests (buckets, scoping, previous-window comparison, command output) | implemented, wired, real-PG-tested; Tiger Cloud itself **not justified** ([TIGER-DATA-EVALUATION](TIGER-DATA-EVALUATION.md)) |
| 7 | **Sentry** | Standard-library reporter: allow-listed events (exception class, frames, message template, request-id/route tags), API process only | `interface/composition_root/error_reporting.py`, `integrations/infrastructure/sentry.py` | Yes, opt-in: `FLAKEPILOT_SENTRY_DSN` (not the worker) | Unit tests capture the outbound envelope and assert seeded secrets never appear | implemented, wired, mock-tested; envelope transport **live-verified** 2026-10-24: one synthetic allow-listed event accepted (`tests/live/test_live_sentry.py`) ([SENTRY-OBSERVABILITY-BOUNDARY](SENTRY-OBSERVABILITY-BOUNDARY.md)) |
| 8 | **SerpApi** | Research stage: one Google search per investigation (query = failing test names), https sources only, deduplicated, budgeted, capped | `integrations/infrastructure/serpapi.py`; fixture `research.py` | Yes, opt-in: `FLAKEPILOT_SERPAPI_API_KEY` | Unit over a recorded SerpApi response (budget, dedup, quota vs rate limit, key never leaks) | implemented, wired, mock-tested; **live-verified** 2026-10-24: adapter search, invalid key, and the research stage of the live investigation route |
| 9 | **Auth0 for AI Agents** | Delegated agent principals (TTL-bounded, repo-scoped subsets of a verified member) with attributed provenance; an RS256 JWKS verifier whose tokens can only narrow a delegation | `identity/application/agents.py`, `agent_tokens.py`, `integrations/infrastructure/auth0.py` | Delegation: the workflow attributes an `AgentAccess` caller, but no route creates one. Verifier: **not wired** — no route accepts agent tokens; `FLAKEPILOT_AUTH0_*` are parsed only and `provider-status` says `auth0_not_wired` | Delegation: unit + real-PG. Verifier: unit (alg confusion, kid, iss/aud, expiry, JWKS refresh bounds) | delegation implemented, real-PG-tested; verifier implemented, mock-tested, **live-verified** 2026-10-25 against a development tenant: one client-credentials token verified and narrowed to `repo.view`+`repo.investigate`, wrong audience and tampered signature refused (`tests/live/test_live_auth0.py`); verifier still not wired; **blocked**: agent HTTP surface not designed |
| 10 | **Render** | One digest-pinned image for API and worker; Blueprint for API + worker + PostgreSQL 16 with alembic pre-deploy and liveness health check; runbook | `Dockerfile`, `.dockerignore`, `render.yaml`, [DEPLOYMENT-RUNBOOK](DEPLOYMENT-RUNBOOK.md) | — | `docker build` not run (no daemon); Blueprint not validated by Render | implemented; **blocked**: owner hosting decision (D-HOST-1..5), no account; not built, not deployed |
| 11 | **DigitalOcean** | Execution-isolation host assessment; droplet ceiling `dedicated_host_container` | [DIGITALOCEAN-ISOLATION-ASSESSMENT](DIGITALOCEAN-ISOLATION-ASSESSMENT.md) | — | Assessment only | **blocked**: owner infrastructure and security reviewer |
| 12 | **TabPFN** | Deterministic prioritization baseline; a leakage-safe evaluation harness (temporal split, past-only features, precision@k, AUC, Brier, base rate); a TabPFN predictor that sends only numeric rows and labels; `evaluate-prioritization` command | `prioritization.py`, `evaluation.py`, `test_intelligence/infrastructure/tabpfn.py`, `composition_root/analytics.py` | Yes, opt-in: `FLAKEPILOT_TABPFN_API_KEY` **and** `--with-tabpfn`; otherwise the candidate is reported `blocked` | Baseline: unit + real-PG. Harness: unit (leakage, metrics). Predictor: unit over recorded API flow. Command: real-PG test | implemented, wired (operator only), mock-tested, real-PG-tested (baseline, command); hosted predictor **live-verified** 2026-10-25 on synthetic rows only: one fit+predict, invalid key → `provider_unauthorized` (`tests/live/test_live_tabpfn.py`); no evaluation on real history yet; selection **not justified** until a recorded evaluation beats the baseline ([TABPFN-PRIORITIZATION-EVALUATION](TABPFN-PRIORITIZATION-EVALUATION.md)) |
| 13 | **Tinker** | Verdict authority stays `ti-classification/1`; no fine-tuning pipeline | — | — | Evaluation doc | **not justified** ([TINKER-EVALUATION](TINKER-EVALUATION.md)) |
| 14 | **ElevenLabs** | Explicit-request spoken brief: `POST …/investigation/brief` composes a ≤1200-byte counts-only brief and returns MP3 unstored; never automatic | `diagnosis/application/brief.py`, `integrations/infrastructure/elevenlabs.py`, the brief route | Yes, opt-in: `FLAKEPILOT_ELEVENLABS_API_KEY`+`_VOICE_ID` (503 `speech_not_configured` otherwise) | Unit: brief content (no repository text), MP3 validation, retries, route authorization and refusal codes | implemented, wired, mock-tested; **live-verified** 2026-10-24: adapter speech, invalid key, and `POST …/investigation/brief` returning a decodable MP3 |
| 15 | **Entire** | Local provenance: one `diag.investigation.recorded` per investigation (stopping condition, stage/source/page/link counts, agent attribution) | `investigation.py` `_provenance`, diag outbox | Yes | Unit + real-PG (one row per outcome, refusals included) | local provenance implemented, wired, real-PG-tested; Entire attestation **not justified**: no runtime API, and its commit trailers conflict with AGENTS.md commit rules ([ENTIRE-PROVENANCE](ENTIRE-PROVENANCE.md)) |
| 16 | **`.TECH` domain** | Domain/DNS/TLS/callback strategy; the origin stays configurable | [DOT-TECH-DOMAIN-STRATEGY](DOT-TECH-DOMAIN-STRATEGY.md) | — | Docs only | **blocked**: purchase is an owner action |

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
| Auth0 (2026-10-25) | One client-credentials `POST /oauth/token` on a development tenant for an M2M application granted `repo.view repo.investigate`; the token verified through `Auth0TokenVerifier` (tenant JWKS) with exactly those scopes and narrowed by `agent_tokens.narrow` without widening (`tests/live`). The same token was refused by a verifier for another audience and with one signature character changed. No route accepts agent tokens |
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

## What the owner must supply before anything else moves to live-verified

- Keys (each enables only its own provider; see README "Hosted providers"):
  `FLAKEPILOT_GEMINI_API_KEY` + `FLAKEPILOT_GEMINI_MODEL`,
  `FLAKEPILOT_SERPAPI_API_KEY`,
  `FLAKEPILOT_BROWSERBASE_API_KEY` + `FLAKEPILOT_BROWSER_ALLOWLIST`,
  `FLAKEPILOT_ELEVENLABS_API_KEY` + `FLAKEPILOT_ELEVENLABS_VOICE_ID`
  (optional `FLAKEPILOT_ELEVENLABS_MODEL`),
  `FLAKEPILOT_TABPFN_API_KEY`, `FLAKEPILOT_SENTRY_DSN`,
  `FLAKEPILOT_AUTH0_DOMAIN` + `FLAKEPILOT_AUTH0_AUDIENCE` (no consumer yet).
  The live tests read separate `FLAKEPILOT_LIVE_*` variables
  (`tests/live/`) so a test run never picks up a deployment key. Still
  missing for live verification: a Browserbase key.
- Data-flow consent per provider (what leaves the process is listed in the
  README table), including the Gemini free-tier decision.
- Hosting and domain decisions (goals 10, 16; D-HOST-1..5) and isolation
  infrastructure with a named reviewer (goal 11).

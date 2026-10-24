# Release-readiness register — what closes locally, what is owner-blocked

The concise go/no-go register behind the [local-only release candidate](RELEASE-CANDIDATE.md).
It consolidates, without replacing them: the 10J assessment and roadmap
([PRODUCTION-READINESS-ASSESSMENT](PRODUCTION-READINESS-ASSESSMENT.md),
[PRODUCTION-READINESS-ROADMAP](PRODUCTION-READINESS-ROADMAP.md)), the 10K
owner decision sheet ([OWNER-DECISION-SHEET](OWNER-DECISION-SHEET.md)), and
the grant-authorization decisions A–E
([GRANT-AUTHORIZATION-DECISION-SHEET](GRANT-AUTHORIZATION-DECISION-SHEET.md)).
Status vocabulary follows the 10J assessment: `PASS` (supported by code,
tests, or recorded operational evidence), `BLOCKED` (needs infrastructure,
credentials, or an owner decision that does not exist). A documented plan is
never a deployed service; a test with fakes is never a live integration.

Verified against `b9d1295` (2026-10-24). Nothing here was provisioned;
no decision below was made by this repository's development process.

## Local-only claim: closable without any owner decision

| Item | Status | Evidence |
|---|---|---|
| Reproducible local demo journey | **PASS** | `ops/demo/run-demo.sh` end-to-end; README step-by-step |
| Startup configuration fail-closed | **PASS** | `test_settings.py`; invalid value stops startup naming the variable |
| Auth/session/CSRF fail-closed behavior | **PASS** | `test_auth_http.py`, `test_iam*.py` (unit + Postgres) |
| Ingestion limits and idempotency | **PASS** | `test_ingestion*.py` (unit + Postgres) |
| Classification rule set (`ti-classification/1`) | **PASS** | `test_classification.py`, over real tables in integration tests |
| Session views + operator discovery/reporting | **PASS** | `test_run_views_http.py`, `test_runs_index_http.py`, CLI tests |
| Audit/outbox transactional integrity | **PASS** | `test_relay*.py`, `test_jobs_postgres.py` |
| Browser-side security headers | **PASS** | `test_run_views_http.py` (CSP/XFO on HTML, nosniff/Referrer-Policy on all) |
| Import boundaries and licence gates | **PASS** | `lint-imports` (21 contracts), pip-licenses dev+runtime in CI |
| Untrusted execution disabled | **PASS** | `ClosedSandbox`, `control_plane_reaper`, `RefusedHostHandoff`, `UnavailableIsolation` all hold in `test_worker.py`, `test_evidence.py`, `test_capability.py` |

## Owner-closable for a local-only release (nothing pending)

The 10J execution milestone's "Local development and testing" go decision
already stands. The one genuinely local item the owner could still close is
**reading and accepting the boundary statement** of
[RELEASE-CANDIDATE](RELEASE-CANDIDATE.md) — no decision record is required
for the local-only release itself, because it claims nothing needing one.

## Open owner decisions, and what stays blocked until each is made

| Decision | Owner question | Documented recommendation | Blocks until owner acts |
|---|---|---|---|
| **A** (grant verification) | Live-verify grants at grant time, or record intent and enforce live at read? | **A2**: intent at grant, live `decide()` enforcement at every read | Grant HTTP administration (there is none yet); final freeze of ADR-015 semantics. Not a local-only blocker |
| **B** (verifier outcomes) | How should verifier states (`verified`/`no_access`/`unavailable`) surface per flow? | Denies answer 404 in all flows (not-member ≡ no-grant ≡ unverified); `may_grant()` only if admin paths are ever built | Nothing shipped; freezes view-path semantics only. Not a local-only blocker |
| **C** (member consent) | What is the minimum member-consent mechanism before any session-admin granting? | Requires a **new ADR** defining consent + audit events before any session-admin route exists | All member session-administration routes. Not a local-only blocker (none exist) |
| **D** (verification delay/cache) | May the verifier result be cached, and for how long? | Stay live-per-view; **never cache denials** | Any caching/code-host-standing wiring. Not a local-only blocker (nothing cached today) |
| **E** (staging GitHub App) | Owner creates the staging App and performs the runbook's ordered checks | Mechanical runbook ready (`verify-github-app` diagnostic; staging-only, free, `Contents:read`, one staging repo). Owner-operated install is safe and unexecuted | **Live-verification claims** — "the handle is a collaborator" stays unverifiable; session view paths in credentialed deployments stay fail-closed-by-default |
| **10K D-HOST-1..5** (WP-1) | Hosting model, provider/tenancy, region, sizing, budget ceiling, cost owner | Recorded as options in `INFRASTRUCTURE-OPTIONS-ANALYSIS.md` only; nothing pre-selected | **Any hosted deployment or hosted claim**; all execution-host milestones (B1/B2/B3) |
| **10K D-RUNTIME-1** | Which per-run isolation runtime? | None (independent review required, per the runbook) | The `per_run_microvm` evidence level; every downstream isolation claim |
| **10K D-RUNTIME-5** | Who is the security reviewer, and what evidence-review process? | None | The B4/B9 review gate; production authorization |
| **10K Group B/C** (B5 vendor, B6 retention, B7 credentials owner, B8 operations) | External key manager, retention duration, monitoring sink/on-call/rollback | Deferred as owner decisions with prerequisites only | Artifact encryption-at-rest claims, any retention behavior, operations behavior in production |

## Acceptance criteria for revisiting each blocked claim

- **Local-only release**: *closed now* — the boundary statement above is the
  claim; nothing further to criteria.
- **Hosted staging deployment**: D-HOST-1..5 decided and recorded (WP-1);
  WP-2 checklist satisfied item by item; a deployment ADR; a deployed,
  health-probe-verified instance; delete-access and budget-owner named.
- **Live-verified GitHub path**: Decision E executed — App created by the
  owner (free), credentials staged exactly once (never committed, secret-store
  handled), runbook's ordered checks all PASS against a contacted provider
  with the request/response log summarized (never the token). Until then
  the mounted verifier stays fail-closed: an unconfigured deployment answers
  404 on the session view paths, which is the safe default.
- **Production execution of untrusted code**: the whole 10J B-chain —
  B1 (dedicated host, decision recorded + provisioned), B2 (per-run runtime
  with a separate kernel measured outside-the-guest), B3 (six host-side limit
  proofs + cleanup proofs incl. reboot), C1 (guest-observed credential/mount
  denial), B4 (unsimulated guest observations at the chosen level + reviewer
  sign-off), B9 (written independent authorization tied to a repository SHA)
  — plus B5/B6/B7/B8 as applicable to the requested surface. Every
  `IsolationChecks` field false until then; no fill-in by CI is possible.

The constraints in the 10J assessment remain: no provider, budget, runtime,
or retention duration is assumed; a missing control cannot be annotated away;
a green test suite is not proof of security, isolation, or encryption.

# FlakePilot — agent guide

Pointers into the repository plus the conventions that are recorded nowhere else.
Do not duplicate the facts here in other docs. Verify this file against the code
when a stage report claims it is wrong; update it when the code changes.

## Read first

| Need | Source |
|---|---|
| What the product does | [docs/product/PRODUCT-SCOPE.md](docs/product/PRODUCT-SCOPE.md) |
| System design, contexts, dependency rules | [docs/architecture/](docs/architecture/) |
| Every standing decision | [docs/adr/README.md](docs/adr/README.md), esp. ADR-004 (planes), ADR-008 (trust model), ADR-010 (toolchain) |
| Execution plane status | [docs/operations/EXECUTION.md](docs/operations/EXECUTION.md) |
| Execution host contract and runbook | [docs/operations/EXECUTION-HOST.md](docs/operations/EXECUTION-HOST.md) and [docs/operations/EXECUTION-HOST-RUNBOOK.md](docs/operations/EXECUTION-HOST-RUNBOOK.md) |
| Production readiness (10J) | [docs/operations/PRODUCTION-READINESS-ASSESSMENT.md](docs/operations/PRODUCTION-READINESS-ASSESSMENT.md) and [docs/operations/PRODUCTION-READINESS-ROADMAP.md](docs/operations/PRODUCTION-READINESS-ROADMAP.md) |
| Infrastructure decisions (10K) | [docs/operations/INFRASTRUCTURE-DECISIONS.md](docs/operations/INFRASTRUCTURE-DECISIONS.md), [docs/operations/INFRASTRUCTURE-OPTIONS-ANALYSIS.md](docs/operations/INFRASTRUCTURE-OPTIONS-ANALYSIS.md), and [docs/operations/INFRASTRUCTURE-IMPLEMENTATION-PLAN.md](docs/operations/INFRASTRUCTURE-IMPLEMENTATION-PLAN.md) |
| Owner decision sheet (10K/WP-1) | [docs/operations/OWNER-DECISION-SHEET.md](docs/operations/OWNER-DECISION-SHEET.md) |
| Release boundary (local-only candidate) and upgrade criteria | [docs/operations/RELEASE-CANDIDATE.md](docs/operations/RELEASE-CANDIDATE.md) and [docs/operations/RELEASE-READINESS-REGISTER.md](docs/operations/RELEASE-READINESS-REGISTER.md) |
| Sponsor-capability phase: implementation map (16 goals) | [docs/operations/SPONSOR-IMPLEMENTATION-MAP.md](docs/operations/SPONSOR-IMPLEMENTATION-MAP.md) and [ADR-017](docs/adr/ADR-017-sponsor-capability-phase.md) |
| Firecracker feasibility (10K, read-only) | [docs/operations/FIRECRACKER-FEASIBILITY-ASSESSMENT.md](docs/operations/FIRECRACKER-FEASIBILITY-ASSESSMENT.md) — candidate assessment only; nothing installed or provisioned |
| Firecracker threat model (10K, read-only) | [docs/operations/FIRECRACKER-THREAT-MODEL.md](docs/operations/FIRECRACKER-THREAT-MODEL.md) — analysis of proposed architecture; distinguishes implemented from proposed |
| Durable jobs and the outbox | [docs/operations/DURABLE-JOBS.md](docs/operations/DURABLE-JOBS.md) |
| Commands, config, HTTP contract | [README.md](README.md) |

## Shape of the code

Python 3.12, FastAPI, PostgreSQL. Modular monolith under `src/flakepilot/`,
one bounded context per directory (`identity`, `test_intelligence`,
`orchestration`, `execution`, `integrations`, `audit`, `repository`,
`diagnosis`, `verification`, `approval`, `shared_kernel`). Each context has
`domain` / `application` / `infrastructure` layers and its own migration
directory and database schema. HTTP routes and the composition roots live in
`interface/`, never business logic. Import boundaries are enforced by
import-linter contracts in `pyproject.toml` (21 contracts; do not weaken one
to make code fit).

Production wiring happens only in `interface/composition_root/`
(`reproduction.py` for the HTTP app, `worker.py` for the worker). A class
existing somewhere does not mean the app uses it. Always check the composition
root before claiming a feature is wired.

## Fail-closed inventory (what production actually uses)

Verified against `interface/composition_root/` at `145ebc93`; the AI and
hosted-provider bullets re-verified at `ae81fcf`, the agent-route, Atlas
and Temporal bullets at `4c1bf81` (`reproduction.py` and `worker.py`
unchanged since), and the deployment bullet against Render at `2a3c744`:

- `_sandbox` returns `ClosedSandbox` — no container, no repository code, ever,
  in the default composition.
- The worker uses `control_plane_reaper()` (`UnavailableRuntimeReaper`) and
  `RefusedHostHandoff`; it does not hold a host ledger key and does not open
  the host ledger.
- Artifacts live in `LocalFsArtifactStore` (plaintext). No sealer is
  constructed; `UnconfiguredSealer`, `ProviderBackedSealer` and `AesGcmSealer`
  exist but are not wired.
- `UnavailableIsolation` is the only production `IsolationProvider`: every
  check false, ungoverned by environment variables.
- Retention (`integrations/application/retention.py`) accepts only
  `policy="disabled"` and selects nothing; audit and outbox keys are not
  artifact keys.
- AI assists through `NullAiAssist` unless the operator sets
  `FLAKEPILOT_AI_ENDPOINT` (local) or `FLAKEPILOT_GEMINI_API_KEY` (hosted
  Gemma); it is reached only through the investigation workflow, behind the
  org AI policy: the session route (`POST /orgs/{org}/runs/{run}/investigation`),
  the agent route below, and the opt-in `temporal-worker`.
- Hosted providers (`composition_root/providers.py`: SerpApi, Browserbase,
  ElevenLabs, TabPFN, Sentry) are built only when their keys are set, in the
  API process and operator commands; absent means no adapter and no contact.
  The worker builds none of them and `render.yaml` gives it none. Live
  verification (local, 2026-10-24: Gemma, SerpApi, ElevenLabs, Sentry;
  2026-10-25: TabPFN on synthetic rows, the Auth0 agent route, one Gemma
  hypothesis through a real investigation; not Browserbase) is recorded in
  [SPONSOR-IMPLEMENTATION-MAP](docs/operations/SPONSOR-IMPLEMENTATION-MAP.md).
- Deployed (2026-10-25): only the control plane, on Render (`render.yaml`,
  region singapore) at `https://app.flakepilot.tech` — the API (GitHub App,
  Gemini, SerpApi, ElevenLabs, Sentry, Auth0 configured; not Browserbase or
  TabPFN), the worker (database URL, log level and `closed` execution mode
  only; no provider keys) and PostgreSQL 16 with an empty IP allow-list.
  Execution stays closed there exactly as above. Live on the deployment:
  GitHub sign-in, the run view, the investigation (SerpApi; Gemma after
  `set-ai-policy`) and the spoken brief
  ([DEPLOYMENT-RUNBOOK](docs/operations/DEPLOYMENT-RUNBOOK.md#deployed-state-2026-10-25)).
- The Auth0 agent route (`POST /agent/orgs/{org}/runs/{run}/investigation`)
  is mounted only when `FLAKEPILOT_AUTH0_DOMAIN`/`_AUDIENCE` are set; unset,
  the path is an unknown route. A token is never authority: an org admin's
  binding (`bind-agent`) is, re-derived per request from that admin's live
  grants and verified code-host standing (stand-in only in tests; the GitHub
  App verifier on this path is not live-verified). Mounted on the deployment,
  not exercised there.
- MongoDB Atlas is an opt-in, write-only copy: only the `export-reports-atlas`
  operator command builds the client (`FLAKEPILOT_ATLAS_URI`/`_DATABASE`); the
  API process and the worker never import the driver, and nothing reads Atlas
  back. PostgreSQL stays the system of record.
- Temporal is an opt-in proof of concept: only the `temporal-*` operator
  commands import it or connect (`FLAKEPILOT_TEMPORAL_*`); the default worker
  and the API process never do. FlakePilot's advisory lock and existing
  `diag.investigation.recorded` row stay authoritative. Verified against a
  real local Temporal dev server only; Temporal Cloud is BLOCKED.

`LoopbackHostChannel` / `LoopbackHostClient` (127.0.0.1, mTLS, fingerprint
pinning, `ReferenceHost` only) and `AesGcmSealer` are tested boundary code.
They are NOT deployed transport and NOT encryption at rest. Do not connect
them to the worker or the composition root.

## Security invariants (violating any of these is a defect)

- The control plane never executes untrusted repository code. A dedicated
  execution host is a different trust boundary: no database URL, session
  secret, GitHub App private key, or unrestricted control-plane credentials.
- Guests receive no host ledger key, runtime socket, control-plane secrets,
  artifact keys, broad mounts, or privileged capabilities. Host-enforced CPU,
  memory, PID, disk, output and wall limits, and default-deny egress. A
  docker container on a laptop is not production isolation evidence.
- Evidence levels simulate < shared_kernel_container <
  dedicated_host_container < per_run_microvm < production_approved. Lower
  evidence never satisfies a higher requirement. `IsolationChecks` stay false
  until real evidence is reviewed on real infrastructure.
- When infrastructure is absent, production execution stays disabled. No
  environment variable or fake evidence marks missing checks as passed.
- HMAC possession alone is not permission; no frame can choose a host path,
  program, or container id. The runtime inventory comes from the host itself.
- Retention duration is a product/legal/security decision, made by a human
  approval outside this repository. Never delete audit or outbox rows as
  artifact retention.
- GitHub authorization stays server-side; tokens get `contents:read` on one
  repository. The single exception is the operator-only
  `backfill-ci-history`, whose token is `actions:read` (instead, not in
  addition) on the one repository registered for the pair. No token carries
  both, and none carries a write permission. Minting was live-checked on
  2026-10-25 (one commit confirmation, the backfill's run/artifact listings
  and one artifact download), and the session-view verifier was
  live-checked on the deployment on 2026-10-25 (the owner's run view loaded);
  verify-github-app was not run live — say so, do not claim it.
- A green test suite is not proof of security, isolation, or encryption.

## Development workflow and Git conventions

- All work happens in the verified local checkout
  `/Users/priyanshujha/Projects/FlakePilot` on the Mac. Never edit a cloud or
  remote checkout and push from it. If that repository is inaccessible or
  synchronization would be unsafe, stop and report instead of working
  elsewhere.
- Before development: inspect `git status --short` first, preserve every user
  change (including untracked files), then `git fetch` and compare with
  `origin/main`. Fast-forward only when cleanly behind; investigate divergence
  or unexplained local commits before proceeding.
- Never run `git reset --hard`, `git clean`, a destructive checkout, or a
  stash that silently hides work. Never force-push or rewrite published
  history.
- Work directly on `main`. No branches or PRs unless a direct change would be
  unsafe.
- Verify `git config user.name` / `user.email` before every commit. Both
  author and committer must be
  `Priyanshu Jha <155089480+CodewithJha@users.noreply.github.com>`
  (repository-local config; fix only that if it drifts). Unsigned commits,
  no co-author or tool-attribution trailers, no backdating, no fabricated
  authorship.
- Run the gates below before committing, and review `git diff` and
  `git diff --cached` so no unrelated change, secret, credential, or generated
  junk enters the commit. Small, logically separated commits.
- Push completed commits to `origin/main` once checks pass and direct-to-main
  is safe — do not stop after editing files. Then verify the pushed SHA on
  GitHub and the actual Actions run for that commit; report failing, pending,
  or unavailable CI honestly.
- If tests fail, the diff shows surprises, or the branch diverged, keep the
  work locally and report the blocker instead of pushing. If no change is
  needed, report that no commit was necessary; never commit empty changes to
  show activity.

## Gates

Run before every commit (exact commands in [README.md](README.md#quality-gates)):
pytest (unit always; integration when PostgreSQL exists via
`FLAKEPILOT_TEST_DATABASE_URL`), ruff format + check, mypy, lint-imports,
`uv lock --check`, `git diff --check`, pip-licenses. CI adds the runtime
licence gate. Never lower an assertion or skip a test to get green.
`isolation-validation.yml` is manual (`workflow_dispatch`) and unrelated to
push CI.

## Status shorthand for reports

Label findings **VERIFIED** (source, command output, tests, or CI shows it),
**NOT VERIFIED** (implemented or plausible but not demonstrated at the
required level), or **BLOCKED** (needs infrastructure, credentials, or a
decision that is unavailable). Infrastructure-dependent blockers live in
[docs/operations/EXECUTION-HOST.md](docs/operations/EXECUTION-HOST.md);
re-list them in a report only after re-checking that they are still true.

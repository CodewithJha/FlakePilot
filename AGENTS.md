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
| Execution host and blockers | [docs/operations/EXECUTION-HOST.md](docs/operations/EXECUTION-HOST.md) |
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

Verified against `interface/composition_root/` at `145ebc93`:

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
- AI assists through `NullAiAssist` only; no HTTP route.

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
  repository. Live minting is NOT VERIFIED — say so, do not claim it.
- A green test suite is not proof of security, isolation, or encryption.

## Git conventions

- Work directly on `main`. No branches or PRs unless a direct change would be
  unsafe. Never force-push or rewrite published history.
- Small, logically separated commits. Run the test suite before committing.
- Author `Priyanshu Jha <155089480+CodewithJha@users.noreply.github.com>`,
  unsigned commits, no co-author or tool-attribution trailers.
- Never backdate commits or claim work that was not performed.

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

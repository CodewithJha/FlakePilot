# ADR-010: Engineering toolchain, repository conventions, and CI quality gates

| | |
|---|---|
| Status | **Accepted** by the project owner, 2026-10-20. Owner choices: Python 3.12, uv with a committed lockfile, GitHub Actions, Ruff, mypy, and TQ-1 to TQ-6 ([Owner answers](#owner-answers)) |
| Related | [ADR-001](ADR-001-modular-monolith.md), [ADR-002](ADR-002-domain-layering.md), [ADR-003](ADR-003-provider-adapters.md), [ADR-004](ADR-004-control-plane-and-execution-plane.md), [ADR-007](ADR-007-implementation-stack-and-licensing.md) (Python, FastAPI, SQLAlchemy plus Alembic, asyncpg, import-linter, pytest, licensing, quality gates), [ADR-008](ADR-008-deployment-and-trust-model.md), [ADR-009](ADR-009-initial-provider-adapters.md), [DEPENDENCY-RULES §1, §7](../architecture/DEPENDENCY-RULES.md#7-enforcement), [REPOSITORY-INSPECTION](../engineering/REPOSITORY-INSPECTION.md) |

## Context

- ADR-007 fixes Python, the main libraries, import-linter, pytest, Apache-2.0, permissive-only Python dependencies, and the pre-release licence-review gate for images (item 5, FQ-13). It also defines the merge gates (item 7).
- It does **not** fix the Python version, the package manager, the CI system, a linter or formatter, a type checker, or the licence-check tool (REPOSITORY-INSPECTION D-6).
- ADR-001 and ADR-002 require boundary checks to exist **before feature code**.
- The repo currently holds docs only, one commit on `main`, and no remote (REPOSITORY-INSPECTION §13).
- Host facts, from read-only commands on 2026-10-20:
  - `python3 --version` gives `Python 3.14.6`, and there is no `python3.12` on `PATH`.
  - `uv --version` gives `uv 0.11.23`.
  - uv's managed-Python directory already contains `cpython-3.12.13-macos-aarch64-none`.

## Decision

### Owner-chosen (2026-10-20)

| # | Choice | What it means here |
|---|---|---|
| 1 | **Python 3.12** as the target runtime | The project declares `requires-python` for 3.12. A committed `.python-version` pins 3.12, and uv honours it ([E-4](#evidence)). Developers and CI use a **uv-managed CPython 3.12**, never the host's default interpreter (here 3.14.6). CI pins 3.12 only. 3.12 is in its **security-only** phase until **2028-10** ([E-1](#evidence), [E-2](#evidence)), so a version bump is planned work before then |
| 2 | **uv**, with a **committed `uv.lock`** | Installs use `uv sync --locked`, which fails if the lockfile is out of date ([E-3](#evidence)). CI also runs `uv lock --check`, the same as `--locked` ([E-3](#evidence)), as the lockfile-freshness gate. `--frozen`, which skips the freshness check, is **not** used in CI. Every gate command runs through uv (`uv run …`), so local and CI environments match |
| 3 | **GitHub Actions** for CI | It matches the code host (ADR-009). Workflow files come in a later step, not in this ADR. **Third-party actions are pinned to full-length commit SHAs** ([E-9](#evidence)), and each action's licence is reviewed and recorded before use. Actions run in CI only and aren't shipped |
| 4 | **Ruff** for linting and formatting | `ruff check` (lint) and `ruff format --check` (format gate, non-zero exit on unformatted files) ([E-5](#evidence)). Ruff is **never** used for architecture boundaries; that's import-linter's job (ADR-007) |
| 5 | **mypy** for type checking | A gate on the whole package, at the strictness set by TQ-3 below |

### Already accepted elsewhere (restated, not changed)

- import-linter (`lint-imports`) for boundaries, and pytest for tests (ADR-007 item 6).
- Apache-2.0. Permissive-only for FlakePilot code and its Python dependencies. OS packages, browsers, and media tools in shipped images are third-party components, reviewed before any image release (ADR-007 item 5, FQ-13).

### Responsibilities of the tools

| Tool | Checks | Doesn't check |
|---|---|---|
| Ruff | Style, formatting, common bug patterns, unused imports | Layering, cross-context edges, the DIAG invariant |
| mypy | Type consistency | Which module may import which |
| import-linter | Layer direction, the DEPENDENCY-RULES §3.1 matrix, the §5 DIAG isolation invariant, framework and vendor imports in domain code | Style, types |
| pytest | Behaviour, including port conformance suites and outcome-recomputation tests (ADR-003, ADR-007 item 7) | Static structure |
| Licence checker | Licences of the installed Python packages | Image contents (see the licence policy below) |

### Merge gates in CI

These are required checks once a GitHub remote exists, enforced by protecting `main`. Every command runs through uv with the lockfile.

| Gate | Command (conceptual) |
|---|---|
| Lockfile freshness | `uv lock --check` |
| Reproducible install | `uv sync --locked` |
| Format | `uv run ruff format --check` |
| Lint | `uv run ruff check` |
| Types | `uv run mypy` |
| Boundaries | `uv run lint-imports` ([E-7](#evidence)) |
| Tests | `uv run pytest`, covering unit, architecture, conformance, and PostgreSQL integration tests |
| Dependency licences | The licence checker over the locked environment, failing closed (below) |

Developers run the same commands locally after `uv sync --locked`.

### Owner-confirmed details (TQ-1 to TQ-5, 2026-10-20)

**TQ-1. Licence checker: `pip-licenses`** ([E-10](#evidence), [E-11](#evidence)).
- **Why it fits:**
  - It reads the metadata of the **installed** environment, so it covers direct and transitive packages alike, and it can target another environment with `--python`.
  - It has an allowlist mode (`--allow-only`) and a deny mode (`--fail-on`) with non-zero exit codes.
  - `--from=mixed` reads licences from classifiers and from the metadata fields.
  - `--with-license-file` includes the full licence text for notice reports.
  - Its own licence is MIT, it is actively released, and it declares Python 3.12 support.
- **Alternatives evaluated:**
  - **licensecheck** (MIT, recent release, requires Python ≥3.12) reads the project's declared requirements and groups rather than the installed environment.
  - **liccheck** (Apache-2.0) has had no release since 2023-09.
  - **uv** has no licence-listing command in its CLI reference ([E-12](#evidence)).

**TQ-2. Licence policy:**
- **Scope (TQ-2):** check the full locked environment, including dev tools, as the gate. Generate the third-party notice report from the **runtime-only** environment.
- **Fail closed:** an unknown, missing, or unparseable licence, or one not on the permissive allowlist, fails the gate.
- **Dual or multi-licensed packages** pass only if at least one option is permissive (`--allow-only` already behaves this way, [E-10](#evidence)). The elected licence is recorded in the repo.
- **Exceptions:** a documented record in the repo (package, version, licence, reason, owner approval, date). It's re-reviewed whenever the version changes. Exceptions never cover a non-permissive licence for shipped Python dependencies (ADR-007 item 5).
- **Retained output:** for each release, a generated third-party licence report, with licence texts (`--with-license-file`, verified in [E-10](#evidence)), kept as a release artifact.
- **Images are separate.** **The Python checker doesn't establish image compliance.** Shipped images need an SBOM-style inventory of OS packages, browsers, and media tools, plus the manual licence review that gates image releases (ADR-007 item 5). The image tooling is chosen in the later stage that builds images (ADR-008 item 1).

**TQ-3. mypy strictness:** `--strict` ([E-6](#evidence)) for the whole package from the first commit. Narrow per-module overrides are allowed only for untyped third-party imports, each one recorded in the configuration ([E-6](#evidence)). It's cheapest to adopt in an empty repo.

**TQ-4, TQ-5. Repository conventions** (all follow DEPENDENCY-RULES §1–§2, §6 and DOMAIN-BOUNDARIES; no new contexts). CI runs on Python 3.12 only (TQ-5):

| Concern | Decision |
|---|---|
| Layout (TQ-4) | One deployable package in `src/flakepilot/`. One subpackage per context, named after the context rather than its code to avoid shadowing Python built-ins (`exec`, `int`): `identity`, `repository`, `test_intelligence`, `orchestration`, `execution`, `diagnosis`, `patches`, `verification`, `approval`, `integrations`, `audit`. Each has `domain/`, `application/`, and `infrastructure/`. `integrations/` holds the port contracts plus the vendor adapters in `infrastructure/` (ADR-003). Plus `shared_kernel/`, and `interface/` (HTTP, server-rendered pages, ingestion endpoint, webhook receiver, worker entry points, composition root) |
| Boundary contracts | import-linter contracts ([E-8](#evidence)): a layers contract per context; contracts forbidding every non-edge of the §3.1 matrix; a forbidden contract for the §5 DIAG invariant (TI, VER, APPR, and PATCH must not reach DIAG); `interface` must not import `integrations`; domain code must not import FastAPI, SQLAlchemy, Alembic, asyncpg, Pydantic, or vendor SDKs (external-package forbidding, [E-8](#evidence)). Whether forbidden contracts catch **indirect** imports by default is **unverified**, to verify when the tool is configured. The DIAG invariant doesn't depend on it: the matrix contracts forbid every non-edge directly, and no permitted path reaches DIAG (DEPENDENCY-RULES §5) |
| Tests | `tests/unit/<context>/<layer>`; `tests/architecture` (import-linter plus layout checks); `tests/conformance` (each port against its fake and its adapters); `tests/integration` (PostgreSQL). **Tests that run untrusted repo code exist only as execution-plane tests through the sandbox port, never in the control-plane test process** (ADR-004). The B-7 abuse suite needs the execution host and isn't a default CI gate |
| Configuration | Environment variables, read once in the composition root into typed settings. **No secrets in the repo**; no `.env` file is committed. Configuration holds secret **references** only. Values are resolved only inside INT adapters (ADR-003 decision 4, ADR-009) |
| Dependencies | Added with uv, so `pyproject.toml` and `uv.lock` change together in one PR. Every addition must pass the licence gate and fit ADR-007 (no psycopg, no broker, cache, or workflow-engine clients, ADR-006). Lock refreshes are separate PRs that pass all gates. No versions are fixed by this ADR |
| Lint and format settings | Ruff rule selection and line length are implementation details, reviewed like code |

## Rationale

- One version pin, enforced through `.python-version` and uv-managed interpreters, removes host drift. The host default here is 3.14, not 3.12.
- `--locked` everywhere makes the lockfile authoritative and turns lockfile drift into a CI failure rather than a silent re-resolve.
- Each tool has a separate responsibility, which keeps ADR-007's rule that import-linter, not a linter, owns the boundaries.
- Checking the **installed** environment covers transitive packages without a separate resolver, and fail-closed matches ADR-007's permissive-only rule.

## Alternatives considered

| Alternative | Why not |
|---|---|
| Use the host interpreter | Version drift (3.14.6 on this host) |
| `uv sync --frozen` in CI | Skips the freshness check ([E-3](#evidence)) |
| Ruff rules as boundary checks | ADR-007 assigns boundaries to import-linter. Ruff can't express the §3.1 matrix and layer contracts as clearly |
| licensecheck, liccheck | See TQ-1 |
| Per-module mypy strictness, starting loose | Harder to tighten later, and there's no legacy code to accommodate |

## Consequences

- **Positive:** local and CI runs use the same interpreter, lockfile, and gate commands. Boundaries are enforced before any feature code. The licence gate is automatic and fails closed.
- **Negative:** a planned Python bump before 3.12's end of life (2028-10). Strict mypy adds friction with untyped libraries. The image licence review stays manual until images exist.
- **Unresolved, not blocking:** whether import-linter's forbidden contracts check indirect imports by default (unverified; to verify when the tool is configured). Workflow files, `pyproject.toml`, `uv.lock`, the `LICENSE` file, and branch protection all come in later, separately authorized steps.

## Owner answers

Confirmed by the owner on 2026-10-20.

| ID | Question | Answer |
|---|---|---|
| TQ-1 | Licence-check tool | pip-licenses |
| TQ-2 | Licence-check scope | Full locked environment for the gate; runtime-only for the notices report |
| TQ-3 | mypy strictness | `--strict` everywhere, with recorded overrides for untyped third-party imports |
| TQ-4 | Package and context naming | `src/flakepilot/` with context-name subpackages |
| TQ-5 | CI Python matrix | 3.12 only |
| TQ-6 | Accept ADR-010? | Accepted. The stale-wording fixes in DEPENDENCY-RULES §7, ADR-002, and ADR-001 were applied |

## Evidence

Fetched 2026-10-20, except E-0, which is from local commands.

| ID | Source | Confirms | Status |
|---|---|---|---|
| E-0 | Local: `python3 --version`, `uv --version`, `ls ~/.local/share/uv/python`, `command -v python3.12` | Host Python 3.14.6; uv 0.11.23; uv-managed CPython 3.12.13 present; no `python3.12` on `PATH` | Verified |
| E-1 | https://devguide.python.org/versions/ | 3.12 status "security", first release 2023-10-02, end of life 2028-10 | Verified |
| E-2 | https://peps.python.org/pep-0693/ | 3.12.10 was the final regular bugfix release with binary installers; after that, source-only security releases until 2028-10 | Verified |
| E-3 | https://docs.astral.sh/uv/concepts/projects/sync/ ; https://docs.astral.sh/uv/reference/cli/ | `--locked` raises an error if the lockfile is out of date; `--frozen` uses the lockfile without checking; `uv lock --check` "is equivalent to `--locked`" | Verified |
| E-4 | https://docs.astral.sh/uv/concepts/python-versions/ ; https://docs.astral.sh/uv/reference/cli/ | uv reads `.python-version`, respects `requires-python`, and has `uv python pin` | Verified |
| E-5 | https://docs.astral.sh/ruff/formatter/ ; https://docs.astral.sh/ruff/linter/ ; PyPI `ruff` JSON | `ruff format --check` exits non-zero on unformatted files; `ruff check` lints; licence MIT; Python 3.12 classifier | Verified |
| E-6 | https://mypy.readthedocs.io/en/stable/command_line.html ; https://mypy.readthedocs.io/en/stable/config_file.html ; PyPI `mypy` JSON | `--strict` enables a defined subset of optional checks; per-module config sections exist; licence MIT; Python 3.12 classifier | Verified |
| E-7 | https://import-linter.readthedocs.io/en/stable/get_started/run/ ; …/get_started/configure/ ; PyPI `import-linter` JSON ; https://raw.githubusercontent.com/seddonym/import-linter/master/LICENSE | The `lint-imports` command; config in `pyproject.toml` or `.importlinter`; licence BSD 2-Clause; Python 3.12 classifier | Verified |
| E-8 | https://import-linter.readthedocs.io/en/stable/contract_types/layers/ ; …/independence/ ; …/forbidden/ | Layers, independence, and forbidden contract types; external packages can be forbidden when external packages are included | Verified. Default handling of indirect imports in forbidden contracts: **unverified** |
| E-9 | https://docs.github.com/en/actions/reference/security/secure-use | Pinning actions to a full-length commit SHA is recommended (the page also describes workflow dependency lockfiles) | Verified |
| E-10 | https://raw.githubusercontent.com/raimon49/pip-licenses/master/README.md | Checks installed packages; `--python` targets another environment; `--allow-only` / `--fail-on` with multi-licence semantics; `--partial-match`; `--from=mixed`; `--with-license-file` | Verified. A notice-file option: **unverified** |
| E-11 | PyPI `pip-licenses` JSON ; https://raw.githubusercontent.com/raimon49/pip-licenses/master/LICENSE | Version 5.5.5, licence MIT (`license_expression`), requires Python ≥3.9, Python 3.12 classifier, last upload 2026-10-05; repo licence MIT | Verified |
| E-12 | https://docs.astral.sh/uv/reference/cli/ | The full CLI reference mentions "license" only once, as the navigation link to uv's own licence page; there is no licence-listing command | Verified (absence) |
| E-13 | PyPI `licensecheck` JSON ; https://raw.githubusercontent.com/FHPythonUtils/LicenseCheck/master/LICENSE.md ; README | Version 2026.0.8, licence MIT, requires Python ≥3.12, last upload 2026-06-18; reads `pyproject.toml` and requirements, with groups | Verified |
| E-14 | PyPI `liccheck` JSON ; https://raw.githubusercontent.com/dhatim/python-license-check/master/LICENSE | Version 0.9.2, Apache-2.0, last upload 2023-09-22 | Verified |
| E-15 | PyPI `pytest` and `uv` JSON | pytest licence MIT, Python 3.12 classifier; uv licence `MIT OR Apache-2.0` | Verified |
| E-16 | https://docs.astral.sh/uv/guides/integration/github/ | Official `astral-sh/setup-uv` action; the example pins actions by SHA and installs with `uv sync --locked` | Verified |

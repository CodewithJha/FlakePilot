# FlakePilot — Repository Inspection (Stage 2.1)

| | |
|---|---|
| Status | **Stage 2.1**, inspection only. Nothing was fixed, scaffolded, installed, or initialized. Stage 2.2 not started |
| Inspected | `Tue Oct 20 19:10:21 IST 2026` (`date` output at the start of inspection) |
| Workspace root | `/Users/priyanshujha/Projects/FlakePilot` |
| Pre-change snapshot | `/tmp/fp_s21_before.txt` (outside the workspace): path, size, mtime, sha256 for every entry, including hidden ones |
| Only change made | This file, plus the new `docs/engineering/` directory |
| Post-inspection follow-up | §1–§12 describe the workspace at inspection time. **Git was initialized afterwards** and the docs were staged, with no commit yet. See [§13](#13-post-inspection-follow-up-2026-10-20-1920-ist) |

**Status vocabulary** used below: **not implemented** (accepted in the docs, absent in the repo), **not found** (looked for, absent, no doc requires it yet), **conflicting** (repo contradicts an accepted decision), **unable to verify** (couldn't be checked from here). An absent implementation is not a conflict.

---

## 1. Summary

The workspace holds **only the Stage 1 documentation**: 16 Markdown files under `docs/`. There is no code, configuration, test, dependency manifest, CI, deployment file, licence file, hidden file, or symlink. **Git is not initialized**, here or in any parent directory. Nothing in the repo conflicts with ADR-001 to ADR-009; everything they decide is not implemented yet.

---

## 2. Version control

| Check | Command | Result |
|---|---|---|
| Inside a work tree? | `git rev-parse --is-inside-work-tree` | `fatal: not a git repository (or any of the parent directories): .git`, exit 128 |
| Enclosing repo? | `git -C . rev-parse --show-toplevel` | Same error, exit 128 |
| `.git` in workspace | `find . -name '.*' ! -name .` | No output (no hidden entries at all) |
| `.git` in parent | `ls -la ~/Projects/.git` | `No such file or directory` |

Branch, status, log, tracked files, ignored files, and remotes: **not applicable** (no repository). `git status --porcelain=v1 --ignored` and `git diff` could not be used for verification; the snapshot diff (§11) is used instead.

**Doc-internal fact:** [PRODUCT-SCOPE](../product/PRODUCT-SCOPE.md) line 7 records that, when it was written, "The directory was empty and was not a git repository". The Git part is still true. Stage 1 work therefore has **no version history**. The only copies outside the workspace are 12 directories `/private/tmp/fp2` … `/private/tmp/fp13` (142 files, e.g. `fp13/BOUNDED-CONTEXTS.md.bak`, `fp2/mtimes.before.txt`) and `/private/tmp/fp_audit_check.py`. `/tmp` is a system temporary directory, not durable backup storage. Their contents were not inspected beyond listing.

---

## 3. Inventory

From `find . -print` (all entries, hidden included) and `ls -la`. Times are local (IST), 2026-10-20. sha256 prefixes are from `/tmp/fp_s21_before.txt`.

| Path | Bytes | Modified | sha256 (first 12) |
|---|---:|---|---|
| `docs/product/PRODUCT-SCOPE.md` | 62410 | 19:02 | `467b2812bc55` |
| `docs/architecture/SYSTEM-ARCHITECTURE.md` | 46736 | 18:58 | `a68c1d48ebea` |
| `docs/architecture/BOUNDED-CONTEXTS.md` | 47950 | 19:07 | `07d20baa0d94` |
| `docs/architecture/DOMAIN-BOUNDARIES.md` | 15354 | 19:02 | `78cc1ccc6dc0` |
| `docs/architecture/DEPENDENCY-RULES.md` | 10751 | 17:27 | `cdf4032d1dfe` |
| `docs/architecture/RELIABILITY-AND-SLOS.md` | 40187 | 19:02 | `1a8dadeffa9f` |
| `docs/adr/README.md` | 7871 | 18:45 | `3a160f4da2d3` |
| `docs/adr/ADR-001-modular-monolith.md` | 4153 | 12:14 | `55b8d3fe3d1f` |
| `docs/adr/ADR-002-domain-layering.md` | 4293 | 17:27 | `54f370fb6ff1` |
| `docs/adr/ADR-003-provider-adapters.md` | 4668 | 17:37 | `3cdae8edaa01` |
| `docs/adr/ADR-004-control-plane-and-execution-plane.md` | 5122 | 17:27 | `c65b74eda17c` |
| `docs/adr/ADR-005-system-of-record-database.md` | 9842 | 18:31 | `bab7f42b9ef3` |
| `docs/adr/ADR-006-durable-jobs-in-the-database.md` | 6246 | 18:29 | `0958d7a734c6` |
| `docs/adr/ADR-007-implementation-stack-and-licensing.md` | 7517 | 18:45 | `03591ed56f34` |
| `docs/adr/ADR-008-deployment-and-trust-model.md` | 8865 | 18:31 | `61892755de1f` |
| `docs/adr/ADR-009-initial-provider-adapters.md` | 8589 | 18:48 | `89798295ea6e` |

- Directories: `.`, `docs/`, `docs/adr/`, `docs/architecture/`, `docs/product/`. File types: 16 × `.md`, nothing else.
- Hidden files/dirs (`.git`, `.cursor`, `.vscode`, `.env*`, `.gitignore`, `.DS_Store`, …): **none** in the workspace.
- Symlinks (`find . -type l`): **none**. Generated or ignored files: **none found** (no generator, no `.gitignore`).
- Every file and docs directory carries the macOS extended attribute `com.apple.provenance` (`xattr -r docs`: 20 entries). Informational; nothing in the docs depends on it.
- `BOUNDED-CONTEXTS.md` was modified at 19:07, three minutes before this inspection. The snapshot records its state at 19:10.

| Category | Status | Evidence |
|---|---|---|
| Documentation | **Present** | 16 files above |
| Backend (control-plane code) | Not implemented | No source files (`find`: only `.md`) |
| Frontend (server-rendered pages, ADR-007) | Not implemented | Same |
| Shared contracts / shared kernel | Not implemented | Same |
| Config, env templates | Not found | No config files, no `.env*` |
| Tests | Not implemented | No test files, no `conftest.py`, no `pytest.ini` |
| Deployment (Docker-based, ADR-008) | Not implemented; ADR-008 assigns it to a later stage | No `Dockerfile`, no compose file |
| CI (`.github/workflows`, etc.) | Not found | No hidden directories |
| Migrations (Alembic, ADR-005/007) | Not implemented | No `alembic/`, no `alembic.ini` |
| Scripts / Makefile | Not found | — |
| Prototypes, generated assets, unfinished code | Not found | — |

---

## 4. Languages, frameworks, dependencies, tools

**Repository state:** no language, framework, or dependency is declared anywhere in the repo.

| Item | Status | Evidence |
|---|---|---|
| `pyproject.toml`, `requirements*.txt`, `poetry.lock`, `uv.lock`, `package.json` | Not found | Only `.md` files exist |
| Python version (`.python-version`, `requires-python`) | Not found. **The docs don't pin a version either** | `grep` for `Python 3` / `3.1x` in `docs/`: no match |
| Package/dependency manager | Not found; **not decided in the docs** | `grep -w 'uv\|poetry\|pip\|pdm\|hatch\|setuptools' docs`: no match |
| ruff / black / mypy / pyright config | Not found; not decided in the docs | — |
| import-linter config (`.importlinter` or in `pyproject.toml`) | Not implemented (required by ADR-007 item 6) | — |
| pytest config | Not implemented (ADR-007 item 6) | — |
| Broker, cache, workflow engine, MongoDB, other infra | Not found. No mention in the docs either | `grep -i 'mongodb\|redis\|kafka\|rabbitmq\|celery\|temporal\|airflow' docs`: no match |

**Host environment (informational, not repo state).** Read-only `--version` calls, run from `/tmp`:

| Tool | Result |
|---|---|
| OS | `Darwin arm64` |
| `python3` | `Python 3.14.6` (`/opt/homebrew/bin/python3`); `python`: not on PATH |
| `pip3` | `pip 26.1.2` |
| `uv` | `uv 0.11.23` |
| `git` | `git version 2.54.0` |
| `docker` / compose | `Docker version 29.5.3`; `Docker Compose version v5.1.4` |
| `psql` | `psql (PostgreSQL) 16.14 (Homebrew)` (client only; no server checked) |
| `node` / `npm` | `v26.10.0` / `11.19.1` |
| `make` | `GNU Make 3.81` |
| `poetry`, `podman`, `ruff`, `mypy`, `pyright`, `lint-imports`, `pytest` | Not on PATH |

---

## 5. Tests and runnability

- **No tests exist** and nothing is runnable: there is no code and no manifest. No test or tool was run against the workspace.
- The only executed checks were read-only: `find`, `ls`, `stat`, `shasum`, `xattr`, `grep`, and an inline Python link checker run with `PYTHONDONTWRITEBYTECODE=1` from stdin (writes nothing).

---

## 6. Secrets and configuration

- No `.env`, `.env.*`, credential, key, or config file exists (`find . -name '.*'`: none; all files are `.md`). Configured-value count: **not applicable** (0 env files).
- The docs contain no secret values; they describe secret *handling* only ([ADR-009](../adr/ADR-009-initial-provider-adapters.md), secret storage row).

---

## 7. Work to preserve

All 16 files in §3: the accepted Stage 1 product scope, architecture, and ADR-001 to ADR-009. They are the only work in the workspace and have no history (§2). The automated link check found **230 relative links, 0 broken** across these files (§11).

---

## 8. Repo state vs accepted architecture

### 8.1 Per ADR

| ADR | Repo status | Notes |
|---|---|---|
| [ADR-001: Modular monolith with strict domain boundaries](../adr/ADR-001-modular-monolith.md) | Not implemented | No code. ADR-001 Consequences: boundary checks must be in place "from the first code" |
| [ADR-002: Per-domain layering and cross-domain dependency rules](../adr/ADR-002-domain-layering.md) | Not implemented | No layers, no shared kernel. "Boundary tooling must exist before feature code" (Consequences) |
| [ADR-003: Provider ports and adapters owned by External integrations](../adr/ADR-003-provider-adapters.md) | Not implemented | No ports, fakes, conformance suites, or adapters |
| [ADR-004: Separate trusted control plane from untrusted execution plane](../adr/ADR-004-control-plane-and-execution-plane.md) | Not implemented | No execution code of any kind; nothing runs untrusted code |
| [ADR-005: One transactional relational database as the control-plane system of record](../adr/ADR-005-system-of-record-database.md) | Not implemented | No persistence code, no migrations. No competing datastore found |
| [ADR-006: Durable jobs, timers, outbox dispatch, and coordination in the system-of-record database](../adr/ADR-006-durable-jobs-in-the-database.md) | Not implemented | No broker, cache, lock service, or workflow engine found |
| [ADR-007: Implementation stack, UI and API approach, licensing, and quality gates](../adr/ADR-007-implementation-stack-and-licensing.md) | Not implemented | No Python project, FastAPI, SQLAlchemy/Alembic, asyncpg, import-linter, pytest, licence check. **No `LICENSE` file** (Apache-2.0 decided, item 5). No third-party licence inventory (no dependencies yet) |
| [ADR-008: Initial deployment model, execution trust model, and sandbox class](../adr/ADR-008-deployment-and-trust-model.md) | Not implemented | No deployment artifacts; ADR-008 item 1 places them in a later stage |
| [ADR-009: Initial provider adapters and telemetry export](../adr/ADR-009-initial-provider-adapters.md) | Not implemented | No GitHub, JUnit XML, Playwright, storage, secret, or OpenTelemetry adapters. No AI provider code (consistent with "none at launch") |

**Conflicting:** none found. No authn, authz, tenant isolation, audit, persistence, execution, or AI-provider code exists to conflict.

### 8.2 Doc-internal facts relevant to Stage 2 (not fixed)

| # | Fact | Evidence |
|---|---|---|
| D-1 | No doc defines Stage 2 or Stage 2.2 scope | `grep 'Stage 2' docs`: no match |
| D-2 | [BOUNDED-CONTEXTS §6](../architecture/BOUNDED-CONTEXTS.md#6-open-questions) says "The repo docs name no stages after Stage 1.3", but the [ADR index](../adr/README.md) is Stage 1.4 and [RELIABILITY-AND-SLOS](../architecture/RELIABILITY-AND-SLOS.md) is Stage 1.5 | `BOUNDED-CONTEXTS.md:379`; `README.md:3,19`; `RELIABILITY-AND-SLOS.md:5` |
| D-3 | [DEPENDENCY-RULES §7](../architecture/DEPENDENCY-RULES.md#7-enforcement) still says the boundary tooling "depends on the language choice (deferred)"; ADR-007 has since chosen Python and import-linter. ADR-002's follow-up has the same wording | `DEPENDENCY-RULES.md:172`; `ADR-002-domain-layering.md:51` |
| D-4 | The status rows of SYSTEM-ARCHITECTURE, DOMAIN-BOUNDARIES, and DEPENDENCY-RULES say "Stage 1.2 complete … ADR-001 to ADR-004 accepted", while SYSTEM-ARCHITECTURE §10 already cites ADR-005 to ADR-009 | Line 5 of each; `SYSTEM-ARCHITECTURE.md:490-502` |
| D-5 | [PRODUCT-SCOPE §12](../product/PRODUCT-SCOPE.md#12-decisions-deferred-to-later-stages) still lists as deferred several choices that ADR-005 to ADR-009 have made, and has no reference to those ADRs. Not a contradiction: SYSTEM-ARCHITECTURE §10 and the ADR index track the decisions | `grep 'ADR-00[5-9]' PRODUCT-SCOPE.md`: no match |
| D-6 | Not decided anywhere in the docs: Python version, package manager, lint/format/type-check tools, CI system for FlakePilot itself ("CI/CD for FlakePilot" is listed as deferred), the licence-check tool, template engine, and ASGI server (the last three are ADR-007 implementation details) | §4 greps; `PRODUCT-SCOPE.md:754`; ADR-007 "Implementation details for later design" |

---

## 9. Prioritized findings

| # | Severity | Finding | Status | Paths |
|---|---|---|---|---|
| 1 | **High** | No version control. Stage 1 work has no history; the only extra copies are ephemeral files in `/tmp` | Not found | Workspace root (no `.git`); `/private/tmp/fp2`–`fp13`, `/private/tmp/fp_audit_check.py` |
| 2 | **High** (sequencing) | Boundary enforcement (import-linter contracts for layers, the §3.1 matrix, the §5 DIAG invariant) must exist before feature code, and none exists | Not implemented | `docs/adr/ADR-001-modular-monolith.md`, `docs/adr/ADR-002-domain-layering.md`, `docs/adr/ADR-007-implementation-stack-and-licensing.md`, `docs/architecture/DEPENDENCY-RULES.md` |
| 3 | Medium | No Stage 2 / 2.2 definition in the docs; scope must come from the owner | Not found | `docs/` (D-1) |
| 4 | Medium | No `LICENSE` (Apache-2.0) and no dependency-licence check | Not implemented | `docs/adr/ADR-007-implementation-stack-and-licensing.md` item 5, item 7 |
| 5 | Medium | Toolchain choices that Stage 2 needs are undecided: Python version, package manager, CI system, lint/type tools | Not found | D-6 |
| 6 | Low | Stale wording: boundary tooling "deferred"; "no stages after Stage 1.3"; "Stage 1.2 complete" status rows; PRODUCT-SCOPE §12 without ADR-005–009 references | Doc-internal | D-2 to D-5 |
| 7 | Info | `BOUNDED-CONTEXTS.md` changed at 19:07, shortly before inspection; later edits are not covered by this report | — | `docs/architecture/BOUNDED-CONTEXTS.md` |

---

## 10. Not inspected or unable to verify

- **Git-based checks** (branch, log, remotes, ignored files, `git diff`): not applicable; no repository.
- **Contents of `/private/tmp/fp*`**: listed only, not compared with the workspace.
- **Whether the docs are final**: unable to verify. One file changed three minutes before inspection (§3).
- **Runtime behavior, tests, licences of dependencies**: nothing exists to run or scan.
- **Host services** (a running PostgreSQL server, Docker daemon state): not checked; only CLI versions were read.
- The parent directory `~/Projects` (contains `.DS_Store`, `.shots`, other entries) was not inspected beyond confirming it has no `.git`.
- Anchor checking in the link script uses a GitHub-style slug approximation; all 230 links passed it.

---

## 11. Validation of this inspection

- **Snapshot diff** (`diff /tmp/fp_s21_before.txt /tmp/fp_s21_after.txt`): the only additions are `./docs/engineering` and `./docs/engineering/REPOSITORY-INSPECTION.md`. The only other changed line is the mtime of the `./docs` directory, caused by adding the subdirectory. All 16 pre-existing files have identical size, mtime, and sha256.
- **Link check**: inline script over the Markdown files, resolving relative paths and `#anchors`. Stage 1 docs: 230 links, 0 broken. This report: 16 links, 0 broken.
- **ADR titles**: every `ADR-00N: Title` in §8.1 was compared by script with line 1 of the ADR file; all 9 match.
- **Paths**: every `docs/...` path cited in this report was checked by script to exist.

---

## 12. Recommendation for Stage 2.2 (not implemented)

**Owner decisions needed first:**
1. **Initialize Git** and make the Stage 1 docs the first commit, before any other change. Also: default branch name, whether and where to host a remote. Not done here, by rule.
2. **Define Stage 2.2 scope** (D-1), since no doc does.
3. **Toolchain choices** (D-6): Python version to pin, package manager and lockfile (host has `uv` and `pip3`; nothing is decided), lint/format/type-check tools, CI system for FlakePilot itself. All must be permissively licensed (ADR-007 item 5).
4. Whether to refresh the stale wording in D-2 to D-5 (doc-only edits).

**Suggested order once confirmed** (within ADR-001 to ADR-009; nothing new):
1. `LICENSE` (Apache-2.0) and a single Python project manifest with the pinned version.
2. The package skeleton for one deployable: the 11 contexts with domain, application, and infrastructure layers, the shared kernel, and the system-wide interface layer (ADR-001, ADR-002, DEPENDENCY-RULES §1, §6).
3. **import-linter contracts before any feature code**: layer direction, the 36-edge §3.1 matrix as the only allowed cross-context edges, the §5 DIAG isolation invariant, and a ban on FastAPI, SQLAlchemy, asyncpg, and vendor SDK imports in domain code (ADR-007 item 6).
4. pytest with a trivial passing test, and CI running import-linter, pytest, and a dependency-licence check as merge gates (ADR-007 item 7).
5. Do **not** add a broker, cache, lock service, or workflow engine (ADR-006 decisions 3–5), or psycopg (ADR-007 item 5). Docker deployment artifacts are a later stage (ADR-008 item 1) unless the owner moves them.

---

## 13. Post-inspection follow-up (2026-10-20 19:20 IST)

Recorded at `Tue Oct 20 19:20:29 IST 2026` (`date`). This section was added **after** the inspection. Nothing in §1–§12 has been changed; those sections stay accurate for 19:10.

### 13.1 Git initialized after the inspection

The coordinator ran `git init -b main && git add docs` with the owner's approval. This inspection did not run it. Read-only evidence, gathered with `GIT_OPTIONAL_LOCKS=0`; `.git/index` had the same sha256 before and after:

| Check | Result |
|---|---|
| `git rev-parse --is-inside-work-tree` / `--show-toplevel` | `true` / `/Users/priyanshujha/Projects/FlakePilot` |
| `git branch --show-current` | `main` |
| `git status --porcelain=v1 -b --untracked-files=all` | `## No commits yet on main`; 17 files `A` (staged); no unstaged changes and no untracked files at that time |
| `git diff --cached --stat` | `17 files changed, 3109 insertions(+)`: the 16 Stage 1 docs plus this report |
| `git log --oneline -n 20` | `fatal: your current branch 'main' does not have any commits yet` |
| `git remote -v` | No remotes |
| `user.name` / `user.email` (local, global, system) | Not set at any level; no `GIT_AUTHOR_*` or `GIT_COMMITTER_*` environment variables |
| Staged blobs vs `/tmp/fp_s21_after.txt` | All 17 staged blobs have the same sha256 as the post-inspection snapshot |

**Effect on finding 1 (§9, High, "No version control"): partially addressed.** A repository exists and the docs are staged. There is still **no commit**, so the docs still have no durable history: staged content is only in `.git/index` and loose objects. A first commit needs a Git identity, which isn't configured.

**This section makes the staged copy of this report stale.** The index holds the pre-follow-up version, so `git status` shows this file as `AM`. It must be re-staged (`git add docs/engineering/REPOSITORY-INSPECTION.md`) before the first commit. The inspection did not stage it.

### 13.2 Toolchain candidates vs the repo and accepted ADRs

The owner chose these candidates in chat on 2026-10-20. **No ADR or other doc records them yet** (§4 greps: no match for a Python version, `uv`, or the other tools). The repo still has no manifest or config: excluding `.git/`, every file is `.md`, and there is no `pyproject.toml`, `uv.lock`, `.python-version`, Ruff or mypy config, `.importlinter`, `.github/`, or `LICENSE`.

| Candidate | Fits accepted ADRs? | Licence (from general knowledge; **to confirm in the licence check**) | Notes |
|---|---|---|---|
| Python 3.12 | Yes. ADR-007 item 1 fixes Python but no version | PSF License (permissive) | The host has `Python 3.14.6`, not 3.12 (§4). FastAPI, SQLAlchemy, Alembic, asyncpg, import-linter, and pytest support for 3.12 is to be confirmed when pinning |
| uv plus `uv.lock` | Yes. ADR-007 leaves the package manager open | Apache-2.0 or MIT (dual) | Already on the host (`uv 0.11.23`). The lockfile also gives the licence-check tool a fixed input |
| GitHub Actions | Yes. ADR-007 item 7 needs CI merge gates and names no CI system. ADR-009 already chose GitHub as the code host | Hosted service, not shipped. The permissive-only rule covers FlakePilot code, Python dependencies, and shipped images (ADR-007 item 5). Third-party actions used in workflows: to confirm | "CI/CD for FlakePilot" is listed as deferred (PRODUCT-SCOPE §12) |
| Ruff (lint and format) | Yes, if it **supplements** import-linter. ADR-007 item 6 makes import-linter the boundary checker; Ruff must not replace it | MIT | Development tool only |
| mypy | Yes. Not mentioned in any ADR | MIT | Development tool only |

**Conflicting:** none found. The candidates don't touch the fixed choices (FastAPI, SQLAlchemy plus Alembic, asyncpg, import-linter, pytest, Apache-2.0) and introduce no broker, cache, workflow engine, or psycopg.

**Recommendation (not done here):** open Stage 2.2 with a short engineering-toolchain ADR, the next free number in the [ADR index](../adr/README.md). It should record the five candidates, the version pin, and the still-open licence-check tool. The same change should fix the stale "deferred" tooling wording in [DEPENDENCY-RULES §7](../architecture/DEPENDENCY-RULES.md#7-enforcement) and the [ADR-002](../adr/ADR-002-domain-layering.md) follow-up (D-3), and optionally D-2, D-4, and D-5.

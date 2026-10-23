# Architecture Decision Records

The ADR index, plus the Stage 1.4 technology decision inventory. ADRs are numbered `ADR-NNN-kebab-title.md`. Deferred decisions and their constraints are also tracked in [SYSTEM-ARCHITECTURE §10](../architecture/SYSTEM-ARCHITECTURE.md#10-unresolved-and-deferred-decisions).

## Index

| ADR | Title | Status |
|---|---|---|
| [ADR-001](ADR-001-modular-monolith.md) | Modular monolith with strict domain boundaries | Accepted |
| [ADR-002](ADR-002-domain-layering.md) | Per-domain layering and cross-domain dependency rules | Accepted |
| [ADR-003](ADR-003-provider-adapters.md) | Provider ports and adapters owned by External integrations | Accepted |
| [ADR-004](ADR-004-control-plane-and-execution-plane.md) | Separate trusted control plane from untrusted execution plane | Accepted |
| [ADR-005](ADR-005-system-of-record-database.md) | One transactional relational database as the control-plane system of record | Accepted |
| [ADR-006](ADR-006-durable-jobs-in-the-database.md) | Durable jobs, timers, outbox dispatch, and coordination in the system-of-record database | Accepted |
| [ADR-007](ADR-007-implementation-stack-and-licensing.md) | Implementation stack, UI and API approach, licensing, and quality gates | Accepted |
| [ADR-008](ADR-008-deployment-and-trust-model.md) | Initial deployment model, execution trust model, and sandbox class | Accepted |
| [ADR-009](ADR-009-initial-provider-adapters.md) | Initial provider adapters and telemetry export | Accepted |
| [ADR-010](ADR-010-engineering-toolchain-and-quality-gates.md) | Engineering toolchain, repository conventions, and CI quality gates | Accepted |
| [ADR-011](ADR-011-authentication-sessions-and-tokens.md) | Authentication sessions, CI upload tokens, and access-check details | Accepted |
| [ADR-012](ADR-012-event-envelope-and-outbox.md) | Event envelope, per-context outbox, and IAM event names | Accepted |
| [ADR-013](ADR-013-stage-4-ingestion-scope-and-deviations.md) | Stage 4 ingestion scope and deviations | Accepted |
| [ADR-014](ADR-014-stage-5-ai-platform-and-deviations.md) | Stage 5 AI platform scope and deviations | Accepted |
| [ADR-015](ADR-015-stage-6-grant-administration-and-deviations.md) | Stage 6 grant administration scope and deviations | Accepted |
| [ADR-016](ADR-016-code-host-access-verification-scope-and-deviations.md) | Code-host access verification scope and deviations | Accepted |

## Stage 1.4 decision inventory (2026-10-20)

Abbreviations: **PS** = [PRODUCT-SCOPE](../product/PRODUCT-SCOPE.md), **SA** = [SYSTEM-ARCHITECTURE](../architecture/SYSTEM-ARCHITECTURE.md), **DR** = [DEPENDENCY-RULES](../architecture/DEPENDENCY-RULES.md), **BC** = [BOUNDED-CONTEXTS](../architecture/BOUNDED-CONTEXTS.md).

| # | Decision area | Decision | Source | Still open (non-blocking unless marked) |
|---|---|---|---|---|
| 1 | Backend framework and application composition | Modular monolith; Python; **FastAPI**, interface layer only | ADR-001/002; ADR-007 | ASGI server (implementation detail) |
| 2 | Primary relational database | **PostgreSQL** system of record, with the owner's conditions | ADR-005 | Namespace mechanism; optional database-level defenses (implementation details) |
| 3 | ORM and schema migrations | **SQLAlchemy plus Alembic**, infrastructure layer only; controlled per-context migrations; **asyncpg**, async | ADR-005 decision 7; ADR-007 | — |
| 4 | Workflow orchestration | Database-backed jobs with leases; `SKIP LOCKED` claiming; polling is the source of truth. Stage 6 ships an in-house module (`orch.jobs`, relay to `aud.events`) | ADR-006 | Tuning. `LISTEN/NOTIFY`, outbox pruning, and retention deletion are not built |
| 5 | Queue, cache, ephemeral coordination | No broker, cache, or lock service; PostgreSQL serves; notifications only as a wake-up hint | ADR-006 | — |
| 6 | AI provider abstraction and model config | Port plus test fake only; no provider at launch. Stage 5 wires `NullAiAssist` only ([ADR-014](ADR-014-stage-5-ai-platform-and-deviations.md)) | ADR-003; ADR-009 | The first network adapter, later |
| 7 | Browser and code-execution isolation | ADR-004 unchanged. Hardened ephemeral containers on a dedicated execution runtime. Co-location only in explicit evaluation installs. Playwright only inside sandboxes. microVM-class required before a hosted multi-tenant service | ADR-004; ADR-008; ADR-009 | Hardening specifics (implementation details). Shipped images: Python-level permissive rule, plus a **pre-release licence review gate** (FQ-13, ADR-007) |
| 8 | Artifact and evidence storage | S3-compatible storage plus local filesystem behind the artifact port; database keeps references and hashes | ADR-005; ADR-009 | Local-filesystem encryption at rest (implementation detail); BC-Q5 (data model) |
| 9 | Authn, authz, tenant isolation | Sign in with the same GitHub App, authentication only. Authorization and tenant scoping enforced, including across orgs in one deployment. Secrets in an encrypted, adapter-only table; pluggable | ADR-008; ADR-009; SA §7 | Encryption scheme and key provisioning; minimal scopes (implementation details) |
| 10 | Frontend framework and API contract | Server-rendered pages; artifacts on a separate origin; code-first API with generated OpenAPI | ADR-007 | Template engine (implementation detail); BC-Q1 (interface/API design) |
| 11 | Observability and telemetry | OpenTelemetry export; audit stays in PostgreSQL | ADR-009; ADR-005 | Signals and collector topology (implementation details) |
| 12 | Testing and quality gates | import-linter plus pytest; merge gates including a licence check; Apache-2.0 project licence | ADR-007 | Licence-check tool (implementation detail) |

Deployment: self-hosted with documented Docker-based deployment, portable, hosted service later ([ADR-008](ADR-008-deployment-and-trust-model.md)). The deployment artifacts belong to a later stage. Ingestion in v1 is push only ([ADR-009](ADR-009-initial-provider-adapters.md)).

**Not reopened:** BC-Q1 and BC-Q3–BC-Q7 keep the design areas assigned in [BC §6](../architecture/BOUNDED-CONTEXTS.md#6-open-questions).

## Owner answers (confirmed 2026-10-20)

| ID | Answer | Recorded in |
|---|---|---|
| OQ-1 | Python | ADR-007 |
| OQ-2 | Self-hosted with Docker-based deployment; control plane and execution workers separate; tenant isolation enforced; hosted service later; portable (verbatim in ADR-008) | ADR-008 |
| OQ-3 | One trusted org per deployment. ADR-004 is not relaxed | ADR-008 |
| OQ-4 | PostgreSQL | ADR-005 |
| OQ-5 | Settled by FQ-2 | ADR-008 |
| OQ-6 | S3-compatible object storage, plus local filesystem for single node | ADR-009 |
| OQ-7 | Sign in with the code host (authentication only) | ADR-009 |
| OQ-8 | Encrypted table in the main database by default; dedicated secret manager pluggable; values resolved only in INT adapters | ADR-005; ADR-009 |
| OQ-9 | No AI provider at launch; port plus test fake | ADR-009 |
| OQ-10 | Playwright; JUnit XML first | ADR-009 |
| OQ-11 | GitHub | ADR-009 |
| OQ-12 | Server-rendered pages; untrusted artifacts on a separate origin | ADR-007 |
| OQ-13 | Code-first API, OpenAPI generated | ADR-007 |
| OQ-14 | OpenTelemetry | ADR-009 |
| OQ-15 | Permissive project licence; permissive dependencies only | ADR-007 |
| OQ-16 | ADR-005 accepted with conditions (verbatim in ADR-005) | ADR-005 |
| FQ-1 | ADR-006 accepted | ADR-006 |
| FQ-2 | Hardened ephemeral containers on a dedicated execution runtime (binding for v1); microVM-class required before a hosted multi-tenant service | ADR-008 |
| FQ-3 | Co-location on the control-plane host only in evaluation installs (explicit opt-in); production needs a dedicated execution host | ADR-008 |
| FQ-4 | More than one org per deployment allowed; trusted for sandbox-host co-tenancy only; authorization and data isolation fully enforced | ADR-008 |
| FQ-5 | FastAPI, interface layer only | ADR-007 |
| FQ-6 | SQLAlchemy plus Alembic, infrastructure layer only | ADR-005; ADR-007 |
| FQ-7 | Async with asyncpg; psycopg excluded (LGPL) | ADR-007 |
| FQ-8 | import-linter plus pytest | ADR-007 |
| FQ-9 | Apache-2.0 | ADR-007 |
| FQ-10 | Permissive-only covers FlakePilot code and FlakePilot-shipped images; third-party services referenced in deployment docs are exempt. Narrowed for image contents by FQ-13 | ADR-007 |
| FQ-11 | Sign-in uses the same GitHub App as the repo installation | ADR-009 |
| FQ-12 | Push ingestion only in v1 | ADR-009 |
| FQ-13 | Option (a): permissive-only covers FlakePilot's code and Python dependencies. OS packages, browsers, and media tools in shipped images are unmodified third-party components with their licence notices. A licence review of each image's contents, including required notices and source availability, must pass before any image is released | ADR-007 |
| TQ-1 to TQ-6 | Licence checker pip-licenses; full locked environment for the gate, runtime-only for notices; mypy `--strict` everywhere with recorded overrides; context-name subpackages under `src/flakepilot/`; CI on Python 3.12 only; ADR-010 accepted. Also owner-chosen: Python 3.12, uv with a committed `uv.lock`, GitHub Actions, Ruff, mypy | ADR-010 |

## Remaining owner questions

None. All Stage 1.4 owner questions are answered. Open items are implementation details for later design, listed in each ADR.

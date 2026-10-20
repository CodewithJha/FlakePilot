# ADR-007: Implementation stack, UI and API approach, licensing, and quality gates

| | |
|---|---|
| Status | **Accepted** by the project owner, 2026-10-20: OQ-1, OQ-12, OQ-13, OQ-15, and FQ-5 to FQ-10 and [FQ-13](README.md#owner-answers-confirmed-2026-10-20) |
| Related | [ADR-001](ADR-001-modular-monolith.md), [ADR-002](ADR-002-domain-layering.md), [ADR-003](ADR-003-provider-adapters.md), [ADR-005](ADR-005-system-of-record-database.md), [DEPENDENCY-RULES §1, §7](../architecture/DEPENDENCY-RULES.md#7-enforcement), [SYSTEM-ARCHITECTURE §2, §3.2](../architecture/SYSTEM-ARCHITECTURE.md#32-boundary-rules) |

## Context

Constraints already in the docs:
- The domain layer must not depend on frameworks, ORMs, HTTP, or provider SDKs (DEPENDENCY-RULES §1).
- Automated boundary checks must fail the build (DEPENDENCY-RULES §7). Every port needs a conformance suite and a fake (ADR-003 decision 5).
- The interface layer authenticates only (SYSTEM-ARCHITECTURE §2).
- Untrusted HTML, DOM, or trace artifacts must never run script in FlakePilot's origin; the mechanism was deferred (SYSTEM-ARCHITECTURE §3.2 rule 4).
- The control plane is one deployable (ADR-001).
- Acceptance needs a benchmark, zero security violations, and a fault-injection recovery suite (PRODUCT-SCOPE §10).
- Browser recordings, including video, are required evidence (PRODUCT-SCOPE Workflow D).

## Decision

**Binding (owner-confirmed):**
1. **Python** for the whole control plane: every context, the interface layer, workers, and INT adapters. Code that runs *inside* sandboxes is the user's own repo and tests, and FlakePilot doesn't choose its language.
2. **Server-rendered pages**, served by the interface layer of the same deployable (ADR-001). No separate frontend application in v1.
3. **Untrusted artifacts are served only from a separate origin** (a distinct host with no FlakePilot session credentials), through short-lived scoped read handles from the artifact port (SYSTEM-ARCHITECTURE §4), or as downloads. They are never inlined into FlakePilot pages. This settles the mechanism SYSTEM-ARCHITECTURE §3.2 rule 4 deferred.
4. **Code-first HTTP API, with an OpenAPI schema generated** from interface-layer definitions. Request and response models live in the interface layer and are separate from domain entities (ADR-002). The generated schema is versioned with the code.
5. **Licensing.**
   - The project licence is **Apache-2.0** (FQ-9).
   - **The permissive-only rule covers** FlakePilot's code, its language-level dependencies, and the **images FlakePilot ships**: the control-plane image and sandbox base images (FQ-10). Inside those images, FQ-13 narrows the rule (see "Shipped images" below).
   - **Exempt:** third-party services that deployment docs only reference and that the deployer runs, such as the PostgreSQL server or an S3-compatible store (FQ-10).
   - **The psycopg driver family is excluded** because it is LGPL. **asyncpg** (Apache-2.0) is used instead (FQ-7).
   - **Shipped images (owner-confirmed, [FQ-13](README.md#owner-answers-confirmed-2026-10-20) option a).** The permissive-only rule applies to FlakePilot's own code and its Python dependencies. OS packages, browsers, and media tools in FlakePilot-shipped images (the control-plane image and sandbox base images) are **unmodified third-party components**, shipped with their licence notices. Examples are copyleft system packages and Playwright's browser and video-capture builds, whose licences include MPL, LGPL, or GPL.
   - **Release gate:** a licence review of each image's contents must pass **before any image is released**. The review confirms that every component is unmodified, that every licence notice is included, and that source is available wherever a component's licence requires it.
6. **Stack** (owner-confirmed, FQ-5 to FQ-8):
   - **FastAPI**, **interface layer only**. FastAPI and its request and response models never appear in the domain or application layers.
   - **SQLAlchemy plus Alembic**, **infrastructure-layer persistence adapters only**, giving controlled per-context migrations (ADR-005 decision 7). No ORM in the domain (DEPENDENCY-RULES §1).
   - **Async throughout, with asyncpg**.
   - **import-linter** for boundary checks: its contracts encode the DEPENDENCY-RULES §1 layers and the §3.1 matrix, and forbid web-framework, ORM, driver, and vendor-SDK imports in domain code.
   - **pytest** for tests.
7. **Quality gates** for merging. These restate requirements that already exist, plus a licence check that follows from item 5:
   - import-linter boundary checks (DEPENDENCY-RULES §7).
   - Port conformance suites against every adapter and fake (ADR-003).
   - Tests that outcomes can be recomputed from stored inputs, for TI and VER (PRODUCT-SCOPE Workflows C and G).
   - A dependency-licence check.
   - The fault-injection recovery suite and security checks feed the PRODUCT-SCOPE §10 acceptance criteria.

**Implementation details for later design** (these don't need an owner decision; every library must be permissive):
- The template engine for server-rendered pages (a permissive engine such as Jinja2).
- The ASGI server.
- The tool for the dependency-licence check.

## Rationale

- Server-rendered pages in the same deployable keep v1 to one build, one runtime, and one authentication path (ADR-001).
- A separate artifact origin is the most direct way to stop artifact script from reaching FlakePilot sessions (ADR-004 decision 4).
- Code-first OpenAPI keeps the schema in sync with the interface layer without a second source of truth. Interface-only models keep domain types private (ADR-002).
- These libraries fit the layering. FastAPI stays at the edge. SQLAlchemy works without domain classes inheriting from ORM base classes, and supports a PostgreSQL schema per context and `SKIP LOCKED` (ADR-005, ADR-006). import-linter expresses the dependency rules declaratively.
- The permissive policy matches an Apache-2.0 project, and the licence check makes it something CI can enforce.

## Alternatives considered

| Alternative | Why rejected |
|---|---|
| TypeScript, Go, or a JVM language (OQ-1 options) | The owner chose Python |
| SPA frontend or API and CLI first (OQ-12 options) | The owner chose server-rendered pages. An SPA would add a second build and deployable for no documented need |
| Schema-first API (OQ-13 option) | The owner chose code-first with a generated schema |
| Rendering artifacts inline with sanitization | Sanitizing untrusted HTML is error-prone. A separate origin removes the class of problem (SYSTEM-ARCHITECTURE §3.2) |
| Litestar or Django (FQ-5 options) | The owner chose FastAPI. Django's ORM-centric models would also push toward domain coupling (ADR-002) |
| pg8000 with a sync stack (FQ-7 option) | The owner chose async with asyncpg |
| psycopg | LGPL, excluded by item 5 |
| Copyleft dependencies allowed (OQ-15 option) | The owner chose permissive only |

## Consequences

- **Positive:** one language and one deployable. Rendering is safe by design. The layering is enforced by import-linter, and the licence policy by CI.
- **Negative:** a separate artifact origin needs its own hostname and configuration in self-hosted installs ([ADR-008](ADR-008-deployment-and-trust-model.md)). LGPL drivers are off-limits. **Every image release depends on passing the licence-review gate** (item 5).
- **Follow-ups:** set up the image licence review as a release gate when images are first produced. The implementation details above.

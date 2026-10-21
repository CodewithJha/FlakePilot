# ADR-009: Initial provider adapters and telemetry export

| | |
|---|---|
| Status | **Accepted** by the project owner, 2026-10-20: OQ-6 to OQ-11, OQ-14, FQ-11, FQ-12. The last column of the table lists implementation details for later design; none needs an owner decision. Shipped browser images follow the licence rule and release gate in [ADR-007](ADR-007-implementation-stack-and-licensing.md) item 5 (FQ-13) |
| Related | [ADR-003](ADR-003-provider-adapters.md) (INT owns provider ports and adapters), [ADR-004](ADR-004-control-plane-and-execution-plane.md), [ADR-005](ADR-005-system-of-record-database.md), [ADR-007](ADR-007-implementation-stack-and-licensing.md), [ADR-008](ADR-008-deployment-and-trust-model.md), [SYSTEM-ARCHITECTURE §4](../architecture/SYSTEM-ARCHITECTURE.md#4-ports-architectural-interfaces) |

## Context

SYSTEM-ARCHITECTURE §4 defines vendor-neutral ports with required guarantees. ADR-003 puts every provider port contract and every provider adapter in INT: adapters are first-party, chosen by configuration, and each must pass the port's conformance suite. Domain and application layers never see vendor types. PRODUCT-SCOPE §12 deferred the first code host, report format, browser framework, AI provider, identity provider, and storage choices. The owner chose them on 2026-10-20.

## Decision

**Binding for every adapter below:** it implements an existing port unchanged. Provider adapters live in INT. The telemetry exporter implements AUD's internal telemetry-export port (SYSTEM-ARCHITECTURE §4) in AUD's infrastructure layer, so no matrix edge is added ([DEPENDENCY-RULES §3.1](../architecture/DEPENDENCY-RULES.md#31-matrix-rows-depend-on-columns)). Vendor types and vendor names never leave the adapter. It passes the port's conformance suite. It's selected by configuration (ADR-003 decisions 1, 5, 7). **No port gains an operation**, and **no context gains a dependency**.

| Port | First adapter (owner-confirmed) | Binding constraints | Implementation detail for later design (doesn't block) |
|---|---|---|---|
| **Code host** (REPO) | **GitHub** app installation | Short-lived installation tokens kept inside the adapter. A read tier, plus an opt-in write tier (branches under the prefix, PRs). The port still has **no merge and no push to existing branches**, and the adapter rejects branch names outside the prefix (SYSTEM-ARCHITECTURE §4). PRODUCT-SCOPE R6 applies: the platform's contents-write permission can reach unprotected branches, so the adapter enforces the prefix, writes are audited, and branch protection is recommended | The exact minimal permission set for each tier |
| **Identity provider** (IAM) | **Sign in with the code host** (GitHub), **using the same GitHub App as the repo installation** (FQ-11) | Authentication only. Code-host org, team, or other claims are **never** FlakePilot authorization (PRODUCT-SCOPE §3.1). The identity port and the code-host port stay separate adapters, even though they share one app registration. The app's credentials stay inside INT adapters. Signing in grants no repository access by itself. The linked identity lets REPO run repository-permission checks through the code-host port, not through identity-provider claims | The minimal user-authorization scopes |
| **CI / report source** (TI) | **JUnit XML** first. **v1 is push ingestion only** (FQ-12): CI uploads reports with ingestion tokens, from any CI. The v1 adapter doesn't use the port's webhook-triggered fetch operation | **Hardened parsing**: no external entity or DTD resolution, plus size, depth, and count limits (SYSTEM-ARCHITECTURE §3.2 rule 4). Deterministic normalization. Missing idempotency-key fields are reported (PRODUCT-SCOPE Workflow B) | The parser library (permissive, with hardened XML handling). Report formats beyond JUnit XML are future adapters |
| **Sandbox execution** (EXEC) | Hardened ephemeral containers on a dedicated execution runtime ([ADR-008](ADR-008-deployment-and-trust-model.md), FQ-2). First browser framework: **Playwright** | Playwright and its browsers run **only inside sandboxes**, never in the control plane (ADR-004). Framework-specific invocation and capture (traces, screenshots, video, console, network log) stay inside the adapter. EXEC and TI see generic artifact kinds only. Trace viewing happens only through the artifact origin or a download ([ADR-007](ADR-007-implementation-stack-and-licensing.md) item 3) | The contents of the sandbox base image, which must pass the pre-release licence review ([ADR-007](ADR-007-implementation-stack-and-licensing.md) item 5) |
| **Artifact storage** (TI, EXEC) | **S3-compatible object storage** (any provider, endpoint from configuration), plus a **local filesystem** adapter for single-node installs | Both meet the port guarantees: objects never overwritten, integrity hash, tenant-namespaced keys, short-lived scoped read handles served on the separate artifact origin (ADR-007), encryption at rest | How encryption at rest is provided for the local filesystem, for example deployment-level disk encryption |
| **Secret storage** (through REPO, DIAG, EXEC) | **Default: an encrypted table in the INT secret adapter's own PostgreSQL namespace** ([ADR-005](ADR-005-system-of-record-database.md) decision 6). **A dedicated secret-manager adapter can replace it** through configuration | Only the INT secret adapter reads or writes that namespace. Values are encrypted with a key **held outside the database**, available to control-plane processes only and **never to execution workers**. Values resolve only inside adapters at the point of use. Domain and application layers, events, audit, and logs see references only (SYSTEM-ARCHITECTURE §7.4; ADR-003 decision 4). Database backups therefore contain ciphertext only | The encryption scheme (authenticated encryption, key rotation), how the key is provisioned, and the crypto library (permissive) |
| **AI provider** (DIAG) | **None at launch.** The AI-port contract plus an in-memory **fake for tests only** | AI stays optional and off by default (P6; PRODUCT-SCOPE §4). With no adapter configured, DIAG reports `unavailable`. That's a policy state, not a production fake (ADR-003 decision 8) | The first real adapter is a future decision. It isn't needed for v1 |
| **Telemetry export** (AUD's internal port, SYSTEM-ARCHITECTURE §4) | **OpenTelemetry** (vendor-neutral standard), exporter endpoint from configuration | Telemetry carries no secret values and no raw untrusted content (the same rule as events, SYSTEM-ARCHITECTURE §5). **The audit log stays in PostgreSQL** and is never sourced from telemetry (ADR-005) | Which signals are exported (traces, metrics, logs), and the collector topology |

## Rationale

- Every choice sits behind a port that already exists, so a different provider later means a new adapter, not a domain change (P4).
- With no AI adapter, the product's core is exercised from day one without AI (P6). The fake keeps DIAG's contract tested.
- The encrypted-table default avoids running an extra service in self-hosted installs (ADR-008), while keeping values reachable only through the adapter. The pluggable secret-manager adapter keeps a stronger option open.
- Choosing both S3-compatible storage and the local filesystem covers single-node and multi-node installs without tying FlakePilot to one vendor (ADR-008 portability).

## Alternatives considered

| Alternative | Why rejected |
|---|---|
| GitLab, Bitbucket, Gitea or Forgejo first (OQ-11 options) | The owner chose GitHub. Others can be added later as adapters |
| A generic OIDC identity provider (OQ-7 option) | The owner chose sign-in with the code host. A generic OIDC adapter can be added later behind the same port |
| Cypress or Selenium; framework-native JSON reports (OQ-10 options) | The owner chose Playwright and JUnit XML |
| A dedicated secret manager as the default (OQ-8 option) | It's an extra service for self-hosters. It stays available as a pluggable adapter |
| An AI provider at launch (OQ-9 options) | The owner chose none |

## Consequences

- **Positive:** a minimal set of first adapters, each replaceable, with no vendor types in domain code.
- **Negative:**
  - Using GitHub for both the code host and sign-in concentrates dependence on one provider in v1, though only behind ports.
  - The encrypted-table default puts encrypted secret values in the same database backups, which makes key management the critical control.
  - GitHub's permission model needs the R6 mitigations.
- **Follow-ups:** the implementation details in the last column.

## Stage 7 implementation note

The code-host read used to pin a reproduction is not the GitHub App installation flow in the table above. It is an optional static bearer (`FLAKEPILOT_GITHUB_CONTENTS_TOKEN`) kept inside the adapter and sent only as a header. With no token the adapter does not call GitHub, and the execution is `inconclusive` / `source_unverified`. There is no write tier, no branch prefix, and no installation-token minting. Redirects and non-HTTPS URLs are rejected. Playwright is not imported by the control plane. The sandbox port's evaluation adapter is described in [EXECUTION](../operations/EXECUTION.md).

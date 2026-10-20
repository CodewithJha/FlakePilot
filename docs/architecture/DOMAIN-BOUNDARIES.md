# FlakePilot — Domain Boundaries

| | |
|---|---|
| Status | **Stage 1.2 complete**, 2026-10-20. ADR-001 to ADR-004 accepted by the project owner |
| Owns | What each domain is responsible for, the data it owns, its public API (conceptual), and the events it publishes. Exactly one owner per responsibility |
| Defers to | Allowed dependencies, the graph, and layer rules: [DEPENDENCY-RULES.md](DEPENDENCY-RULES.md). Runtime interactions: [SYSTEM-ARCHITECTURE.md §8](SYSTEM-ARCHITECTURE.md#8-workflow-interactions-aj). Product behavior: [PRODUCT-SCOPE.md](../product/PRODUCT-SCOPE.md). Per-context language, invariants, contracts, and context interactions: [BOUNDED-CONTEXTS.md](BOUNDED-CONTEXTS.md) |

Domains are modules of one monolith ([ADR-001](../adr/ADR-001-modular-monolith.md)), not services or directories. Each domain owns its persistence exclusively. Others refer to its records by ID and read them only through its query API.

**Shared kernel** (not a domain, contains no business rules): identifiers (org, repo, investigation, and so on), principal/actor reference, correlation ID, event envelope, typed error set, clock. Changing it requires an ADR ([DEPENDENCY-RULES §6](DEPENDENCY-RULES.md#6-shared-kernel)).

---

## 1. Domains

### IAM — Product and identity management
- **Responsibilities:**
  - Principals: users, ingestion tokens, `system`.
  - Mapping of identity-provider subjects, and linked code-host identities.
  - Orgs and membership. Org roles (platform operator, org admin) and repo grants (`repo.view`, `repo.investigate`, `repo.approve`, `repo.configure`). The last-admin guard. Only org admins grant `repo.approve` (D2).
  - **The authorization decision point.** It combines FlakePilot grants with code-host access through the `CodeHostAccessVerifier` port it defines (implemented by REPO).
  - Sessions, quotas (decisions and usage accounting), org retention policy, break-glass grants, org suspension.
- **Data owned:** principals, identity links, orgs, memberships, grants, ingestion-token hashes, sessions, quota limits and usage, retention policy, break-glass records.
- **Public API:** authenticate, authorize(principal, action, resource), manage members and grants, issue or revoke ingestion tokens, reserve and report quota, get retention policy, org member count.
- **Events:** `member.*`, `grant.issued`, `grant.revoked`, `ingestion_token.*`, `quota.changed`, `quota.denied`, `retention_policy.changed`, `break_glass.*`, `org.suspended`, `integration.config_changed` (identity provider).
- **Must not:** know about investigations, patches, or approvals. Treat identity-provider claims as authorization.

### REPO — Repository access
- **Responsibilities:**
  - Repo connections and their states (`pending_validation`, `active`, `validation_failed`, `degraded`, `disconnected`).
  - Installation lifecycle. Validation of permission tiers.
  - **Versioned repo config:** commands, environment definitions, regression set, acceptance-criteria defaults, patch-scope rules, auto-advance policy, write-tier flag, egress allowlist, test-environment secret *references*.
  - Code-host reads: SHA resolution and reachability, source snapshots, file ranges.
  - **Code-host write operations** (branch at SHA under prefix, commit, PR, find-existing). Called only by APPR.
  - Implements IAM's `CodeHostAccessVerifier`. Watches PR state read-only.
- **Data owned:** connections, installation references, config versions, secret references for test environments, PR references.
- **Public API:** connect, complete installation, validate, get config (version), set a test-environment secret (write-only), resolve SHA, check SHA reachable, get snapshot, read file range, apply approved patch (branch + commit + PR, idempotent by key), get PR state.
- **Events:** `repo_connection.state_changed`, `repo_config.version_created`, `installation.changed`, `integration.config_changed` (code host).
- **Must not:** merge or push to existing branches (the port has no such operation). Hand credentials to anyone. Decide whether an approval is valid.

### TI — Test intelligence
- **Responsibilities:**
  - Ingestion (acceptance, validation, idempotency) and normalization.
  - Test identity and per-test history. Failure signatures and signature matching.
  - **Evidence bundles** (versioned, immutable per version).
  - **Reproduction-outcome evaluation** (`reproduced`, `not_reproduced`, `different_failure`, `execution_failure`, `unsupported`), recomputed from raw run outputs.
  - **Failure classification** (`flaky`, `deterministic_regression`, `infrastructure_failure`, `inconclusive` plus reason codes, including `no_passing_baseline` per D4) using versioned rules.
  - Observed failure rate for VER.
- **Data owned:** uploads, raw-report artifact references, normalized runs and results, test identities, signatures, evidence-bundle versions, reproduction evaluations, classification versions, classification rule versions.
- **Public API:** accept upload, query history, assemble evidence, evaluate reproduction, classify, match signature, observed failure rate, get evidence items (read-only).
- **Events:** `upload.*`, `evidence.sealed`, `reproduction.evaluated`, `classification.recorded`.
- **Must not:** read DIAG output in any form. Open investigations (D3). Delegate a classification decision to anything outside its rules.

### ORCH — Investigation orchestration
- **Responsibilities:**
  - The investigation aggregate, and the **only writer** of investigation state (`queued`, `running`, `needs_input`, `awaiting_approval`, `applying`, `completed`, `failed`, `cancelled`, `archived`), the `stage` attribute, and resolutions (`patch_applied`, `closed` with reason).
  - Stage sequencing and auto-advance (reading REPO config). Stage-job leases and retries.
  - Cancellation propagation. Duplicate-investigation check (Q8). Opening approval requests in APPR. Archival.
- **Data owned:** investigations, stage jobs and attempts, transition history, cancellation intents.
- **Public API:** create (user only, D3), start stage, submit human patch, retry, retry application, cancel, close, archive, query.
- **Events:** `investigation.state_changed`.
- **Must not:** compute any outcome (classification, reproduction, verification) or approval validity. Pass DIAG output to TI or VER. Be depended on by any domain.

### EXEC — Browser and code execution
- **Responsibilities:**
  - Build run specs from REPO config. Sandbox lifecycle through the sandbox port (provision, execute, collect, teardown, orphan cleanup).
  - Run records with raw outputs. Environment fingerprints. Artifact export (manifest, hashes, partial or truncated labels).
  - Retention deletion of run artifacts. Onboarding dry run. Instance sandbox configuration (operator).
- **Data owned:** runs, run attempts, artifact manifests and references, environment fingerprints.
- **Public API:** start run, cancel run, get run records and artifacts (read-only), dry run.
- **Events:** `run.requested`, `run.started`, `run.finished`, `run.errored`, `run.cancelled`, `artifacts.exported`, `artifact.deleted`, `dry_run.finished`, `integration.config_changed` (sandbox).
- **Must not:** interpret results as outcomes (that's TI and VER). Choose commands outside the REPO config. Hold secret values outside the adapter.

### DIAG — AI-assisted diagnosis
- **Responsibilities:**
  - **The only path to AI providers.** Org data-sharing policy (`disabled`, `evidence_only`, `evidence_and_source`) and its enforcement. AI provider selection and credentials per org (through INT references).
  - Request building with delimited untrusted content. Request metadata recording.
  - Output-schema validation. Mechanical claim checks (`evidence_linked`, `unverified`, `contradicted`).
  - Diagnosis records (`produced`, `no_hypothesis`, `rejected`, `unavailable`).
  - **AI patch-candidate generation** (raw diff text only, validated by PATCH). AI-spend reporting to IAM.
- **Data owned:** data-sharing policy, AI config and credential references, request metadata, diagnoses, hypotheses, claim statuses, overrides, raw responses (only if the org opted in, PRODUCT-SCOPE D6).
- **Public API:** diagnose, generate patch candidate, get diagnosis (read-only), set policy and provider (org admin).
- **Events:** `ai_request.sent`, `diagnosis.recorded`, `patch_candidate.generated`, `override.recorded`, `integration.config_changed` (AI provider and policy).
- **Must not:** write to, or be read by, TI, VER, or APPR. Expose model confidence as anything other than "model-reported, uncalibrated". Give the model side-effect tools.

### PATCH — Patch proposals
- **Responsibilities:**
  - Patch versions (`validating`, `valid`, `invalid`, `superseded`, `applied`). Provenance (`ai` or `human` with author).
  - Precondition check against the classification (with an audited `inconclusive` override).
  - Validation rules: revision binding, path scope, size, content and secret scan, **masking** detection and labeling.
  - Patch export.
- **Data owned:** patch versions, diffs and hashes, validation reports, opaque hypothesis references.
- **Public API:** check classification precondition (ORCH, before an AI candidate is requested), submit candidate, get patch (diff, hash, base SHA, validity, masking label, human author), mark applied (called by ORCH only), export.
- **Events:** `patch_version.created`, `patch.validated`, `patch.superseded`, `patch.applied`.
- **Must not:** call AI providers or DIAG. Run builds or tests. Expose hypothesis content or confidence through its API.

### VER — Verification
- **Responsibilities:**
  - Acceptance-criteria snapshots (frozen, overrides stricter-only per Q9). N_p derivation.
  - Baseline, patched, and regression phases through EXEC. Reruns of new failures.
  - **Verification outcome** (`verified_pass`, `verified_failure`, `inconclusive`, `execution_failure`), computed only from run records and the snapshot. Supersession.
- **Data owned:** verifications, criteria snapshots, phase records, outcomes, reason codes.
- **Public API:** start verification, cancel, get latest non-superseded verification for a patch (read-only).
- **Events:** `verification.started`, `verification.completed`, `verification.superseded`, `verification.cancelled`.
- **Must not:** read DIAG. Trust results computed inside the sandbox. Change criteria after the snapshot.

### APPR — Human approval
- **Responsibilities:**
  - Approval requests (opened by ORCH, carrying the investigation creator).
  - **Eligibility**, including D2 separation of duties and masking acknowledgment.
  - Decision records (`approve` / `reject`, immutable). Approval states (`active`, `consumed`, `invalidated`).
  - **Staleness detection** (every PRODUCT-SCOPE Workflow H trigger; **not** target-branch drift, D1). TTL.
  - **Honoring an approval during application:** re-authorize, recheck staleness, call REPO's write operation, consume.
- **Data owned:** approval requests, decision records, approval states, invalidation records, application attempts.
- **Public API:** approve, reject, apply (approval ID), invalidate for investigation, get eligibility and approval status.
- **Events:** `approval_request.opened`, `decision.recorded`, `approval.invalidated`, `approval.eligibility_lost`, `application.attempted`, `application.succeeded`, `application.failed`, `approval.consumed`.
- **Must not:** merge. Approve anything other than a `verified_pass`. Read DIAG output or model confidence.

### INT — External integrations
- **Responsibilities:**
  - **All port contracts and all vendor adapters**: AI provider, code host, CI/report source, sandbox, artifact storage, secret storage, identity provider ([SYSTEM-ARCHITECTURE §4](SYSTEM-ARCHITECTURE.md#4-ports-architectural-interfaces)).
  - Port conformance suites and fakes.
  - Credential references and resolving secret values inside adapters.
  - Adapter health probes. Redaction of exported artifacts inside the export adapter.
- **Data owned:** credential references (values live only in the secret store), adapter health status.
- **Public API:** port contracts (called by domain application layers only), health status.
- **Events:** `integration.health_changed`. (`integration.config_changed` is published by the consuming domain that owns the config.)
- **Must not:** contain business rules. Depend on any domain. Be called by the interface layer. Authorize users. It has no user-facing use cases.

### AUD — Audit and observability
- **Responsibilities:**
  - The append-only audit log, built from **every** event envelope.
  - Audit queries and export. Correlation views.
  - Metrics for PRODUCT-SCOPE §10, derived from events. Telemetry export.
- **Data owned:** audit records, metric aggregates.
- **Public API:** query audit (authorized through IAM), export, metrics queries.
- **Events:** `audit.exported`.
- **Must not:** let anyone edit or delete audit records, operators included. Interpret or change domain state. Be called synchronously by other domains (audit arrives through the outbox).

---

## 2. Responsibility ownership index

Every responsibility has exactly one owner. Where two domains are involved, the split is shown.

**This index is authoritative** for the five responsibilities also listed in PRODUCT-SCOPE §8: evidence bundle, reproduction outcome, repo config, AI patch candidates, and the data-sharing policy. Port ownership follows [ADR-003](../adr/ADR-003-provider-adapters.md).

| Responsibility | Owner | Split, if any |
|---|---|---|
| Authentication (mapping to principal) | IAM | The identity provider does the actual authentication (through INT) |
| Authorization decision | IAM | Code-host permission fact supplied by REPO through the inverted port. Enforcement in every domain's application layer |
| Grants, including who may grant `repo.approve` (D2) | IAM | — |
| Quotas: limits, decisions, accounting | IAM | Checked at TI, ORCH, EXEC, DIAG |
| Retention **policy** | IAM | **Deletion** of owned data: TI (raw reports), EXEC (run artifacts). Archival: ORCH |
| Break-glass | IAM | Recorded by AUD |
| Repo connections, repo config, test-environment secret references | REPO | Values held in the secret store (INT) |
| Code-host reads and writes | REPO | APPR is the only caller of writes |
| Ingestion, normalization, history, signatures | TI | Format parsing in the CI port adapter (INT) |
| Evidence bundle | TI | — |
| Reproduction outcome | TI | Raw runs from EXEC |
| Failure classification | TI | — |
| Investigation lifecycle, stage sequencing, auto-advance, leases, cancellation | ORCH | Auto-advance *policy value* lives in REPO config |
| Sandbox lifecycle, run records, artifacts from runs | EXEC | Isolation from the sandbox provider (INT adapter) |
| All AI calls, data-sharing policy, claim checks, AI patch candidates | DIAG | — |
| Patch versions, validation, masking | PATCH | Candidate text from DIAG (AI) or a human |
| Acceptance criteria snapshot, verification outcome | VER | Runs through EXEC |
| Approval eligibility, decisions, staleness, honoring an approval | APPR | Code-host write performed by REPO |
| Port contracts, adapters, credential references, health | INT | Config entry in the consuming domain |
| Audit log, metrics, correlation views | AUD | Events produced by every domain |

# FlakePilot — Reliability, SLOs, and Initial Scaling Assumptions

| | |
|---|---|
| Status | **Stage 1.5**, 2026-10-20 |
| Owns | Service objectives and how they are measured, benchmark definitions, the failure-handling matrix, initial scaling assumptions, and the triggers for revisiting decisions |
| Defers to | Product metrics and provisional targets: [PRODUCT-SCOPE §10](../product/PRODUCT-SCOPE.md#10-success-metrics-and-provisional-acceptance-criteria). Retries, leases, idempotency, cancellation: [SYSTEM-ARCHITECTURE §5](SYSTEM-ARCHITECTURE.md#5-cross-cutting-runtime-mechanisms). Durable execution: [§6](SYSTEM-ARCHITECTURE.md#6-sync-vs-async-and-durable-execution). Workflow mechanics: [§8](SYSTEM-ARCHITECTURE.md#8-workflow-interactions-aj). Ownership: [BOUNDED-CONTEXTS](BOUNDED-CONTEXTS.md). Permitted dependencies: [DEPENDENCY-RULES §3.1](DEPENDENCY-RULES.md#31-matrix-rows-depend-on-columns). Decisions: [ADR-001](../adr/ADR-001-modular-monolith.md)–[ADR-009](../adr/ADR-009-initial-provider-adapters.md) |
| Not in scope | Code, dashboards, alert configuration, deployment files |

**Conventions**
- **TBD: requires benchmark (B-n)** means no evidence exists for a number yet. The target will be set from benchmark B-n, following the PRODUCT-SCOPE §10 calibration plan: measure a baseline, then set the target with explicit reasoning. **No number in this document is an estimate presented as a fact.**
- **PS §10 provisional** means the target already in PRODUCT-SCOPE §10, used by reference. It is not restated or changed here.
- **Absolute** means the target is zero violations. It's never a percentage and never provisional (PRODUCT-SCOPE §10, "Security and authorization violations").
- **Measurement.** Business events are emitted by the owning context, and AUD derives metrics from event envelopes (DEPENDENCY-RULES, envelope exception). Telemetry leaves through AUD's telemetry-export port, implemented in AUD's infrastructure layer with OpenTelemetry ([ADR-009](../adr/ADR-009-initial-provider-adapters.md); SYSTEM-ARCHITECTURE §4). How interface-layer and job-runtime telemetry reach that exporter is open item RO-4 (§5).
- **Reference environment for every benchmark**: the documented self-hosted production topology from [ADR-008](../adr/ADR-008-deployment-and-trust-model.md). That is the control-plane units (request-serving and worker roles of one build, [ADR-001](../adr/ADR-001-modular-monolith.md)), PostgreSQL, artifact storage, and a **dedicated execution host**. An evaluation co-located install may be measured separately, but its numbers never set production targets.

---

## 1. Service objectives

### 1.1 Objectives

| ID | Objective | What is measured, and who emits it | Window | Target and rationale | Validation | Status |
|---|---|---|---|---|---|---|
| SLO-1 | **API availability** | The share of interface-layer requests answered without a server-side error. Emitted by the interface layer, exported through AUD (RO-4) | TBD: requires benchmark (B-1) | TBD: requires benchmark (B-1). The control plane is one failure domain with PostgreSQL ([ADR-001](../adr/ADR-001-modular-monolith.md) consequences; [ADR-005](../adr/ADR-005-system-of-record-database.md)), so availability is per deployment and bounded by the operator's database | B-1, plus production telemetry | Requires benchmark |
| SLO-2 | **API latency** for synchronous operations ([SYSTEM-ARCHITECTURE §6](SYSTEM-ARCHITECTURE.md#6-sync-vs-async-and-durable-execution) sync rows) | Latency distribution per operation class: reads, decisions, config, ingestion acceptance. Emitted by the interface layer (RO-4) | TBD: requires benchmark (B-1) | TBD: requires benchmark (B-1); percentiles and thresholds are chosen at calibration | B-1 | Requires benchmark |
| SLO-3 | **JUnit XML ingestion latency** (accepted to queryable) | Time from `upload.received` to `upload.processed`, from TI envelopes | Per PS §10 | **PS §10 provisional** ("Ingestion latency"). The maximum report size it applies to is **TBD: requires benchmark (B-2)** | B-2 | Provisional (PS §10) |
| SLO-4 | **Ingestion correctness** | Duplicate results created by resubmission; rejections without a machine-readable reason. TI envelopes `upload.duplicate` and `upload.rejected` | Continuous | **Absolute**: no duplicate results (PS §10 "Recovery", **0 duplicates**), and every rejection carries a reason (PRODUCT-SCOPE Workflow B success criteria) | B-2 with resubmission and malformed-input cases; audit query reconciling `upload.duplicate` against stored results | Absolute |
| SLO-5 | **Investigation and verification progress** | Time per stage and from creation to verification outcome, from ORCH `investigation.state_changed` and VER `verification.started` / `verification.completed` | Per PS §10 | **PS §10 provisional** ("Duration"). Per-stage targets are **TBD: requires benchmark (B-5)** | B-5 on the PS §10 labeled benchmark set | Provisional (PS §10) + benchmark |
| SLO-6 | **Inconclusive rate** | The `inconclusive` share for classification (TI) and verification (VER) | Per PS §10 | **PS §10 provisional and guardrail**: never improved by weakening criteria | B-5 | Provisional (PS §10) |
| SLO-7 | **Durable job pickup** | Time from a job becoming due to being claimed ([ADR-006](../adr/ADR-006-durable-jobs-in-the-database.md)). Emitted by the job runtime (RO-4) | TBD: requires benchmark (B-3) | TBD: requires benchmark (B-3). Polling is the source of truth and notifications are only a hint (ADR-006 decision 9), so the polling interval bounds the floor | B-3 | Requires benchmark |
| SLO-8 | **Recovery from interruption** | The share of interrupted stage jobs (fault-injected) resumed without data loss, and duplicate uploads or PRs | Per PS §10 | **PS §10 provisional** ("Recovery") for the resumed share; **absolute** for duplicates (PS §10) | B-3 fault-injection suite (PS §10) | Provisional + absolute |
| SLO-9 | **Stuck-work detection** | Time from a job's lease expiring with no further progress until it is visible to operators (PRODUCT-SCOPE Workflow J). Also the age of the oldest undelivered outbox row | TBD: requires benchmark (B-3) | TBD: requires benchmark (B-3) | B-3 (kill workers, block the relay) | Requires benchmark |
| SLO-10 | **Artifact availability** | The share of artifact reads and writes that succeed through the artifact port (INT adapter typed errors) | TBD: requires benchmark (B-4) | TBD: requires benchmark (B-4). Depends on the operator's storage backend (ADR-009: S3-compatible or local filesystem) | B-4 | Requires benchmark |
| SLO-11 | **Artifact integrity** | Reads whose integrity hash doesn't match (the port checks on read, SYSTEM-ARCHITECTURE §4) | Continuous | **Absolute**: no artifact whose hash doesn't match is ever served or used as evidence. A mismatch is a detected failure, never a silent pass | Corruption injection in B-4; port conformance suite ([ADR-003](../adr/ADR-003-provider-adapters.md)) | Absolute |
| SLO-12 | **Retention execution** | How far deletion lags behind the TTL; deletions without an audit record | TBD: requires benchmark (B-4) for lag | Lag: TBD: requires benchmark (B-4). TTL values come from the PRODUCT-SCOPE Workflow J provisional values. **Absolute**: every deletion batch is audited, and audit records are never deleted by artifact retention | B-4; audit query matching deletion batches to audit records | Lag benchmark + absolute |
| SLO-13 | **Security invariants** (§1.2) | Violations of S-1 to S-6 | Continuous | **Absolute** (PS §10) | §1.2 validation methods; B-7 | Absolute |

### 1.2 Security invariants (absolute)

| ID | Invariant | Sources | How it is validated (no code here) |
|---|---|---|---|
| S-1 | **No untrusted repo or test code runs in the control plane**, including evaluation installs, where only the host is shared | ADR-004 decision 1; ADR-008 placement; ADR-009 (Playwright only in sandboxes) | (a) A boundary-check rule: process-spawning and code-loading facilities may be used only by the sandbox adapter's runtime client, never in contexts or other adapters (import-linter, [ADR-007](../adr/ADR-007-implementation-stack-and-licensing.md); the rule's exact shape is an implementation detail). (b) Sandbox-port conformance suite. (c) B-7 abuse repos whose tests try to run code on, or reach, the control plane; they must fail inside the sandbox |
| S-2 | **No unauthorized cross-tenant access** | SYSTEM-ARCHITECTURE §7.1–7.2; ADR-005 decision 5; ADR-008 item 3 | (a) Tests showing the data-access guard rejects unscoped queries. (b) Authorization tests for every command and query, including two orgs in one deployment (FQ-4). (c) A periodic audit query: any read or write where the actor's org isn't the target's org outside break-glass must return nothing |
| S-3 | **No secret exposure**: never to AI providers, events, audit, logs, or PR bodies, and **never any unlisted secret or control-plane credential to sandboxes** | PRODUCT-SCOPE §4; SYSTEM-ARCHITECTURE §7.4; ADR-004 decision 3; ADR-009 secret row | (a) Canary secret values in benchmark configs. Search AI request payloads in tests (the AI port fake), events, audit, logs, PR bodies, and exported artifacts; any hit is a violation. Artifact redaction is best-effort (R4), so an artifact hit is still reported. (b) Compare the secret names injected per run (`artifacts.exported` audit) with the repo config's list for that environment. (c) B-7 sandbox probes for control-plane credentials and metadata endpoints |
| S-4 | **No auto-merge and no push to an existing branch.** No repo write without an `active` approval matching the diff hash and base SHA | P3; PRODUCT-SCOPE Workflow H success criteria; SYSTEM-ARCHITECTURE §4 (no such port operation); ADR-009 | (a) Structural: the code-host port has no merge or push-to-existing operation; the conformance suite asserts this. (b) An audit query: every `application.succeeded` must have a matching `approval.consumed` and a branch under the prefix |
| S-5 | **Audit fails closed**: no business change commits without its envelope | SYSTEM-ARCHITECTURE §5; ADR-005 decision 3; ADR-006 decision 2 | (a) Fault-injection test: a failed outbox write must roll back the business change (§2, F-10b). (b) A reconciliation query: every state-change row has exactly one envelope |
| S-6 | **AI never decides classification, verification outcomes, approval eligibility, or investigation transitions** | P1; DEPENDENCY-RULES §5 | (a) import-linter matrix contracts (no path from TI, VER, or APPR to DIAG). (b) B-5 run with AI disabled and with the AI fake: TI and VER outcomes must be identical (P6) |

### 1.3 Benchmarks

Every benchmark runs on the reference environment (Conventions). **Pass criterion for setting a target:** the target is set from the measured baseline with written reasoning (PRODUCT-SCOPE §10 calibration, steps 2–3), and re-measured after changes to rules, criteria, or providers (step 4).

| ID | Workload | Measures |
|---|---|---|
| B-1 | Synthetic interface traffic over sync operations: reads, decisions, config edits, ingestion acceptance | SLO-1, SLO-2; database connection-pool use |
| B-2 | JUnit XML uploads across a range of report sizes and bursts, plus resubmissions, malformed, oversized, and hostile reports (entity expansion, deep nesting) | SLO-3, SLO-4; the report-size limit; parser resource use |
| B-3 | Many concurrent investigations, with fault injection: kill workers, expire leases, duplicate enqueues, block the outbox relay, restart PostgreSQL | SLO-7, SLO-8, SLO-9; outbox lag; lock waits |
| B-4 | Artifact writes and reads at representative sizes (logs, traces, video) on both storage adapters; storage outage and corruption injection; retention runs | SLO-10, SLO-11, SLO-12 |
| B-5 | The PRODUCT-SCOPE §10 labeled benchmark set, end to end, with AI disabled and with the AI fake | SLO-5, SLO-6; S-6 |
| B-6 | Sandbox capacity: concurrent sandboxes per execution host at the configured resource limits; per-test timeout rate against host load | Scaling hypotheses H-4 and H-7 (§3) |
| B-7 | Abuse suite: malicious repos and tests (escape attempts, egress probes, credential probes, prompt-injection content), two-org tenant probes, canary secrets | S-1 to S-5. Pass = zero violations (absolute) |

---

## 2. Failure-handling matrix

**Rules applied throughout** (from existing docs):
- **Typed errors** come from INT adapters: `transient`, `rate_limited`, `permission_denied`, `not_found`, `conflict`, `invalid_request`, `quota_exceeded`, `unavailable` (SYSTEM-ARCHITECTURE §4).
- **Retries** use bounded backoff and only for `transient`, `rate_limited`, and `unavailable` (§5). **Retry counts and backoff parameters are TBD: requires benchmark (B-3).**
- **After retries are exhausted:** external-provider errors move the investigation to `needs_input`, and control-plane errors move it to `failed` (PRODUCT-SCOPE Workflow I).
- **Infrastructure failures never become test failures.** Sandbox, database, and storage problems produce `execution_failure`, or no outcome plus `needs_input`. They never produce `verified_failure` or `deterministic_regression` (PRODUCT-SCOPE §5.2, §5.4).
- **AI failures only degrade diagnosis** (P1, P6).
- **ORCH is the only writer of investigation state.**
- **Class** is one of: **retryable** (the same request can succeed later), **terminal** (it won't succeed without a change in input or configuration), or **inconclusive** (the run completed but the evidence can't decide).
- **Paths** lists the context interactions involved as A→B: A calls B or consumes B's events, and each must be a DEPENDENCY-RULES §3.1 edge. IF is the interface layer. AUD(env) is the envelope exception.

### F-1 PostgreSQL

| ID | Failure | Detection | Durable state (owner) | Retry and idempotency | User-visible outcome | Audit | Recovery or operator action | Class | Paths |
|---|---|---|---|---|---|---|---|---|---|
| F-1a | Unavailable | Connection errors in every context; job pickup stops | All contexts: nothing commits. All state is in PostgreSQL ([ADR-005](../adr/ADR-005-system-of-record-database.md)), so no partial cross-context state is possible (one context per transaction) | Sync requests fail and the caller may retry; ingestion resubmits safely (idempotency key). Leased jobs re-run after their leases expire (ADR-006 decision 1). A sandbox run whose record couldn't be stored is re-run by run ID, and any orphan is torn down first (SYSTEM-ARCHITECTURE §8 D) | Requests fail with a server error. Investigations pause in their current state | No business change commits, so nothing goes unaudited | Restore the database; workers resume by polling | retryable | — |
| F-1b | Degraded: slow, or connection pool exhausted | Latency and pool-saturation telemetry; growing SLO-7 and SLO-9 values | All contexts: unchanged | Bounded backoff. If a lease can't be renewed, it expires and the job re-runs. Steps are idempotent, and transitions use compare-and-set, so a late duplicate writer has no effect (§5) | Slower responses and stage progress | Unchanged | Reduce worker concurrency or add database resources (§3) | retryable | — |
| F-1c | Recovering after a restore to an earlier point | Operator procedure | All contexts: state and envelopes roll back **together** (same database), so audit stays consistent with state. External effects after the restore point may exist | Application uses find-or-create by approval ID, which re-finds an existing branch or PR (SYSTEM-ARCHITECTURE §8 H). Orphaned sandboxes self-expire (sandbox-port guarantee) | Work after the restore point is redone or must be re-requested | The restore itself is an operator action recorded outside the restored data (RO-1) | Follow the backup and restore procedure (RO-1) | retryable | — |

### F-2 Workers and jobs

| ID | Failure | Detection | Durable state (owner) | Retry and idempotency | User-visible outcome | Audit | Recovery or operator action | Class | Paths |
|---|---|---|---|---|---|---|---|---|---|
| F-2a | Worker crash mid-step | Lease expiry | Job rows owned by the job's context (ADR-006 decision 8). Stage jobs belong to ORCH | Re-run idempotently up to a bounded count (count TBD: requires benchmark, B-3). Keys: investigation + stage + attempt, run ID, approval ID (§5) | The investigation stays `running` | `investigation.state_changed` only if the state changes | None, unless retries are exhausted (F-2d) | retryable | ORCH→EXEC, ORCH→TI, ORCH→VER |
| F-2b | Lease expires while the original worker is still alive | The renewal fails; a second worker claims the job | The job's owning context (ORCH for stage jobs) | The first worker's late writes fail compare-and-set or idempotency checks and have no effect | None | None | None | retryable | — |
| F-2c | Duplicate job (enqueued or claimed twice) | Idempotency-key conflict | The key owner: ORCH (stage job), EXEC (run), APPR (approval), TI (upload) | The duplicate is a no-op (§5) | None | None | None | retryable | — |
| F-2d | Retries exhausted | Attempt count reaches the bound | ORCH | Control-plane cause: `running` → `failed`. External cause: → `needs_input` (Workflow I). TI normalization: `processing_failed` (Workflow B). Application: `application.failed` → `needs_input` (Workflow H) | `failed` (user may retry, which resumes after the last completed stage) or `needs_input` with a reason | `investigation.state_changed`, `upload.processing_failed`, `application.failed` | User retry; operator inspects stuck work (Workflow J) | terminal (`failed`) / retryable (`needs_input`) | ORCH→APPR, ORCH→TI |

### F-3 Outbox and event dispatch

| ID | Failure | Detection | Durable state (owner) | Retry and idempotency | User-visible outcome | Audit | Recovery or operator action | Class | Paths |
|---|---|---|---|---|---|---|---|---|---|
| F-3a | Backlog (relay lag) | Age of the oldest undelivered row (SLO-9) | Each publishing context's outbox rows are committed and durable (shared-kernel infrastructure, ADR-005 decision 3) | Delivered in order once capacity returns. **Staleness stays safe under lag**: APPR rechecks every trigger before applying (Workflow H step 1), so a delayed event can't cause a stale application | Delayed reactions: transitions, invalidations, audit visibility | Audit appends are delayed, never lost | Add worker capacity (§3). Delivered rows may be pruned only after every subscriber, including AUD, has consumed them (mechanism: implementation detail) | retryable | ORCH→VER, ORCH→APPR, APPR→PATCH, APPR→VER, APPR→REPO, APPR→IAM, AUD(env) |
| F-3b | Duplicate delivery | The subscriber has already processed that event ID | The subscriber context's dedupe records (for example APPR, ORCH, or AUD) | Dedupe by event ID (SYSTEM-ARCHITECTURE §5; ADR-006 decision 3) | None | None | None | retryable | AUD(env) |
| F-3c | Dispatch failure (subscriber keeps erroring) | Per-subscriber error count | Subscriber context (for example APPR, ORCH, or AUD): the outbox row stays undelivered for it | Bounded retry. **What happens to an event that keeps failing is open (RO-2)**: parking, operator visibility, effect on ordering. Constraint: **an event is never dropped for AUD**, because audit integrity depends on it (S-5) | Delayed reactions for that subscriber | Not lost | Operator visibility (Workflow J); RO-2 | retryable | AUD(env) |

### F-4 Sandbox and execution

| ID | Failure | Detection | Durable state (owner) | Retry and idempotency | User-visible outcome | Audit | Recovery or operator action | Class | Paths |
|---|---|---|---|---|---|---|---|---|---|
| F-4a | Provisioning failure | The sandbox adapter returns a typed error (`unavailable`, `transient`, `quota_exceeded`) | EXEC records the environment error in the run record. TI (reproduction) or VER (verification) sets the outcome | EXEC retries transient errors, bounded (§8 D). Run ID is the key | Reproduction or verification: `execution_failure`, never `verified_failure`. The run is excluded from classification evidence (PRODUCT-SCOPE D5). Investigation → `needs_input` | `run.errored`, `investigation.state_changed` | Check the execution host's capacity and health. The user retries | retryable | EXEC→INT, TI→EXEC, VER→EXEC, ORCH→TI, ORCH→VER |
| F-4b | Per-test timeout | The run reports the test-level timeout | EXEC run record (a test fact). TI or VER decides the outcome | None: this is a result, not an error | Patched run only: contributes to `verified_failure`. Also at baseline: `confounded` → `inconclusive` (Workflow G). **Risk**: an overloaded host could cause timeouts in patched runs only (H-7) | `run.finished`, `verification.completed` | B-6 bounds host load. Resource limits per sandbox (ADR-008) | inconclusive / terminal per Workflow G | VER→EXEC |
| F-4c | Environment-level or whole-stage timeout; sandbox crash | Typed error or the run reports an environment error | EXEC run record; TI or VER outcome | Bounded retry within the phase (§8 G) | `execution_failure`. Investigation → `needs_input` | `run.errored` | User retries | retryable | VER→EXEC, TI→EXEC |
| F-4d | Execution error: baseline doesn't build, or dependency or setup failure | Environment signals in the run | EXEC facts; VER or TI outcome | Persistent errors aren't retried | `execution_failure` (Workflow G; PRODUCT-SCOPE §5.4). If the **patched** build fails while the baseline built: `verified_failure` (the patch's fault, Workflow G step 2) | `run.finished`, `verification.completed` | Fix the environment config (`repo.configure`) | terminal (until the config changes) | VER→EXEC |
| F-4e | Teardown failure | Teardown error; the sandbox outlives its TTL | EXEC lifecycle | Teardown is idempotent and retried. Self-expiry is the backstop (sandbox-port guarantee). Orphans are cleaned before a re-run (§8 D) | None. The outcome is already recorded | `run.*`, plus an operator alert (mechanism: implementation detail) | The operator checks the execution host. **Risk**: a leftover sandbox may still hold that run's injected test secrets until it expires | retryable | EXEC→INT |

### F-5 JUnit XML input

| ID | Failure | Detection | Durable state (owner) | Retry and idempotency | User-visible outcome | Audit | Recovery or operator action | Class | Paths |
|---|---|---|---|---|---|---|---|---|---|
| F-5a | Malformed report or missing metadata | Schema validation (Workflow B) | TI upload `rejected` | None. Resubmitting after a fix is safe | Synchronous rejection with field-level reasons | `upload.rejected` | Fix the CI reporter | terminal | IF→TI |
| F-5b | Oversized report or rate limit | Size and rate limits (IAM quota) | TI; IAM quota | None. Never partially ingested | Rejection with an explicit quota reason | `upload.rejected`, `quota.denied` | Raise the quota or split the report | terminal | IF→TI, TI→IAM |
| F-5c | Hostile XML: entity expansion, external entities, deep nesting, huge element counts | The hardened parser refuses it (no entity or DTD resolution, size/depth/count limits; ADR-009; SYSTEM-ARCHITECTURE §3.2 rule 4) | TI upload `rejected`. Nothing runs, and the content stays data | None | Rejection with a reason | `upload.rejected`; repeated attempts from one token are visible to org admins (Workflow B) | Org admin may revoke the token | terminal | IF→TI, TI→INT |
| F-5d | Normalization fails after acceptance | Error in the worker | TI `processing_failed` | Retried automatically. Resubmitting is safe | The report isn't queryable yet | `upload.processing_failed` | None, unless it persists (F-2d) | retryable | TI→INT |

### F-6 Artifact storage

| ID | Failure | Detection | Durable state (owner) | Retry and idempotency | User-visible outcome | Audit | Recovery or operator action | Class | Paths |
|---|---|---|---|---|---|---|---|---|---|
| F-6a | Outage while writing a raw report | Artifact port `unavailable` | TI: no upload is accepted. Acceptance includes storing the raw report (§8 B step 1) | Client resubmits (idempotent) | Synchronous retryable error | `upload.rejected` (reason: unavailable) | Restore storage | retryable | IF→TI, TI→INT |
| F-6b | Outage while exporting run outputs | Artifact port `unavailable` during export | EXEC run record (environment error). No outcome can be computed without raw outputs, because results are claims to recompute (ADR-004 decision 5) | Bounded retry, then the run ends with an environment error | `execution_failure`. Investigation → `needs_input` | `run.errored` | Restore storage; the user retries | retryable | EXEC→INT, TI→EXEC, VER→EXEC |
| F-6c | Outage while reading (evidence view, DIAG excerpts, TI/VER recomputation) | Artifact port `unavailable` | TI, VER, DIAG: unchanged. No outcome is recorded | Bounded retry, then an external-provider error → `needs_input` (Workflow I) | Evidence is temporarily unavailable; the stage waits | `investigation.state_changed` | Restore storage | retryable | TI→INT, DIAG→EXEC, VER→EXEC |
| F-6d | Artifact missing because retention deleted it | Expected: the item is marked removed in evidence | TI marks the evidence item removed (mechanism: BC-Q5). Recorded outcomes are immutable and stay | None | The item is shown as removed. Recomputation falls back to retained normalized results; raw re-parsing is no longer possible | TI and EXEC deletion events (TI event name: BC-Q4) | None | terminal | TI→EXEC |
| F-6e | Artifact unexpectedly missing, or **integrity mismatch** | `not_found`, or a hash mismatch on read (port guarantee) | TI or EXEC (item owner): the item is **never used as evidence or served** (SLO-11). How the item is durably flagged is open (RO-3, alongside BC-Q5) | None for that object | The item is shown as unavailable. A stage that needs it → `needs_input`; the user may re-run reproduction or verification | Operator alert; record per RO-3 | Investigate storage integrity | terminal (object) / retryable (re-run) | TI→INT, EXEC→INT |

### F-7 AI provider and patch candidates

| ID | Failure | Detection | Durable state (owner) | Retry and idempotency | User-visible outcome | Audit | Recovery or operator action | Class | Paths |
|---|---|---|---|---|---|---|---|---|---|
| F-7a | AI unavailable: none configured (the v1 default), policy `disabled`, or provider unhealthy | Policy check; `integration.health_changed` | DIAG diagnosis outcome `unavailable` | None | Diagnosis unavailable. **Classification, reproduction, verification, and approval are unaffected** (P6, S-6). Investigation → `needs_input` | `diagnosis.recorded` | Configure a provider (later, ADR-009) | terminal (until configured) | ORCH→DIAG, DIAG→INT |
| F-7b | Timeout or rate limit | Typed error `transient`, `rate_limited`, or `unavailable` | DIAG | Bounded retry (Workflow E). Cost counted per actual call | Job `errored`, investigation → `needs_input` | `ai_request.sent`, `investigation.state_changed` | The user retries | retryable | DIAG→INT, DIAG→IAM |
| F-7c | Malformed output | Schema validation | DIAG outcome `rejected`. The validation failure and its reason are always recorded. Raw output is kept only on org opt-in (PRODUCT-SCOPE D6, Workflow E) | Bounded retry, then `rejected` | No hypothesis stored | `diagnosis.recorded` | The user retries | retryable → terminal | DIAG→INT |
| F-7d | Invalid AI patch candidate (doesn't apply, out of scope, masking, secret detected) | PATCH validation | PATCH `invalid` with reasons; never verified (Workflow F) | None for that candidate. A new candidate means a new request | Reasons shown | `patch.validated` | The user requests a new candidate or writes one | terminal | ORCH→DIAG, ORCH→PATCH, PATCH→REPO |
| F-7e | AI spend quota exceeded | IAM quota check | DIAG; ORCH | None | `needs_input` / `quota_exceeded` | `quota.denied` | An admin raises the quota | terminal (until quota changes) | DIAG→IAM |
| F-7f | Instructions injected into the output | None needed | DIAG | — | No effect. Output is data (PRODUCT-SCOPE §4) | — | — | terminal (ignored) | — |

### F-8 Code host

| ID | Failure | Detection | Durable state (owner) | Retry and idempotency | User-visible outcome | Audit | Recovery or operator action | Class | Paths |
|---|---|---|---|---|---|---|---|---|---|
| F-8a | Auth failure: installation token can't be minted, or app credentials are invalid | `permission_denied` or `unavailable` from the code-host adapter | REPO re-validates → `degraded` | `transient` and `unavailable` are retried; `permission_denied` is not (§5) | New investigations and application are blocked; ingestion continues while `degraded` (Workflow B). Active approvals are invalidated (Workflow H, repo `degraded`) | `repo_connection.state_changed`, `approval.invalidated` | The operator fixes the app credentials (deployment config), then REPO re-validates → `active` | terminal (until fixed) | REPO→INT, APPR→REPO |
| F-8b | Sign-in fails (same GitHub App, FQ-11) | Identity port error | IAM. No new sessions | `transient` is retried | Users can't sign in. Existing sessions are unaffected until they're revoked | — | Same credential fix as F-8a | retryable / terminal | IAM→INT |
| F-8c | Permission revoked or app uninstalled | Webhook, or `permission_denied` | REPO `degraded` / `disconnected`. APPR invalidates active approvals. A user losing code-host write invalidates their approval | None | Application blocked; `needs_input` | `repo_connection.state_changed`, `approval.invalidated` | Reinstall or restore permissions | terminal | REPO→INT, APPR→REPO, APPR→IAM |
| F-8d | PR creation fails: branch protection, partial progress, transient error, base SHA missing | Typed error during application | APPR application attempt; approval state | Partial or transient: idempotent find-or-create resume, bounded. Branch protection: `application.failed` and the approval stays `active`. Base SHA missing: approval `invalidated` (Workflow H table) | `applying` → `needs_input` | `application.attempted`, `application.failed`, `approval.invalidated` | Adjust branch rules, then retry application | retryable / terminal per Workflow H | ORCH→APPR, APPR→REPO, REPO→INT |

### F-9 Cancellation and partial completion

| ID | Failure | Detection | Durable state (owner) | Retry and idempotency | User-visible outcome | Audit | Recovery or operator action | Class | Paths |
|---|---|---|---|---|---|---|---|---|---|
| F-9a | Cancel in `queued`, `running`, `needs_input`, or `awaiting_approval` | User command | ORCH → `cancelled`. EXEC tears down. VER leaves the verification without an outcome (`verification.cancelled`). APPR invalidates any active approval | Teardown and invalidation are idempotent (SYSTEM-ARCHITECTURE §8 I) | `cancelled`; partial artifacts labeled partial | `investigation.state_changed`, `run.cancelled`, `verification.cancelled`, `approval.invalidated` | — | terminal | ORCH→EXEC, ORCH→VER, ORCH→APPR |
| F-9b | Cancel during `applying` | The command arrives in `applying` | ORCH | Refused (Workflow H) | The cancel is rejected; application finishes or fails | — | Cancel after it leaves `applying` | terminal (request refused) | — |
| F-9c | Cancel races a stage finishing | Compare-and-set on the transition | ORCH | Whichever transition commits first wins. A later `verification.completed` for a cancelled investigation is a no-op (duplicate and late events have no effect, §8 I) | Consistent with the committed state | `investigation.state_changed` | — | terminal | ORCH→VER |
| F-9d | Partial completion after a crash (for example, baseline done, patched phase interrupted) | Lease expiry | VER phase records | Completed phases aren't re-run. An incomplete phase restarts in a fresh sandbox, and its partial runs are discarded and labeled (§8 G) | Verification continues | — | — | retryable | VER→EXEC |
| F-9e | Diagnosis cancelled mid-call | Cancellation signal | DIAG | The call is abandoned and its cost recorded; no hypothesis is stored (§8 E) | `cancelled` | `investigation.state_changed` | — | terminal | ORCH→DIAG |

### F-10 Retention and audit

| ID | Failure | Detection | Durable state (owner) | Retry and idempotency | User-visible outcome | Audit | Recovery or operator action | Class | Paths |
|---|---|---|---|---|---|---|---|---|---|
| F-10a | Retention deletion fails | Deletion-job error | TI (raw reports), EXEC (run artifacts), keyed by owner domain and artifact reference (§8 J) | Retried; repeated failure alerts the operator (Workflow J) | None | `artifact.deleted` (EXEC); TI's event name is pending (BC-Q4) | Fix storage | retryable | TI→INT, EXEC→INT, TI→IAM, EXEC→IAM |
| F-10b | **The audit (outbox) write fails** | The transaction errors | The originating context: **the whole business change rolls back**, because the envelope is in the same transaction (SYSTEM-ARCHITECTURE §5; ADR-005 decision 3; ADR-006 decision 2) | Sync command: error to the caller, who may retry. Async step: the step fails and the job re-runs idempotently | The action didn't happen; no partial state | Fails closed (S-5) | Restore database health (F-1) | retryable | — |
| F-10c | AUD's append from the outbox into the audit log fails | AUD append errors; growing outbox age | AUD: the outbox row stays undelivered (F-3c) | At least once, deduped by event ID. Never dropped | Audit visibility is delayed | Not lost | Same as F-3a and F-3c | retryable | AUD(env) |
| F-10d | Audit export fails | Export error | AUD | Retried; no partial export is visible (§8 J) | The export is reported as failed | `audit.exported` only on success | The user retries | retryable | IF→AUD, AUD→IAM |

---

## 3. Initial scaling assumptions

**Deployment model** (binding, [ADR-008](../adr/ADR-008-deployment-and-trust-model.md)):
- Self-hosted. Control-plane units are request-serving and worker roles of one build ([ADR-001](../adr/ADR-001-modular-monolith.md)).
- PostgreSQL ([ADR-005](../adr/ADR-005-system-of-record-database.md)).
- Artifact storage: S3-compatible or local filesystem ([ADR-009](../adr/ADR-009-initial-provider-adapters.md)).
- A **dedicated execution host** in production. Co-location only in explicit evaluation installs.
- Jobs and outbox in PostgreSQL: `SKIP LOCKED` claiming, polling as the source of truth, notifications only as a hint ([ADR-006](../adr/ADR-006-durable-jobs-in-the-database.md)).

**Scaling levers** within those decisions: the number of request-serving processes; the number of worker processes and their concurrency; the database connection pool; the execution host's capacity (concurrent sandboxes), capped by IAM's per-org concurrent-sandbox quota (PRODUCT-SCOPE Workflow J); the storage backend's capacity.

**Bottleneck hypotheses** (unverified, each tied to a benchmark):

| ID | Hypothesis | Benchmark | Signal |
|---|---|---|---|
| H-1 | Database connections run out first as request-serving and worker processes grow | B-1, B-3 | Pool saturation, wait time |
| H-2 | Lock contention on job claiming stays low with `SKIP LOCKED`, but rises with very short polling intervals | B-3 | Lock waits, claim latency |
| H-3 | Outbox and audit tables grow until relay throughput or pruning becomes the limit (audit retention per the PRODUCT-SCOPE Workflow J provisional value) | B-3, B-4 | Outbox age, table size |
| H-4 | Execution host CPU and memory limit throughput, because verification multiplies runs (N_p repeats per PRODUCT-SCOPE Workflow G) | B-6 | Sandbox queue wait, host use |
| H-5 | Artifact volume, mostly video and traces, dominates storage size and throughput | B-4 | Bytes per run, write latency |
| H-6 | Ingestion is bursty after CI runs, which stresses parsing and normalization | B-2 | Processing backlog |
| H-7 | Host overload can cause timeouts in patched runs only, a false-`verified_failure` risk | B-6 | Per-test timeout rate against host load |

**Metrics that justify more capacity** (thresholds all **TBD: requires benchmark**):
- Job pickup lag (SLO-7) or outbox age (SLO-9) rising: add worker processes or concurrency, if database headroom exists (H-1).
- Pool saturation or lock waits rising: database resources first.
- Sandbox queue wait or timeout rate rising: execution host capacity (H-4, H-7).
- Stage durations outside the PRODUCT-SCOPE §10 provisional targets.

**Conditions that need a new ADR** (existing triggers, referenced, not changed):

| Change | Trigger already written | Where |
|---|---|---|
| Message broker | A measured need; requires a new ADR | ADR-006 decision 3 |
| External workflow engine | Measured evidence that jobs can't meet the §6 requirements or the PRODUCT-SCOPE §10 targets once calibrated; requires a new ADR | ADR-006 decision 5 |
| Separate cache or coordination service | ADR-006 decision 4 says no documented requirement calls for one, so adding one would supersede that decision | ADR-006 decision 4 |
| Extracting a service | Measured scaling, isolation, or team need; must keep the dependency graph | ADR-001 decision 5 |
| Hosted multi-tenant service | A superseding ADR before launch, with microVM-class isolation, per-tenant execution segmentation, physical data isolation, and the operator treated as a separate party | ADR-008 item 6 |
| Physical tenant isolation | Still deferred | SYSTEM-ARCHITECTURE §10 |

**Volume assumptions to benchmark** (unknown; none is assumed as fact): orgs and repos per deployment; report uploads per period and report sizes; concurrent investigations; repeats per verification; artifact bytes per run; concurrent interactive users; retention-period data volume.

---

## 4. Consistency notes

- **Ownership.** Each row's durable-state owner matches [BOUNDED-CONTEXTS](BOUNDED-CONTEXTS.md) §4.3:
  - EXEC owns run records and facts.
  - TI owns uploads, reproduction outcomes, and classification.
  - VER owns verification outcomes.
  - ORCH alone writes investigation state.
  - APPR owns approvals and staleness.
  - PATCH validates candidates.
  - INT adapters map provider errors to typed errors.
  - AUD owns audit and telemetry.
- **Semantics.** Retry, idempotency, cancellation, and recovery follow SYSTEM-ARCHITECTURE §5, §6, and §8 and PRODUCT-SCOPE Workflow I. They aren't redefined here.

---

## 5. Open items

**Resolved product-scope clarifications** (owner-confirmed 2026-10-20):
- **P-1, resolved as PRODUCT-SCOPE D5.** FlakePilot's own sandbox errors during reproduction aren't infrastructure signals. Runs that end in `execution_failure` are excluded from classification evidence ([PRODUCT-SCOPE §5.1](../product/PRODUCT-SCOPE.md#51-failure-classification-workflow-c) and [§11 confirmed decisions](../product/PRODUCT-SCOPE.md#confirmed-decisions)). Applied in F-4a.
- **P-2, resolved as PRODUCT-SCOPE D6** (replaces Q10). Raw AI output, including malformed output, is stored only if the org opts in, and this is off by default. The validation failure and its reason are always recorded ([PRODUCT-SCOPE Workflow E](../product/PRODUCT-SCOPE.md#workflow-e--ai-assisted-diagnosis)). Applied in F-7c.

**Tracked items for later design** (implementation or design areas, not blocking):

| ID | Item | Area |
|---|---|---|
| RO-1 | Supported backup and restore procedure and objectives, and how restores are recorded | Deployment documentation (later stage) |
| RO-2 | What happens to an event a subscriber keeps failing on: parking, operator visibility, ordering. Constraint: never dropped for AUD | Implementation design |
| RO-3 | Durable flag for an artifact that is missing or fails its hash check | Data model, with BC-Q5 |
| RO-4 | How interface-layer and job-runtime telemetry reach AUD's exporter. The permitted route is interface or application code to AUD's public API (DEPENDENCY-RULES §1); AUD's API currently lists queries and export only | Interface/API design, with BC-Q1 |
| RO-5 | REPO validation when transient retries run out (Workflow A defines only the permanent-failure case) | Implementation design |
| — | Every **TBD: requires benchmark** target and parameter in §1 to §3 (retry counts, backoff, polling, lease length, thresholds) | Benchmarks B-1 to B-7 |

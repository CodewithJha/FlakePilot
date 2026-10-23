# Infrastructure decisions — owner decision register

Stage 10K. Decision preparation only. Nothing here provisions, subscribes, or
enables anything, and every decision in this register remains the repository
owner's until explicitly made and recorded. The live product posture is the
one assessed in [PRODUCTION-READINESS-ASSESSMENT](PRODUCTION-READINESS-ASSESSMENT.md):
untrusted execution disabled, `ClosedSandbox` and the fail-closed guards in
place, plaintext artifacts at rest (the one caveat this register is designed
to close — see C-decisions).

Vocabulary per the recommendation policy: **Verified** (checked against
current source or citable source in
[INFRASTRUCTURE-OPTIONS-ANALYSIS](INFRASTRUCTURE-OPTIONS-ANALYSIS.md)),
**Research** (external source, date checked), **Recommendation** (engineering
judgment, needs owner approval), **Assumption** (stated guess that a decision
must confirm or reject), **OWNER DECISION** (cannot be made by this
repository).

Recommendations are made in the "Recommendation" field of each decision and
consolidated at the end; no owner decision is made silently.

---

## A. Execution host

### D-HOST-1 · Hosting model and provider

- Question: on what kind of infrastructure does FlakePilot's execution host
  run — a cloud VM with nested virtualization, a bare-metal cloud instance,
  or a self-managed dedicated server?
- Why it matters: a per-run microVM requires Linux KVM (`/dev/kvm`). Nested
  KVM on a cloud VM works (Azure, DigitalOcean, Google Cloud are named by an
  independent write-up; AWS VMs generally do not expose it — Research,
  checked 2026-10-23) but adds a hypervisor-on-hypervisor kernel/
  compatibility risk that later becomes evidence-taint (identity fingerprint
  includes host kernel).
- Options and trade-offs:
  - Cloud VM with nested KVM: fastest to start, on-demand, no bare-metal
    premium; disadvantages: second virtualization layer, kernel/upgrades
    controlled by the provider, data residency inherited from the platform.
  - Bare-metal cloud instance (example class: single-host dedicated servers,
    price ranges from low-double-digit USD/month in the secondary-market
    range to multi-thousand USD/month on the major clouds — Research
    anecdote, not verified pricing): strongest deliverability of the trust
    boundary, no nested-virt risk; disadvantages: fixed capacity, slower
    to replace, provider-dependent patching model.
  - Self-managed dedicated server (e.g., a machine the owner already owns
    or funds): maximal control and lowest recurring spend; disadvantages:
    the owner carries hardware, power, network, patching and retirement
    duties; availability depends on a home/office network unless collocated.
- Security/operational consequences: the host holds the ledger key and the
  runtime socket; model determines who else can see that machine though VPN
  or panel access. On cloud panels, consider who can snapshot the host.
- Cost category (informational, not verified): cloud VM — monthly usage
  item; bare-metal — monthly recurring; self-managed — capital + utilities.
  Exact figures deliberately not asserted.
- Dependencies: D-HOST-2 (tenancy), D-HOST-3 (region), D-HOST-4 (sizing).
- Missing information: whether any compliance or data-residency constraint
  applies (assumed none until stated), whether the owner already has a
  preferred provider or hardware.
- Recommendation: **bare-metal or dedicated-tenancy KVM host (one host,
  initially single-node, scale-up not scale-out)**. Rationale: the
  roadmap B1–B2 chain needs a stable, fingerprintable host whose kernel is
  the full issuer of attestations, and nested-virt evidence would be
  harder to review; one host is the cheapest correct boundary. Assumption:
  no compliance constraint blocks any cloud provider, budget allows a
  small fixed monthly line item.
- OWNER DECISION: model and provider; cost owner and ceiling (D-HOST-5).
- Work blocked until made: WP-2 provisioning; everything in roadmap B1–B4,
  B8 external phases.

### D-HOST-2 · Dedicated tenancy and administrative ownership

- Question: is the host single-tenant and administratively owned by one
  named person, per [EXECUTION-HOST](EXECUTION-HOST.md) §5?
- Why it matters: the standing contract requires that the execution
  platform never be shared with anything else untrusted; a multi-tenant
  host cannot carry the trust boundary the evidence assumes.
- Options: (a) dedicated whole host single-operator (recommended), (b) shared
  VM / shared host not permitted by contract; (c) later multi-host with
  per-host identities – deferred design.
- Consequences: (a) means one operator identity owns SSH; no control-plane
  daemons run there; the platform never invents credentials.
- Dependencies: D-HOST-1.
- Recommendation: (a), with admin access per runbook §5 (key SSH, recorded
  sudo policy, never used by the control plane).
- OWNER DECISION: named operator; whether that is the owner themselves.
- Blocks: the same set as D-HOST-1.

### D-HOST-3 · Region and data residency

- Question: any region, data-residency, or jurisdictional constraint for
  where test artifacts and evidence are stored and processed?
- Why it matters: artifacts contain org code, pull-request paths, and
  internal names; evidence records are future audit material; "unknown" is
  itself a liability here (assessment area 4/9).
- Options: (a) no constraint (single region chosen by D-HOST-1 choice),
  (b) EU/other-residency placement contractually required by a customer or
  regulation, (c) not applicable to satisfy any known vendor marketing
  requirements.
- Dependencies: D-HOST-1 and KMS region behavior (D-KMS-1: keys non-exportable
  by region; Research — GCP KMS stores key material in the region chosen,
  AWS KMS keeps a key in its region, multi-region keys are explicit).
- Recommendation: state honestly — the repo cannot guess whether a
  residency constraint exists. Default engineering proposal if no
  constraint exists: single region, keep control-plane and execution host
  in the same region to avoid cross-region key/data split.
- OWNER DECISION confirm-or-reject the "no constraint" assumption.

### D-HOST-4 · Resource sizing and scaling assumptions

- Question: how big does the host need to be and how does it scale?
- Why it matters: every per-run microVM consumes host vCPU, memory, and
  disk; underestimating forces horizontal scale-out before the single-host
  baseline is proven.
- Options: (a) one host sized for the initial expected concurrency
  (assumption: tens of runs/day, a handful concurrent — needs the owner's
  usage portrait), (b) more hosts behind the existing ledger/worker,
  (c) autoscaling groups (rejected initially: multi-host evidence review
  before any autonomy).
- Constraints verified in code: per-run limits are bounded by
  `WALL_MAX = 120 s`, `MEMORY_MAX = 2048 MiB`, `PIDS_MAX = 256`
  (`execution/domain/plane.py`) — Verified. So a single 8-vCPU / 16–32 GiB /
  100+ GiB SSD class host supports these on the order of a few concurrent
  runs at worst-case settings.
- Recommendation: (a) small single host, scale up first (more vCPU/RAM per
  host), then scale out; assumption on usage must be confirmed.
- OWNER DECISION: budget ceiling and a written usage portrait.

### D-HOST-5 · Budget ceiling and cost owner

- Question: what monthly spend is authorized and whose money is it?
- Why it matters: ADR-004/008 trust model assumed a fixed agent mainframe
  "budgeted and accountable"; spend approval must record who authorizes.
- Cost categories observed (Research, checked 2026-10-23, figures not
  verified): bare-metal single-host line item, low-double-digit USD/month on
  budget providers to multi-thousand USD/month on premium clouds; cloud KMS
  or managed monitoring are usage-metered add-ons; no cost approved here.
- Recommendation: a single small dedicated host (D-HOST-1) with a defined
  monthly ceiling, reviewed after the first evidence cycle.
- OWNER DECISION: ceiling, owner of the spending, and how it is recorded.

### D-HOST-6 · Network boundaries and administrative access

- Question: who can reach the host, on what ports, and how is the operator
  authenticated?
- Why it matters: the execution host's attack surface is where the trust
  boundary meets the world; default-deny is the standing contract.
- Options: SSH key-only, single operator (recommended); SSH password disabled,
  no panel-password reuse; monitoring/scrape exposed only to a dedicated
  interface; no control-plane DB reach.
- Verified: runbook §5 proposes exactly this; nothing has been deployed.
- Recommendation: adopt runbook §5 and add one explicit "no shared panel
  credentials" item. OWNER DECISION: whether an out-of-band management
  (provider rescue mode/reset) access counts as a breaker and who holds it.

### D-HOST-7 · Patch management, recovery, host retirement

- Question: how does the host patch, recover, and retire?
- Why it matters: runbook §12/§13 invalidates evidence on kernel, runtime,
  or digest change; patching cadence therefore drives evidence lifetime and
  re-verification cost.
- Options: fixed cadence (recommended: monthly patch window; automatic
  security updates for the OS, manual decision for kernel major versions);
  configuration management (the runbook anticipates a rebuildable-from-
  definition host — implement as a small recorded config, not as new
  product features).
- Recommendation: monthly window, record every patch in the validation log;
  a host retirement runbook rehearsed once (WP-8) before production.
- OWNER DECISION: cadence acceptance and retirement funding.

## B. Isolation runtime

### D-RUNTIME-1 · Candidate runtime (primary)

- Question: which VMM/isolation runtime does the host run?
- Verified (repository): the evidence ladder requires a separate kernel per
  run (`per_run_microvm` in `execution/domain/capability.py`; the collector
  refuses to represent a shared-kernel container at that level).
- Options analyzed in [INFRASTRUCTURE-OPTIONS-ANALYSIS](INFRASTRUCTURE-OPTIONS-ANALYSIS.md)
  §2. Recommended: **Firecracker** for the initial implementation stage.
  Rationale (Research, checked 2026-10-23): smallest device model (~5
  virtio devices), boots in ~125 ms with roughly 5 MiB overhead, built-in
  per-VM rate limiters that map onto the six resource limits, a real
  production record for untrusted code (AWS Lambda/Fargate, Fly.io, Vercel
  Sandbox, E2B), and *actively patched in 2026* — a current release
  hardened the virtio device initialization (CVE-2026-5747). Its listed
  limitations are mostly advantages here: no shared filesystem (nothing
  should share with the host), no device hotplug (one-shot runs), no GPU
  passthrough (not needed).
- Alternatives retained: Cloud Hypervisor (broader device surface, VFIO,
  hotplug, live migration — features FlakePilot does not need; kept as a
  documented fallback if Firecracker proves incompatible with the chosen
  host kernel), QEMU `microvm` machine type (most mature, widest surface —
  a larger attack surface to argue to a reviewer), Kata Containers and
  gVisor (rejected for the baseline: Kata adds a Kubernetes-class
  orchestration layer the ledger/worker design does not require; gVisor is
  a userspace kernel that does not satisfy the measured per-run
  `per_run_microvm` target without a specific review approval).
- OWNER DECISION: candidate runtime and acceptance of the associated
  operational maturity argument.
- Blocked until made: WP-3 runtime integration, WP-4 measurement.

### D-RUNTIME-2 · Threat model and isolation guarantees

- Verified (repository): the six control groups the report must evidence
  (network/metadata/egress, mounts/credentials, unprivileged, limits,
  cleanup) are fixed in `execution/domain/capability.py` and mirrored by
  `ops/isolation/evidence_report.py`.
- Options: (a) adopt the runbook's threat model as the baseline: each run in
  its own kernel, limits enforced outside the guest, no runtime socket in
  the guest, secrets never in guest env, and treat "kernel escape" as out
  of scope without public review; (b) adopt a stronger public target
  (supply-chain hardened, e.g., requiring separately signed kernel and a
  signed rootfs) — stricter but more work.
- Recommendation: (a) for the first cycle; move to (b) incrementally via
  image-provenance controls the runbook §11 already requires (digest-pinned
  image, immutable digest attached to the runtime). OWNER DECISION if a
  stricter baseline is wanted up front.

### D-RUNTIME-3 · Guest lifecycle, image provenance, immutable digests

- Verified (repository): `run.sh` and the harness already pin and compare
  image digests; the HostGate pattern requires the digest used at runtime
  to equal the pinned one; `image_provenance=digest_pinned` is a control
  in `evidence_report.py`.
- Recommendation: make digest pinning mandatory for every run (a muxer must
  pass digest; mutable tags refused); guest lifecycle = one-shot create /
  run / destroy, agents reached over vsock rather than TCP; snapshots NOT
  used to skip boot for untrusted inputs (restore-from-snapshot is
  stateful; mixing snapshots with untrusted artifacts must be deferred to
  reviewer judgement).
- OWNER DECISION: not required unless rejecting digest pinning as
  mandatory.

### D-RUNTIME-4 · Network restrictions, metadata protection, filesystem isolation

- Verified (repository): `evidence_report.py` requires both metadata
  endpoints and both public-egress endpoints observed `blocked` from
  inside the guest; `MECHANISM_LEVEL` never auto-passes these.
- Recommendation: host-side `nftables` default-deny with only assigned
  per-run egress runtime grants (initially none), IMDS/Azure endpoints
  dropped at the host firewall, no host filesystem shared to the guest
  except a per-run scratch mount the cleanup control must erase. This
  matches the Firecracker no-virtio-fs limitation rather than working
  around it.
- OWNER DECISION: none beyond runtime choice.

### D-RUNTIME-5 · Operational maturity and independent review

- Research (checked 2026-10-23): Firecracker is Apache-2.0 with an active
  2026 release cadence including a security fix (CVE-2026-5747); Kata
  Containers and Cloud Hypervisor are also active with recent releases;
  QEMU is universally deployed. Vendor marketing and community essays are
  not independent security proof; the working rule stays "reviewer
  confirms, marketing does not."
- Consequence: even a mature runtime does not translate directly into a
  review pass. The evidence chain (WP-4) and reviewer exist for this
  reason; runtime maturity lowers effort, not the bar.
- OWNER DECISION: who is the named reviewer (already a roadmap decision,
  restated here since it also gates runtime acceptance).

## C. Artifact encryption and key management

### D-KMS-1 · Key-manager class

- Question: cloud KMS, self-hosted Vault, or a hardware/external key
  arrangement?
- Verified (repository): `ArtifactKeyProvider` already defines the
  interface — `seal_key()` returns `(key_id, wrapped_key)` and
  `open_key(key_id)` returns the wrapped-DEK bytes
  (`integrations/application/sealing.py`); `AesGcmSealer` takes the
  wrapped-DEK blob format version 1 with a key identifier and 12-byte
  nonce (`integrations/infrastructure/aes_gcm.py`) — an envelope
  architecture; a cloud KMS wraps data-encryption keys and never returns
  the KEK (Research: AWS KMS KEK never leaves region; GCP Cloud KMS
  key material stored region-pinned; both verified 2026-10-23).
- Options:
  - (a) Cloud KMS aligned with the artifact store's provider (managed
    encryption-as-service, zero ops, non-exportable keys, per-call audit);
    recommended where the artifact store also lives at that provider.
  - (b) HashiCorp Vault (self-managed, portable, supports more than KMS —
    dynamic secrets/PKI; adds an unseal/recovery surface the single
    operator must own; Vault does not replace a KMS for data-at-rest wrap
    semantics as directly).
  - (c) External/HYOK KMS (customer-held external key manager; strongest
    separation, adds a latency and availability dependency — Research
    confirms every operation becomes a remote call; Google's EKM requires
    a partner; Azure EKM still in preview per vendor docs).
- Dependency: (a) only becomes coherent once D-HOST-1 picks an ecosystem;
  if the host is a commodity bare-metal provider, (b)/(c) become the
  natural comparison; the repository does not pick the vendor.
- Failure behavior: verified — `UnavailableKeyProvider` fails closed with
  `encryption_unavailable`; a sealed store refuses to continue, never
  falls back to plaintext (`sealed_store.py` and its tests). This is the
  design's answer to "what happens when the key manager is unavailable":
  block, never degrade. The status page must surface this as
  non-productivity, not as data loss.
- Custody/access boundaries: keys never reach untrusted guests; the guest
  path already carries no artifact keys (assessment area 1); the
  recommended pattern is that the *worker* holds a short-lived KMS
  credential, the *guest* never sees container/dek material, and
  `open_key` lives in the control plane's read path.
- Recommendation: **pick the KMS class that matches where the artifact
  store will run** (default: cloud KMS of the same provider if the store
  is cloud; self-hosted Vault if fully self-managed hardware is used and
  no cloud KMS exists); defer vendor pick to D-HOST-1's outcome. Do NOT
  start integration with a fake or local-only provider; the current
  plaintext-at-rest limitation stays explicit until the real manager is
  wired and round-trip tested. OWNER DECISION: manager class and vendor.
- Blocks: WP-5 (external KMS integration), production claim of
  encryption-at-rest.

### D-KMS-2 · Key custody, rotation, revocation boundaries

- Verified (repository): key ids are part of the sealed blob format, so
  rotation or revocation does not need rewrite-on-read; revocation of an
  old key id has defined behavior (open refuses; the artifact store is a
  non-trusted store that must not be silently degraded).
- Recommendation: rotation only via a new key id (no in-place key
  mutation); revocation list maintained at the KMS provider; a logged
  "key unavailable" drill is required before production approval.
- OWNER DECISION: rotation cadence default (suggest yearly or on
  compromise) and who can revoke.

### D-KMS-3 · Overflow: export of wrapped keys to the guest?

- Question: may a wrapped data key travel to the guest so the guest can
  seal/unseal its own outputs?
- Verified (repository): the standing contract says guests receive no
  artifact keys; `seal_key`/`open_key` sit in the control plane; ADR-008
  forbids secret material in guest mount/env.
- Recommendation: **no wrapped keys in the guest, ever.** The guest writes
  plaintext through a controlled path (or the control plane post-processes
  sealing after run completion). Alternative patterns exist but each
  weakens the guest boundary.
- OWNER DECISION: none required unless the owner wants to override the
  standing contract.

## D. Retention and audit

### D-RET-1 · Data categories and business purpose

- Verified (repository): three stores exist in durable terms — artifact
  blobs (test output), audit log (append-only), outbox/envelope rows;
  `integrations/application/retention.py` currently accepts only
  `policy="disabled"` and never treats audit or outbox rows as artifact
  keys. This separation is verified and fixed (unit-tested).
- Recommendation: classification is the owner's business decision;
  engineering proposes the categories: raw run artifacts (deleteable
  after retention), audit rows (never deleteable by retention), outbox
  rows (governed by durable-jobs rules, not by artifact retention).
- OWNER DECISION: confirm or amend the classification.

### D-RET-2 · Retention duration proposal (PROPOSAL ONLY)

- PROPOSAL, not policy: delete raw artifacts 90 days after the run
  reaches a terminal state; retain audit indefinitely. Rationale: matches
  the flaky-test investigation workflow window; audit must outlive
  investigations; the number is arbitrary and must be replaced by a
  chosen value.
- Verified: no current retention is enabled; audit rows are never
  deleteable by this pipeline (assessment area 9).
- OWNER DECISION (must come from product/legal/security, not from this
  repository): duration, tenant scope, and the legal-hold question (a
  legal hold overrides deletion — nothing in the repository implements
  legal hold; treat that as absent until separately designed).
- Blocks: WP-6 retention implementation; production exposure of artifact
  storage at scale (growth risk listed in the assessment).

### D-RET-3 · Deletion authorization, verification, recovery

- Verified: `plan_deletion` refuses on an unconfigured policy; deletion of
  any audit/outbox record through retention is structurally refused.
- Recommendation: retention, when finally implemented, stays: (i) dry-run
  first, (ii) artifact keys only, (iii) no SQL on audit/outbox rows ever
  through this path, (iv) restart-safe idempotent batch deletion, (v) the
  deletion run itself is audit-logged (a new audit entry type). Recovery
  of a deleted artifact is intentionally not possible; deletion is
  one-way; docs must say so.
- OWNER DECISION: approval to implement at all, and the "who presses the
  button" question (operator-only or automated).

### D-RET-4 · Legal and business approval requirement

- Verified: the assessment flags retention as BLOCKED pending a
  product/legal/security approval; nothing in this repository can grant
  it.
- OWNER DECISION: obtain the approval; record the approvers and date in a
  dated decision record; the record is the prerequisite for WP-6 code.

## E. GitHub App

### D-GH-1 · App ownership and repository scope

- Question: who creates/provisions the GitHub App, and which repositories
  may it ever see?
- Verified (repository): the control plane mints installation tokens as
  `contents:read` for exactly one repository and never places tokens in
  the guest (`integrations/infrastructure/installation.py`); the
  composition root uses `GitHubCodeHost(None)` statically (no live
  credentials). Scope is enforced *per token mint*, not by App policy
  alone — but App installation scope is the outer bound.
- Recommendation: the owner creates the App (or delegates to a personal
  identity boundary they own), installs it on at most one test fixture
  repository for verification, and the "wallet" of allowed repositories
  grows only by explicit record.
- OWNER DECISION: App identity, install scope, account ownership.
- Blocks: WP-7 (live minting and least-privilege proof).

### D-GH-2 · Minimum permissions

- Verified: `contents:read` as the entire permission; no write scope
  anywhere in the code path; an audit exists of token usage
  (`installation.py` limits lifetime, never logs the token).
- Recommendation: keep it exactly `contents:read`; add no scopes when
  provisioning (metadata-only scopes default on — confirm in GitHub UI
  that no additional permission is accidentally accepted during install;
  this is a manual-review item in WP-7).
- OWNER DECISION: ratify the `contents:read` minimum when registering.

### D-GH-3 · Credential provisioning and rotation

- Options: (a) GitHub App private key stored plaintext on the control
  plane (current design), (b) key wrapped inside the artifact secret
  store, (c) key held by a secrets manager and pulled by the control
  plane at start (recommended once a secret manager exists — overlaps
  D-KMS-1 — but not a blocker for WP-7's first live proof).
- Verified: no App ID/private key exists today; the storage of a private
  key has never been tested against a real provider.
- Recommendation: record where the key will live before creating it; if
  (c), the same KMS decision covers it.
- OWNER DECISION: create the App, choose key storage, choose rotation
  cadence (GitHub enforces App key expiry? Research pending — flag as
  unknown; GitHub manual private-key rotation is the current process to
  assume).

### D-GH-4 · Token minting verification and incident response

- Verified: token minting from `contents:read` App installation is
  implemented but never run against GitHub; live minting is NOT VERIFIED
  per the assessment.
- Recommendation: WP-7's acceptance includes a dated record of a live
  minted token used to read only one authorized repository; plus an
  incident-response note: if the private key is suspect, rotate via the
  GitHub UI immediately, invalidate outstanding tokens (short TTL — the
  codebase considers tokens "until it expires" per
  `installation.py:78`), and re-verify scope by re-reading the install
  record.
- OWNER DECISION: none once D-GH-1/D-GH-3 are settled.

## F. Monitoring and operations

### D-OPS-1 · Metrics and alerting requirements

- Verified (repository): no monitoring/alerting code exists (checked
  2026-10-23; the assessment area 12 calls this a FAIL). The runbook
  already enumerates the alert set: authority unavailable, unreadable
  ledger, repeated rejected macs, cleanup unable to run (§12).
- Options: (a) self-hosted Prometheus + Grafana + Alertmanager + Loki
  (Research, checked 2026-10-23: Kubernetes-convention stack, mature,
  zero recurring software cost, operator burden on us), (b) a managed
  observability as-a-service (vendor recommendation externalized; usage-
  metered), (c) bare minimum cron-probes only (not an honest control
  plane; kept only as a pre-production stop-gap).
- Recommendation: (a) — an open-source stack run *outside* the execution
  host (so it is not compromised with the host it watches), plus runbook
  §12's alert list; defer a vendor decision.
- OWNER DECISION: accept self-hosted ops burden or pick a vendor; also
  who is the named on-call operator.

### D-OPS-2 · Log redaction and audit integrity

- Verified: report tooling already refuses secret-shaped values and
  counts environment names without storing them
  (`evidence_report.py` `_SECRET_PATTERNS`, `_env_rule`);
  `installation.py` never logs tokens. Application logs today may still
  carry request bodies (verified limitation in the assessment).
- Recommendation: before any logs leave the machine, a redaction pass is
  required for logs that touch user content; audit rows (append-only,
  tamper-evident by `audit/__init__.py` design) are exported raw, not
  redacted.
- OWNER DECISION: which export path first; logging redaction policy as a
  documented rule.

### D-OPS-3 · On-call ownership

- Question: who is notified at 2am?
- Verified: no on-call exists. For a single-operator project the honest
  answer is the owner.
- Recommendation: the owner is on call initially; monitoring burden is
  deliberately low (one host, small alert list). OWNER DECISION: accept.

### D-OPS-4 · Incident response and escalation

- Verified: the runbook §13 incident plan (stop register, keep
  authenticated cancel/complete, stop cleanup, retain audit/outbox)
  exists as a plan, not a drill.
- Recommendation: one tabletop drill (WP-8) before any production
  authorization; escalation goes owner-side first.
- OWNER DECISION: ratify the incident plan and drill timing.

### D-OPS-5 · Recovery, rollback, retirement

- Verified: worker restarts cleanly (durable-jobs design); a control-plane
  rebuild story is not yet documented (the assessment lists a rollback
  path for the whole deployment as missing).
- Recommendation: recovery runbook for the *control plane* (Postgres
  rebuild from backup, worker redeploy) belongs to WP-8; the host
  retirement plan is D-HOST-7's.
- OWNER DECISION: none beyond cadence.

### D-OPS-6 · Operational acceptance criteria

- Recommendation (a concrete proposal): production is operationally
  acceptable when (i) a drill (log kills a run, host reboots mid-run, key
  manager unavailable) has each been executed once and passed, (ii) the
  alert list from the runbook each has a ticket/response path, (iii) a
  named on-call exists. Anything less is non-go regardless of security
  review.
- OWNER DECISION: ratify the acceptance criteria, since they also gate
  WP-10.

---

## Consolidated recommendations and approval summary

| ID | Question | Recommendation | Confidence | Owner needed? |
|---|---|---|---|---|
| D-HOST-1 | Hosting model | One dedicated/bare-metal KVM host first | High (boundary clarity) | Yes |
| D-HOST-2 | Tenancy | Single tenant, single operator | High | Yes |
| D-HOST-3 | Region | Confirm-or-reject "no residency constraint" | — | **Yes (cannot be guessed)** |
| D-HOST-4 | Sizing | Small single host, scale up first | Medium (usage portrait unknown) | Yes (with D-HOST-5) |
| D-HOST-5 | Budget | Define ceiling & owner | — | **Yes** |
| D-HOST-6 | Access | Runbook §5 + no shared panel creds | High | Confirm |
| D-HOST-7 | Patch/retire | Monthly window, rehearsed retirement | Medium | Confirm |
| D-RUNTIME-1 | Runtime | **Firecracker** primary; Cloud Hypervisor fallback | High | **Yes** |
| D-RUNTIME-2 | Threat model | Runbook baseline, stricter later | High | Confirm |
| D-RUNTIME-3 | Lifecycle | Mandatory digest pinning, no snapshots for untrusted input | High | Confirm |
| D-RUNTIME-4 | Network | Default-deny + no shared fs | High | Confirm |
| D-RUNTIME-5 | Maturity | Reviewer validates, marketing does not | High | Reviewer choice |
| D-KMS-1 | KMS class | Match artifact-store ecosystem (cloud KMS default) | Medium | **Yes (vendor)** |
| D-KMS-2 | Rotation | Key-id rotation, no in-place keys | High | Confirm cadence |
| D-KMS-3 | Guest keys | Never | High | Confirm |
| D-RET-1 | Categories | Artifacts deleteable, audit/outbox not | High | Confirm |
| D-RET-2 | Duration | **90-day PROPOSAL only** | Low (must be set by owner) | **Yes** |
| D-RET-3 | Deletion | Dry-run first, artifact keys only, audited | High | Approve to implement |
| D-RET-4 | Approval | Product/legal/security signed record | — | **Yes** |
| D-GH-1 | App owner | Owner creates, installs on one test repo | High | **Yes** |
| D-GH-2 | Permissions | `contents:read` only | High (already enforced) | Ratify |
| D-GH-3 | Key storage | Secret manager pull at start | Medium | **Yes** |
| D-GH-4 | Verification | WP-7 live proof | — | ratify record |
| D-OPS-1 | Monitoring | Self-hosted Prometheus/Grafana/Loki outside the host | Medium–High | **Yes (vendor/on-call)** |
| D-OPS-2 | Redaction | Redact logs leaving the machine, export audit raw | High | Confirm |
| D-OPS-3 | On-call | Owner | — | **Yes** |
| D-OPS-4 | IR | Runbook §13 + drill | High | Confirm |
| D-OPS-5 | Recovery | Control-plane recovery runbook in WP-8 | Medium | Confirm |
| D-OPS-6 | Ops acceptance | Drill-passed + alert-pathed + named on-call | High | Ratify |

Highest-priority decisions to unblock the most work: **D-HOST-1/3/5,
D-RUNTIME-1, D-KMS-1, D-GH-1, D-OPS-1**, plus **D-RET-2/D-RET-4** if
retention is wanted early. Everything else can be ratified in one pass.

Until those owner decisions exist, no work package in
[INFRASTRUCTURE-IMPLEMENTATION-PLAN](INFRASTRUCTURE-IMPLEMENTATION-PLAN.md)
past WP-1 can start, and the repository's posture stays exactly as the
assessment describes: untrusted execution disabled, artifacts plaintext at
rest, no retention, no live GitHub test, no monitoring deployed.

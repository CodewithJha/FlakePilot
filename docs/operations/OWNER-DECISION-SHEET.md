# Owner decision sheet

Stage 10K / WP-1. A review aid for the repository owner, derived from the
existing registers — it introduces no new decisions and changes nothing in
the roadmap or implementation plan. Source documents:
[INFRASTRUCTURE-DECISIONS](INFRASTRUCTURE-DECISIONS.md) (full detail),
[INFRASTRUCTURE-OPTIONS-ANALYSIS](INFRASTRUCTURE-OPTIONS-ANALYSIS.md)
(evidence and sources, checked 2026-10-23),
[INFRASTRUCTURE-IMPLEMENTATION-PLAN](INFRASTRUCTURE-IMPLEMENTATION-PLAN.md)
(work packages WP-1..WP-10), [EXECUTION-HOST-RUNBOOK](EXECUTION-HOST-RUNBOOK.md).

**Standing posture, unchanged by this sheet:** production untrusted-code
execution stays disabled; `ClosedSandbox`, the fail-closed guards and
all-false `IsolationChecks` are untouched; artifacts stay plaintext at
rest; retention stays disabled; no GitHub App exists; no monitoring is
deployed; nothing has been purchased.

## Executive summary

The repository is fully prepared to accept owner decisions; nothing further
can start without them. Five decisions unblock the most work:

1. **D-HOST-1** — what kind of machine the execution host is (bare metal
   vs cloud VM with nested KVM vs self-managed).
2. **D-HOST-3** — whether any region/data-residency constraint applies.
   The repository cannot guess this; it is a hard unknown.
3. **D-HOST-5** — the budget ceiling and the cost owner.
4. **D-RUNTIME-1** — the isolation runtime. Engineering recommends
   **Firecracker**, on the strength of the smallest device model, active
   2026 patching (CVE-2026-5747 fix), and a real production record for
   untrusted code. Selecting it does **not** establish that its isolation
   is sufficient — only measured evidence (WP-4) and an independent
   review (WP-9) can do that.
5. **D-KMS-1** — the key-management *class* (the vendor defers to the
   hosting choice). Until a real key manager is wired and round-trip
   tested, the plaintext-at-rest limitation remains explicitly stated.

After those, one pass can ratify the remaining confirmations (access,
patching, threat model, network policy, retention categories, GitHub
permission minimum, redaction). Independent design work — key-management
testing, retention implementation, GitHub live proof, monitoring drills —
can each be prepared locally but produces no live capability until its own
explicit approval. A fill-in response block is provided at the end;
unanswered items simply stay `UNDECIDED`, which is always a safe state.

## Group A — Decisions needed before infrastructure provisioning

Provisioning is work package WP-2 in the implementation plan. Per its
verified prerequisites it needs, at minimum: D-HOST-1, D-HOST-2, D-HOST-3,
D-HOST-4, D-HOST-5, D-RUNTIME-1.

### A-1 · Hosting model, provider, tenancy — D-HOST-1, D-HOST-2

- Question: on what kind of infrastructure does the execution host run, and
  who administratively owns it?
- Options documented (options analysis §1): bare-metal cloud instance ·
  cloud VM with nested KVM (Azure/DigitalOcean/Google Cloud named by an
  independent source, checked 2026-10-23; AWS VMs generally do not expose
  nested KVM) · self-managed dedicated server.
- Recommendation: one bare-metal / dedicated-tenancy KVM host first;
  single tenant; one named operator over SSH keys only (D-HOST-2, and
  access rules in D-HOST-6). Rationale: strongest boundary, fixed host and
  kernel into the evidence fingerprint, no nested-virtualization risk.
- Main trade-off: bare metal costs more on premium clouds and takes longer
  to start; a nested-KVM cloud VM is fastest and cheapest but the second
  virtualization layer complicates the trust argument a reviewer has to
  accept.
- Blocks provisioning: yes. Deferrable: no.

### A-2 · Region and data residency — D-HOST-3

- Question: is there any region or data-residency constraint (legal,
  contractual, compliance) for where test artifacts and evidence live?
- Options documented: no constraint (default single region, host and
  control plane colocated) · a residency requirement the owner must state
  · genuinely not applicable.
- Recommendation: none possible — this is the owner's knowledge, not
  engineering's. Recorded as `UNDECIDED` until answered.
- Main trade-off: guessing wrong affects artifact storage, evidence
  location, and the key manager's region behavior (non-exportable keys are
  region-pinned per provider docs checked 2026-10-23).
- Blocks provisioning: yes. Deferrable: no.

### A-3 · Sizing and expected workload — D-HOST-4

- Question: what concurrency and volume should the first host absorb?
- Options documented: one small host scaled up first (recommended) vs
  multi-host scaling (needs multi-host evidence review first — deferred).
- Anchor fact (verified in code): per-run bounds are wall 5–120 s, memory
  64–2048 MiB, pids 8–256 (`execution/domain/plane.py`), so a few
  concurrent worst-case runs fit an 8-vCPU / 16–32 GiB class host.
- Recommendation: one small host; needs the owner's usage portrait to size
  honestly — **do not assume one**.
- Main trade-off: undersizing surfaces early (annoying but safe,
  single-host), oversizing wastes approved spend.
- Blocks provisioning: yes (WP-2 needs it to know what to buy).
  Deferrable: no — but it can be refined after the first usage data.

### A-4 · Budget ceiling and cost owner — D-HOST-5

- Question: what monthly spend is authorized, whose money, and how is the
  approval recorded?
- Cost categories only are documented (no figures asserted): bare-metal
  monthly line item (spectra from secondary-market low-double-digit
  USD/month class to multi-thousand USD/month on premium clouds —
  research anecdote, not verified pricing), cloud-VM usage item,
  self-managed capital cost. The exact number is the owner's.
- Recommendation: a single small-dedicated-host line item with an explicit
  ceiling, ratified in a dated record.
- Blocks provisioning: yes. Deferrable: no.

### A-5 · Isolation runtime selection — D-RUNTIME-1

- Question: which VMM/runtime will the host run?
- Options analyzed (options analysis §2): Firecracker (recommended) ·
  Cloud Hypervisor (fallback) · QEMU `microvm` · Kata Containers · gVisor
  (last two rejected for the baseline — Kata brings a Kubernetes
  orchestration layer the ledger does not need; gVisor's userspace-kernel
  model does not meet the measured `per_run_microvm` target without
  specific review).
- Recommendation: Firecracker. Evidence: ~5 virtio devices (minimal
  surface), ~125 ms boot / ~5 MiB overhead claims, built-in rate limiters
  mapping onto the six limits, jailer+seccomp, active 2026 maintenance
  (CVE-2026-5747 fix), production use for untrusted code at AWS
  Lambda/Fargate, Fly.io, Vercel Sandbox, E2B — all per cited sources,
  checked 2026-10-23. Its limitations (no shared filesystem, no hotplug,
  no GPU) match FlakePilot's requirements rather than fighting them.
- Main trade-off / risk: selecting a runtime is a **candidate selection**,
  not a sufficiency verdict — isolation sufficiency is only established by
  measured guest-observed evidence (WP-4) passing independent review
  (WP-9). Maturity lowers effort, not the bar.
- Blocks provisioning: yes (host image/kernel choice depends on the
  runtime). Deferrable: no; the fallback option stays open only until the
  host is leased.

### A-6 · Security reviewer and evidence-review process — D-RUNTIME-5

- Question: who is the named independent reviewer for the evidence bundle
  (WP-4 output → WP-9), and what cadence applies?
- Options documented: a named person outside the implementation and
  outside the repository's toolchain; re-collection cadence per identity
  change (kernel/runtime/digest changes invalidate evidence — verified
  rule in `ops/isolation/evidence_report.py`).
- Recommendation: name the reviewer now (any competent external reviewer)
  so WP-4 has a recipient when it runs.
- Main trade-off: without a reviewer, collected evidence has no destination
  and the authorizing decision (WP-10) has no input.
- Blocks provisioning: no; **blocks the evidence/review chain** (WP-4
  handoff, WP-9). Deferrable: to just before WP-4, but naming early is
  cheaper.

### Also pre-provisioning confirmations (ratify now if convenient)

- **D-HOST-6** access model (runbook §5 + no shared panel credentials) —
  blocks provisioning operationally (the host must be configured this way
  at creation).
- **D-HOST-7** patch window and retirement plan — blocks provisioning
  only in the sense that the cadence should exist before the host does.

## Group B — Decisions that can be prepared independently

These need owner input but not, in the first instance, infrastructure.
Design and code preparation may proceed once decided; **none of them
authorizes secret generation, service registration, data deletion, or any
production wiring.**

| ID | Question | Recommendation | Boundary |
|---|---|---|---|
| D-KMS-1 | Key-manager *class* (vendor defers to D-HOST-1) | Match the artifact store's ecosystem: cloud KMS if the store is on that cloud; self-hosted Vault on fully self-managed hardware; external/HYOK only if a written requirement demands it | Testing with a real manager needs an account (external); the envelope code (`ArtifactKeyProvider`, versioned AES-GCM blob) is verified and provider-agnostic |
| D-KMS-2 | Rotation/revocation cadence | New key-id rotation, no in-place key mutation; revocation at the manager; an unavailability drill before any production claim | Final cadence mechanics verifiable only against the real manager |
| D-KMS-3 | May wrapped keys reach untrusted guests? | **Never** (standing contract; sealer is control-plane side) | Owner confirmation only |
| D-RET-1 | Data categories | Artifacts deleteable after approved retention; audit rows never through retention; outbox governed by durable-jobs rules | Verified structural separation exists (`retention.py`) |
| D-RET-2 | Retention duration | 90-day proposal **only**; the real number is the owner's with product/legal/security input | No deletion until D-RET-3/4 approved; the sheet's silence means `UNDECIDED` |
| D-RET-3 | Deletion implementation rules | Dry-run first, artifact keys only, no SQL against audit/outbox via this path, deletion events audit-logged, one-way (no recovery) | Implementation blocked until D-RET-4 record exists |
| D-RET-4 | Who signs the retention approval | The owner plus the product/legal/security reviewers named by the owner | The signed, dated record is the prerequisite; nothing else suffices |
| D-GH-1 | App ownership and scope | Owner creates the App; installs on at most one test fixture repository | No App may be created by this task; scope growth only by explicit record |
| D-GH-2 | Permission minimum | `contents:read` exactly (verified already enforced in `installation.py`) | Ratify the minimum at registration time |
| D-GH-3 | Credential storage | Pull the App private key from a secrets manager at control-plane start (pairs with D-KMS-1); decide before creating the key | Key creation itself is an owner action (WP-2/WP-7), not authorized by this sheet |
| D-OPS-1 | Monitoring approach | Self-hosted Prometheus + Grafana + Alertmanager + Loki on infrastructure **separate** from the execution host | Vendor decision is the owner's; deployment is WP-8, needs the drill to claim operational acceptance |
| D-OPS-2 | Log redaction | Redact before logs leave the machine; export audit rows raw | A documentation rule now; enforcement is implementation work |
| D-OPS-3 | On-call ownership | Named person (recommendation: the owner initially) | A name is required before WP-8's drill can be "seen by on-call" |
| D-OPS-4 | Incident response | Adopt runbook §13 (stop register, keep authenticated cancel/complete, cleanup stopped, audit/outbox retained) + one tabletop drill | The drill is WP-8; the plan is ratifiable now |
| D-OPS-5 | Recovery/rollback | Control-plane recovery runbook written in WP-8 | Design accepts now |
| D-OPS-6 | Operational acceptance criteria | Production is operationally acceptable when the three drills pass, every runbook alert has a response path, and a named on-call exists | Ratify the criteria now; they gate WP-10 |

D-RUNTIME-2 (threat model), D-RUNTIME-3 (mandatory digest pinning, no
snapshots for untrusted input), and D-RUNTIME-4 (network default-deny, no
shared filesystem) sit here too as engineering recommendations awaiting a
one-pass ratification.

## Group C — Decisions that must remain deferred

These cannot be finalized until real infrastructure exists or independent
review has occurred. Each lists the missing evidence.

| ID / package | What is deferred | Missing evidence |
|---|---|---|
| D-RUNTIME-1 (sufficiency, not selection) | The verdict that the chosen runtime's isolation is actually sufficient | Guest-observed measurements of all seven `IsolationChecks` on the real host (WP-4) + the named reviewer's sign-off (WP-9). Selection alone never establishes sufficiency |
| D-KMS-1 (final vendor + testing) | Vendor pick and any encryption claim | The hosting decision (ecosystem match) and a real account; the round-trip drill (WP-5). Until then: plaintext-at-rest limitation stays explicit everywhere |
| D-KMS-2 (tested failure semantics) | Verified unavailability behavior in production shape | The unavailability drill against the real manager — code-level fail-closed is verified now, runtime-level is not |
| D-RET-2 (final duration) | The real retention period and legal-hold handling | The product/legal/security approval (D-RET-4); legal hold is not implemented and must not be presumed |
| D-GH-4 | Ratification of the live-minting record | The live authenticated test itself (WP-7) with real credentials — nothing about live GitHub auth can be claimed before then |
| D-OPS-1 (deployment efficacy) | Proof the alerts fire and are seen | The monitoring instance and the three drills (WP-8) |
| D-OPS-6 (ratified acceptance) | Operational acceptance | Drill transcripts — criteria are ratifiable now, the pass/fail record is not |
| WP-9/WP-10 | Security review verdict and production go/no-go | Everything above; explicitly the last gate. Production authorization cannot be granted now under any reading of the current evidence |

## Owner Responses Needed

Fill in freely; anything left is `UNDECIDED`, which blocks that branch only.

- Hosting preference (D-HOST-1 / D-HOST-2):
- Region/data-residency requirements (D-HOST-3):
- Maximum initial budget (D-HOST-5):
- Expected workload (runs/day, concurrency) (D-HOST-4):
- Isolation runtime (D-RUNTIME-1) — accept Firecracker proposal / choose other:
- Independent security reviewer (D-RUNTIME-5):
- Key-management preference (D-KMS-1):
- Retention requirements — duration, legal hold, approver names (D-RET-2/3/4):
- GitHub App — create now? identity, scope, key storage (D-GH-1/2/3):
- Monitoring/on-call ownership (D-OPS-1/3):

## What Approval Does Not Authorize

- Reviewing or completing this sheet **does not authorize spending**. WP-2
  provisioning requires a separate, explicit approval recorded with the
  cost owner's name and date.
- Selecting a runtime **does not establish that its isolation is
  sufficient**. Sufficiency exists only as measured, guest-observed
  evidence passing independent review.
- Production untrusted-code execution **remains disabled**.
- Existing fail-closed behavior, `ClosedSandbox`, and all-false
  `IsolationChecks` **remain unchanged** by any answer on this sheet.
- The evidence collector (`ops/isolation/`) does not authorize production
  execution — a report is evidence, never authorization.
- Encryption cannot be claimed until a **real external key manager** is
  wired and the round-trip is tested in a deployed configuration; no fake
  or local-only provider counts, and none is proposed.
- Retention (and any data deletion) **remains disabled** until the
  approval record required by D-RET-4 exists — approved by humans outside
  this repository.
- GitHub App live authentication **remains unverified** until an actual
  authenticated test happens (WP-7); creating the App and requesting
  credentials are owner actions, never urgent ones forced by this sheet.
- **No Stage 10L work begins automatically.** Every subsequent work
  package starts only on your explicit instruction.

## Next Safe Actions (all local, no spend, no registrations)

1. Owner fills the response block above (or marks items `UNDECIDED`).
2. Record the answers as WP-1's dated decision records
   ([INFRASTRUCTURE-IMPLEMENTATION-PLAN](INFRASTRUCTURE-IMPLEMENTATION-PLAN.md)
   WP-1 acceptance criteria).
3. Ratify the Group B confirmations in the same pass.
4. Only after provisioning approval: WP-2 buys the recorded, sized host.
5. In parallel or earlier: WP-5/WP-6/WP-7/WP-8 *design* phases per their
   plan entries, still without producing any live capability.

## WP-2 provisioning prerequisite checklist

WP-2 cannot start until **all** of these have recorded owner answers:

- [ ] D-HOST-1 — hosting model and provider chosen
- [ ] D-HOST-2 — tenancy and the named administrative operator
- [ ] D-HOST-3 — region/residency constraint answered or explicitly waived
- [ ] D-HOST-4 — sizing and a written usage portrait
- [ ] D-HOST-5 — budget ceiling and cost owner approved
- [ ] D-RUNTIME-1 — runtime selected (host image depends on it)
- [ ] D-HOST-6 / D-HOST-7 — access and patching cadence ratified

Not required for WP-2 (needed later): D-KMS-1 vendor, D-RET-2/3/4,
D-GH-1..3, D-OPS-1..6, reviewer/WP-4/WP-9/WP-10.

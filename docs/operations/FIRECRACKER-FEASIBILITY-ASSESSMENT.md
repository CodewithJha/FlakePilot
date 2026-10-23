# Firecracker technical feasibility assessment

Stage 10K (WP-1 continuation). Read-only, no-cost technical assessment of
Firecracker as the preferred isolation-runtime *candidate* for a future
dedicated execution host. Nothing is installed, provisioned, purchased, or
wired; no production behavior changes; no stage is created beyond 10K. The
live product posture is unchanged and remains the one assessed in
[PRODUCTION-READINESS-ASSESSMENT](PRODUCTION-READINESS-ASSESSMENT.md):
untrusted execution disabled, `ClosedSandbox` in place.

Vocabulary: **Verified** (checked in current source, or against a cited
primary source, with the date checked recorded), **Inferred** (a reasoned
consequence of verified facts, labeled as such), **Unknown** (not
establishable from the sources consulted — left unknown, not guessed).
Documented product behavior is Firecracker's vendor documentation speaking;
it is **not** independent proof that a future FlakePilot deployment will be
secure. Independent security evidence for *our* deployment can only come
from real measurement on real infrastructure and independent review
(WP-4/WP-9), which have not happened.

## 1. Executive summary

- Firecracker remains **technically feasible as a candidate** for a future
  dedicated execution host: its documented requirements (Linux + KVM) match
  the standing host contract in [EXECUTION-HOST](EXECUTION-HOST.md), its
  documented limitations (no GPU/PCI passthrough, no live migration, no
  virtio-fs) are irrelevant to running short untrusted test commands, and
  its resource-control surface (per-microVM vCPU/memory config, jailer
  cgroups/rlimits, per-device rate limiters) covers everything FlakePilot's
  quota domain already models.
- The compatibility question that is still **open** is the one no
  documentation can answer: whether the real, not-yet-chosen host (kernel
  version, CPU, virtualization availability) boots current Firecracker
  releases correctly. That requires the host, and the host requires WP-2,
  which is blocked by the zero-budget hard constraint, `UNDECIDED` residency,
  and the absence of a selected provider/operator. There is **no host** to
  test on today (`BLOCKED`, not merely unplanned).
- macOS and Docker cannot establish the required boundary. Firecracker runs
  on Linux with KVM; the current development machine is a Mac, and a
  Docker-on-Mac experiment would share a Linux VM kernel — `shared_kernel_container`
  evidence at best, which by definition never satisfies `per_run_microvm`.
  No Firecracker was installed here and no Mac configuration was changed.
- **Go/no-go** (next investigation step only): **GO** for continuing the
  local, no-cost investigation — a pre-provisioning host-compatibility
  checklist and a measurement plan written in the runbook's terms, plus a
  threat-model walkthrough of Firecracker against D-RUNTIME-2. **NO-GO** for
  anything spend-bearing: provisioning, installation, image building, or any
  claim that a microVM boundary exists.

## 2. Scope and non-goals

In scope: host prerequisites (A), guest lifecycle (B), security-boundary
control analysis (C), a proposed-not-deployed integration sketch (D), and
operational suitability (E), plus a Firecracker ↔ Cloud Hypervisor
comparison. Out of scope and not done:

- no spending, provisioning, subscriptions, accounts, or credentials;
- no Firecracker installation or image building (not even locally);
- no application code, runtime wiring, composition-root, or configuration
  changes; `IsolationChecks` untouched (all false);
- no retention change, no KMS selection, no encryption wiring;
- no Stage 10L; no new work-package authorization;
- no cost figures, workload numbers, or availability guarantees (the owner
  supplied none — D-HOST-4 remains `UNDECIDED`), and none are invented here.

## 3. Verified FlakePilot architecture (what the code actually uses)

All facts below were re-verified in the working tree at HEAD
`afcfdb9` (2026-10-23):

- `_sandbox` returns `ClosedSandbox()`
  (`src/flakepilot/interface/composition_root/reproduction.py:392`). No
  repository code is ever executed by the control plane.
- The worker uses `control_plane_reaper().reclaim()` and does not open the
  host ledger (`worker.py:64`); host-side handoff is `RefusedHostHandoff`
  (`src/flakepilot/execution/application/plane.py:69`). `UnavailableExecutionPlane`
  is the production plane object (`plane.py:37`).
- Quota domain: wall-clock 5–120 s, memory 64–2048 MiB, PIDs 8–256
  (`src/flakepilot/execution/domain/plane.py:14–16`), enforced today as
  request validation, **not** as OS-level enforcement on any sandbox.
- `UnavailableIsolation` reports all `IsolationChecks` false
  (`src/flakepilot/execution/application/reproduction.py:88,166`); the
  evidence ladder is `simulation → shared_kernel_container →
  dedicated_host_container → per_run_microvm → production_approved`
  (`src/flakepilot/execution/domain/capability.py`), and weaker evidence can
  never satisfy a stronger level.
- The evidence collector (`ops/isolation/evidence_report.py`,
  `SCHEMA_VERSION = 1`, `MECHANISM_LEVEL` map) accepts a check as passing
  only from an unsimulated, guest-observed entry; simulated or host-side
  observations derive `ERROR`. Adding Firecracker to `MECHANISM_LEVEL` may
  happen **only after** a real measurement exists.
- Artifacts: `LocalFsArtifactStore`, plaintext at rest; `AesGcmSealer` and
  any key provider exist as untested-in-production code and are **not
  wired** in the composition root. Retention selects nothing (`disabled`
  policy only).

Reading of these facts: FlakePilot's side of the future integration —
durable jobs, quota domain, artifact store, evidence schema, fail-closed
guards — exists in code and was not built around any specific VMM. Nothing
in it assumes Firecracker; that is what makes the candidate assessment
meaningful.

## 4. Authoritative sources consulted (all checked 2026-10-23)

Primary Firecracker sources, read from the `firecracker-microvm/firecracker`
repository at `main`; each claim in this document traces to one of these:

| # | Source | URL |
|---|---|---|
| S1 | Design (threat containment, sandboxing, MMDS, monitoring) | `https://raw.githubusercontent.com/firecracker-microvm/firecracker/main/docs/design.md` |
| S2 | Production host setup recommendations | `https://raw.githubusercontent.com/firecracker-microvm/firecracker/main/docs/prod-host-setup.md` |
| S3 | The Firecracker jailer | `https://raw.githubusercontent.com/firecracker-microvm/firecracker/main/docs/jailer.md` |
| S4 | Kernel support policy | `https://raw.githubusercontent.com/firecracker-microvm/firecracker/main/docs/kernel-policy.md` |
| S5 | Creating custom rootfs and kernel images | `https://raw.githubusercontent.com/firecracker-microvm/firecracker/main/docs/rootfs-and-kernel-setup.md` |
| S6 | Virtio-vsock device | `https://raw.githubusercontent.com/firecracker-microvm/firecracker/main/docs/vsock.md` |
| S7 | Release notes, v1.17.0 (latest release; published 2026-09-10) | `https://api.github.com/repos/firecracker-microvm/firecracker/releases/tags/v1.17.0` |
| S8 | Docs directory listing (index of official docs) | `https://api.github.com/repos/firecracker-microvm/firecracker/contents/docs` |
| S9 | Firecracker ↔ Cloud Hypervisor context already recorded in Stage 10K | [INFRASTRUCTURE-OPTIONS-ANALYSIS](INFRASTRUCTURE-OPTIONS-ANALYSIS.md) §runtime comparison (its sources checked 2026-10-23) |

Known-from-source release facts: v1.17.0 is the current release (2026-09-10);
its notes add virtio device reset, guest-kernel v6.18 support, and `bzImage`
boot support, and fix (among others) a vsock RX-suppression bug after
pause/resume, an aarch64 jailer TOCTOU race, and three `virtio-mem` defects
(S7). The virtio-PCI init-sequence hardening fixing CVE-2026-5747 was
recorded against earlier release notes in S9 (checked 2026-10-23). Release
cadence and supported-release rules follow Firecracker's published
[release policy](https://raw.githubusercontent.com/firecracker-microvm/firecracker/main/docs/RELEASE_POLICY.md)
(referenced from S1/S4; not re-summarized here beyond what S4 states).

## 5. Host prerequisites (A)

- **Linux + KVM is a hard requirement** — Firecracker "runs on Linux hosts
  and with Linux guest OSs" and "relies on KVM and on the processor
  virtualization features for workload isolation" (S1, S2). `Verified`
  from S1/S2. Consequence: the current macOS laptop cannot run it
  natively; no source consulted establishes any `/dev/kvm` equivalent for
  a KVM Linux VMM under macOS's hypervisor model (`Unknown` whether one
  could exist — and moot, because a Mac-side experiment could not
  establish the production boundary anyway, below).
- **Supported host kernels** (S4): v5.10 (min. end of support 2024-01-31),
  v6.1 (2025-10-12), and v6.18 (min. end of support 2028-05-28) on 4K-page
  hosts; aarch64 is a supported target as well. Inference for sizing the
  future host image: a new host built today should target a kernel line
  inside the supported window (v6.18-class), because the two older lines
  are within or past the support window the policy describes once newer
  lines are added — the exact support-boundary reading for a specific
  candidate kernel on a specific host is a **WP-2/3 compatibility check**.
- **Hardware virtualization and `/dev/kvm`**: the jailer creates only
  `/dev/kvm` and `/dev/net/tun` device nodes inside the jail (S3), i.e.
  the VMM's OS-level needs in the jail reduce to those two nodes. The
  host itself must expose a working KVM; whether a given bare-metal SKU
  or server does, with which CPU flags and microcode state, is verified
  at host-administration time (`Unknown` until a host exists).
- **Host configuration dependencies** (S2, all `Verified` from the
  document): default-restrictive seccomp and no `--no-seccomp` in
  production; bounded handling of the 8250 serial console output
  (guest-writable; can drive unbounded host memory/storage, and can be
  re-enabled *from inside the guest* even if disabled at boot —
  `8250.nr_uarts=0` plus rate-limited/upper-bounded log consumption);
  swap disabled or a secure-swap configuration; microcode kept current;
  SMT and Kernel Samepage Merging disabled for tenant separation
  (side-channel posture); an "overwatcher" process that SIGKILLs
  unresponsive Firecracker processes (signal-handler deadlock is
  documented as possible, no security impact claimed); optionally reduced
  `kvm min_timer_period_us` and `kvm.nx_huge_pages` settings. These are
  host-composition items for the runbook, not package installs.
- **Why a Mac/Docker experiment cannot establish the boundary** (`Inferred`
  from `Verified` facts): the standing evidence ladder requires
  `per_run_microvm` for production-grade isolation claims; Docker on a
  laptop — whether Linux or via Docker Desktop's Linux VM on macOS — is
  `shared_kernel_container` by the definition in
  `capability.py`, and a level can never satisfy a stricter one. A
  container-from-container (nested KVM) setup would additionally weaken
  the boundary to VMM-inside-VM, already ranked weaker in
  [INFRASTRUCTURE-OPTIONS-ANALYSIS](INFRASTRUCTURE-OPTIONS-ANALYSIS.md).
  The AGENTS.md invariant states it directly: a Docker container on a
  laptop is not production isolation evidence.

## 6. Security-boundary analysis (C)

Per-control analysis. "FC provides" means documented product behavior
(vendor documentation); "must measure" means evidence FlakePilot must
collect on its own infrastructure before any `IsolationChecks` moves.

| Control | Firecracker provides (documented) | FlakePilot must implement | Must be independently measured |
|---|---|---|---|
| Process isolation per run | One Firecracker process = one microVM; "strongly recommended that each Firecracker process corresponds to a workload of a single tenant" (S2); all vCPU threads treated as malicious from start (S1) | Host-side launcher that pins one process per run; no reuse across runs | Process-per-run actually observed; no cross-run state (`Measured`, guest + host) |
| OS-level containment of the VMM | Jailer: chroot + mount namespace + `pivot_root`, privilege drop to per-run uid/gid, cgroups v1/v2, `setrlimit` (`fsize`, `no-file`), optional PID namespace and netns (S3). Jailer treats **all its inputs as trusted** — operator responsibility | Per-run uid/gid provisioning, cgroup setup, trusted-path hygiene for the launcher; cleanup after run (jailer cleanup is the user's job, with documented `notify_on_release` caveats — S3) | Escape-relevant properties: jail filesystem contents, cgroup presence/limits, uid actually dropped — measured guest/host, not assumed |
| CPU / memory limits | Per-API vCPU and memory config; cgroup CPU/memory controllers recommended (S1, S2) | Mapping of quota domain (WALL 5–120 s, MEMORY 64–2048 MiB, PIDS 8–256) onto API values + cgroups; refusal outside domain (exists in `plane.py` today as validation) | Guest-observed enforcement at chosen limits (WP-4) |
| Disk / I/O / network throughput | Token-bucket rate limiters per block device and network interface (S1); jailer `fsize`; serial/log upper-bounding guidance (S2) | Output-size and disk quota wiring on the host side; bounded artifact/log sinks | Disk, output size, and egress rate observed under load |
| Wall-clock / timed termination | No documented built-in run-time kill timer (not a documented feature — `Unknown`/absent from sources consulted) | Launcher enforces wall timeout and SIGKILL/cleanup path; overwatcher for unresponsive processes (S2 recommendation) | Crash/timeout/kill behavior; interrupted-cleanup recovery |
| Network egress restrictions | **Not provided** — "Firecracker does not perform any network traffic filtering"; egress must be filtered at host level; documented IMDS-block example rules for `nft`/`iptables-nft` (S1, S2) | Default-deny egress design: firewall rules or a no-egress network namespace per run; decide whether test workloads need any egress at all | Rule presence + actual blocked/stopped flows measured from guest |
| Metadata-service protection | MMDS exists and is fully operator-configured (S1); does not itself block access to *host* IMDS — the concrete block is the documented host firewall rule (S2) | Keep MMDS empty or unused initially; IMDS deny rule if the host platform has one | IMDS endpoint unreachable from guest |
| Credentials and secret isolation | Not provided by the VMM (nothing secret is provisioned by Firecracker — `Unknown`/absent from documentation whether any host creds leak; treat as must-not-leak) | Continue: no DB URL, session secret, GitHub App key, or artifact keys on the execution host (standing invariant); secrets never enter the guest; artifact sealing stays host-side and unwired until WP-5 | Credential-absence audit on the host; guest inability to reach control-plane endpoints |
| Filesystem / host mounts | No shared filesystem bridging by default (no virtio-fs — S9); guest storage via pre-built rootfs images; jail has no host paths beyond the two device nodes (S3) | Read-only rootfs + minimal writable scratch block per run; no host bind mounts at all | Mount table observed from guest |
| Tenant separation across runs | Per-process tenant isolation is the documented design boundary (S2 caveat) | Serial (not concurrent) multi-tenant runs initially on one host, or per-tenant host rules — an owner decision if concurrent | Cross-run contamination checks |
| Guest lifecycle/termination | `InstanceStart`/pause/resume; guest reset signaling; on-reboot shutdown semantics via boot args (S1, S4) | Launch/teardown state machine in the launcher; handling of guest-side hangs | Termination reliability incl. abnormal exits |
| Host patching / runtime updates | No self-update; operator applies releases per the release policy; host kernel updates are distribution work (S2); microcode updates recommended (S2) | Version-pinned runtime and images; advisory tracking; rebuild path | Post-update re-verification — reflected in section 9 |

Standing invariants that carry over unchanged (`Verified` policy, not
dependent on the VMM): the control plane never executes untrusted repository
code; guests receive no control-plane secrets, no host-ledger key, no broad
mounts, no privileged capabilities; a VMM choice does not by itself satisfy
any `IsolationChecks`.

## 7. Proposed future integration architecture — **not deployed** (D, B)

Everything in this section is a sketch for a future work package on a future
host. **None of it exists in the repository today**, and this document does
not authorize building it. The only permitted follow-on is more local design
documentation.

Component sketch (all proposed, none deployed):

1. **Control plane** (exists, unchanged): FastAPI app via _reproduction.py;
   issues a durable job; never talks to the host's runtime directly.
2. **Host-side launcher** (proposed, trusted side): the only component with
   host-administration authority on the dedicated host. It owns the runtime
   inventory (which Firecracker binary, kernel, rootfs images are present —
   reported by the host, never chosen by a control-plane frame), maps the
   job's quota tuple from the quota domain to Firecracker API values +
   jailer cgroups/rlimits, invokes `jailer` → Firecracker per run with a
   per-run uid/gid, applies host egress firewall rules, enforces wall-time
   termination plus overwatcher, and performs cleanup. It hands back only
   artifact keys and evidence, never credentials.
3. **MicroVM guest** (proposed): minimal Linux guest (supported kernel line,
   S4) with a minimized rootfs (S5 pattern); runs the test command; exports
   results over **virtio-vsock** — the natural guest↔host channel: guest
   `AF_VSOCK` ports map 1:1 to host AF_UNIX sockets, no guest networking
   needed; a guest-initiated connection to port *p* is forwarded to
   `v.sock_<p>` on the host (S6). Initial state: **no network interface at
   all** for test runs (default-deny egress by construction) — vsock still
   works with no NIC, since it is a separate virtio device.
4. **Artifact path** (proposed wire-up of existing stores): guest writes to
   a per-run scratch block device; the launcher copies bounded artifacts
   into `LocalFsArtifactStore`; later, sealing (existing unwired
   `AesGcmSealer`) under WP-5's key management once an owner decision
   exists.
5. **Evidence path** (exists as schema, proposed as flow): the collector
   (`ops/isolation/`) produces a v1 report whose mechanism-level entries
   must be guest-observed and unsimulated; review via `bundle.py`-style
   report review, which today truthfully returns `unavailable()` — that is
   correct as long as no evidence exists.

**Guest lifecycle sketch (B)**, per run: create jail directory with unique
id → copy exec file (S3 steps) → launch with per-run uid/gid → API configure
(machine config, boot source, rootfs, optional vsock, **no NIC**) →
`InstanceStart` → agent executes command under guest limits → results over
vsock → wall-time/orchestration termination → SIGKILL-on-stall via
overwatcher → scratch disk discarded → jail directory removed → evidence
emitted. Failure modes the launcher must own (S3-documented gaps):
jailer-level cleanup is the operator's job with documented release-race
caveats; the jailer/Firecracker PID separation note (killing the jailer does
not kill Firecracker without `--new-pid-ns`); the documented signal-handler
deadlock → overwatcher; the serial-console reactivation-from-guest caveat.
Crash, timeout, and interrupted cleanup are therefore **launcher design
requirements**, not Firecracker features — `Inferred`, from S2/S3 being
explicit that users own these.

## 8. Firecracker versus Cloud Hypervisor (main documented alternative)

Comparison only; no runtime is selected. Context rows draw on S1–S7 and the
Stage 10K runtime comparison (S9).

| Axis | Firecracker | Cloud Hypervisor |
|---|---|---|
| Isolation model | KVM microVM; one process per VM; jailer (chroot/namespaces/cgroups/seccomp/rlimits); documented single-tenant-per-process caveat (S1–S3) | KVM (or MSHV on some hosts) microVM; Rust VMM maintained by Microsoft/Cyberus/Tencent et al. (S9); fewer production-scale references cited in Stage 10K comparison |
| Host requirements | Linux + KVM; supported host kernel lines v5.10/v6.1/v6.18 (S4); host hardening list (S2) | Linux + KVM; comparable kernel-permissiveness per its docs (S9); no separate jailer binary — host containment must be provided by the operator or an external wrapper |
| Operational complexity | Moderate: jailer lifecycle, kernel/rootfs pipeline, egress firewall, overwatcher, cleanup (all above); smallest documented device surface (~5 virtio devices) | Moderate: fewer wrapper conveniences (no jailer equivalent), richer device surface to configure and constrain |
| FlakePilot integration implications | vsock channel maps cleanly onto the evidence/artifact flow; quotas map per-API + cgroups; limitations (no live migration, no hotplug, no virtio-fs) match FlakePilot's short-run, no-shared-storage shape (S1, S9) | Also virtio/vsock-capable; more devices to lock down per-run; the extra surface is review work, not an integration blocker |
| Main security trade-offs | Smallest attack surface of the compared VM options per Stage 10K survey; but no network filtering, no hardware-vulnerability mitigation, trusted jailer inputs — all operator-owned (S1, S2) | Larger device/feature surface (more code to review and constrain); mitigated historically by active upstream maintenance; fewer large-scale production isolations cited |
| Evidence required before selection | Same for both: on-host boot + quota + egress + cleanup measurements, guest-observed, per-run, via the v1 evidence schema; plus independent security review (WP-9) | Same provenance chain of custody; Cloud Hypervisor additionally needs whatever the host-compatibility check yields for its release line |
| Unknowns / unresolved risks (both) | Real host/kernel/CPU behavior (`Unknown` until a host exists); side-channel posture on the actual hardware (`Unknown`); no independent third-party security audit of either VMM was found in the sources consulted — vendor docs and production anecdotes (AWS Lambda/Fargate, Fly.io, Vercel, E2B for Firecracker; S9) are **not** independent proof | Same core unknowns apply |

`Inferred` bottom line: the two are closer in operational shape than the
Stage 10K register's preference suggests; Firecracker's margins are its
smaller surface, its included jailer, and the larger body of
production-scale isolation anecdotes. None of those margins short-circuits
the measurement/review path, so the recorded owner response (preferred
candidate) remains exactly right as a working posture.

## 9. Unresolved risks and evidence requirements (E, residual)

- **Compatibility is the blocking unknown.** Firecracker's fit to the
  eventual host (kernel line, KVM availability, CPU, microcode state,
  cgroup version present) cannot be checked without the host. Today this
  is `BLOCKED` by WP-2 (zero budget + residency `UNDECIDED` + no
  provider/operator), not merely deferred.
- **Single-host failure domain**: one dedicated host means the execution
  plane is unavailable during host failure/patching. FlakePilot's current
  posture already tolerates this (execution disabled = plane stays
  unavailable); no availability guarantee is claimed or needed yet
  (`Inferred`).
- **Host/runtime changes invalidate evidence**: the host kernel is part of
  the evidence fingerprint; a host or runtime update therefore requires
  re-collection of the affected measurements, per the runbook's
  fingerprinting model. Budget owner-attention for this recurring cost
  once a host exists (`Inferred` from the runbook contract).
- **Operational complexity for a small team** (S2/S3-documented duties):
  egress firewall maintenance, serial/log bounding, overwatcher, per-run
  cleanup, microcode/SMT/KSM host hygiene, release tracking (S7 shows
  recent fixes to memory-hotplug, vsock, and jailer code paths are still
  landing). This is a standing operational base, not a one-time setup
  (`Verified` duties, from the docs).
- **Key-management boundary**: unchanged by Firecracker. Artifact sealing
  stays host-side and unwired; the managed-KMS preference remains
  conditional on the future hosting ecosystem (D-KMS-1), and no vendor is
  selected.
- **Monitoring**: absent from the repository today (WP-8, blocked
  externally); Firecracker's own metrics/logs (named pipes, S1) would be
  additional *host-local* sources a future monitoring design must bound and
  consume — it does not substitute for the WP-8 control-plane monitoring.
- **No independent security evidence yet**: production
  deployments/usership and vendor documentation establish plausibility,
  not proof. WP-4 (measurement) and WP-9 (independent review) remain the
  required evidence path before any `IsolationChecks` changes.

## 10. Go/no-go — next investigation step only

**GO** for the next investigation step, which is local and no-cost:

- a pre-provisioning host-compatibility checklist (kernel line vs. S4, KVM
  presence checks, cgroup version, the S2 hardening items as a
  boot-strapping checklist), expressed as inputs WP-2 will need;
- a measurement plan written strictly in the vocabulary of
  [EXECUTION-HOST-RUNBOOK](EXECUTION-HOST-RUNBOOK.md) and the v1 evidence
  schema — what would be measured, from the guest, per control in §6;
- a threat-model walkthrough of the above against D-RUNTIME-2's questions.

**NO-GO** for everything spend-bearing or state-changing: provisioning,
installation (local or cloud), image building, credentials, wiring,
`IsolationChecks` changes, retention or encryption changes, or any claim
that a microVM boundary exists today. There is no Firecracker deployment,
and none is authorized.

## 11. Owner decisions still required

Unchanged from the decision register and sheet; this assessment does not
move any of them:

- D-HOST-1/2 remainder: provider, actual host, tenancy, named operator.
- **D-HOST-3 residency: `UNDECIDED` — continues to block provisioning.**
- **D-HOST-5 budget: zero — hard constraint; nothing spend-bearing is
  authorized.**
- D-RUNTIME-1 remainder: final runtime selection after host-compatibility
  checks, threat-model review (D-RUNTIME-2), measurements, and independent
  review (WP-9) — today's preference is preserved as *preferred candidate,
  pending host compatibility and security review*; it is not an approval.
- D-RUNTIME-5: naming the independent reviewer.
- D-KMS-1 remainder: hosting-ecosystem confirmation, vendor, custody rules.
- D-HOST-4 workload inputs, D-RET-2/3/4 (retention approval), D-GH-1/2/3
  (GitHub App), D-OPS-1/3 (monitoring/on-call): all still `UNDECIDED`.

## 12. Explicit no-infrastructure statement

**No infrastructure was installed, provisioned, deployed, purchased, or
configured.** No Firecracker binary, jailer, kernel image, or rootfs image
was built or downloaded; no cloud account, subscription, or credential was
created; the macOS development machine was not modified; no application
code, composition root, worker, `IsolationChecks`, retention behavior, or
sealing wiring was changed. Production untrusted-code execution remains
disabled, `ClosedSandbox`, `UnavailableExecutionPlane`,
`control_plane_reaper`, and `RefusedHostHandoff` remain exactly as they
were. No Stage 10L was started. Every component in §7 is proposed, not
deployed.

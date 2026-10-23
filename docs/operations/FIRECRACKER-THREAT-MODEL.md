# Firecracker threat-model walkthrough

Stage 10K (WP-1/WP-3 design slice). Code-grounded security analysis of the
proposed Firecracker execution architecture against malicious repository
code. Companion to [FIRECRACKER-FEASIBILITY-ASSESSMENT](FIRECRACKER-FEASIBILITY-ASSESSMENT.md)
(what the candidate is) — this document is *how it fails and what must be
proven*. Read-only, local, no-cost: nothing is installed, provisioned,
wired, or enabled, and no stage beyond 10K is started.

## Status vocabulary (the load-bearing rule)

- **Implemented** — verified in the working tree at HEAD `f602d70` (paths
  and lines cited). Possibly also unit-tested; unit tests are named.
- **Proposed** — a design on paper for a future host. It is **not** a
  FlakePilot capability, and no sentence below treats it as one.
- **Unknown** — not establishable today; named rather than guessed.
- **Tested** — a unit test exists for the behavior; a test is never network
  or isolation evidence. The unit suite does not test a microVM, because
  none exists.

Trust assumptions follow [EXECUTION-HOST](EXECUTION-HOST.md): the control
plane (database URL, session secret, GitHub App private key) is not a safe
place for repository code; the guest, its repository, tests, and
dependencies are untrusted; a shared-kernel container is not a kernel
boundary. The seven evidence fields and their 21 controls
(`FIELD_TO_CONTROLS`, `ops/isolation/evidence_report.py:60`) are the target
measurement grid referenced in every table row.

## 0. Baseline: what runs today

`ClosedSandbox.run` returns `("closed", "execution_closed")` unconditionally
(`integrations/infrastructure/sandbox.py:93–110`), and the composition root
constructs only `ClosedSandbox` (`reproduction.py:383–392`, "does not start
a container"). The plane refuses every handoff (`UnavailableExecutionPlane`,
`execution/application/plane.py:37–43`) and the worker reclaims nothing
(`control_plane_reaper`, `plane.py:92–94`). `UnavailableIsolation.current()`
reports every check false (`execution/application/reproduction.py:88–92`).
`ContainerSandbox` exists **only as a unit-test argument builder**
("The worker does not construct it", `sandbox.py:4–15`).

Consequence for this model: today there is no guest, so no guest-originated
attack path exists. Every threat row below is therefore partly prophylactic
(keep it that way) and partly design input for the future host. The one
live attack surface today is the control plane itself — HTTP routes,
 durable-job payloads, artifact reads — which the last rows cover.

## 1. Threat table

Attacker capability in every row: repository code executing inside the
guest of a future Firecracker host (that is the trust premise). Where a row
concerns some other actor, the capability is stated in the row.

### T1 · Guest escape and hypervisor vulnerabilities

| Aspect | Analysis |
|---|---|
| Path | Guest code exploits a VMM, virtio, or KVM defect or an overlooked emulated device to execute on the host |
| Implemented (code) | Nothing runs, so no live path (`ClosedSandbox`); the evidence schema refuses any escape-relevant claim unless guest-observed; unit tests: `tests/unit/execution/test_plane.py` |
| Proposed | Minimal device surface (no network device in v1 runs — no NIC to attack), default-restrictive Firecracker seccomp, jailer (chroot/mount-ns/privilege-drop/cgroups/rlimits), supported host/guest kernel lines only, advisory tracking; **but** vendor docs are not a security proof — the feasibility assessment records that no independent third-party audit of Firecracker was found |
| Required evidence | Escape-focused and behavior measurements at `per_run_microvm`, collected via the v1 collector; independent security review (WP-9) — neither has happened |
| Residual risk | Firecracker/KVM 0-day, host-kernel 0-day, CPU side channels (Firecracker docs: it "is not able to mitigate host's hardware vulnerabilities"). **Blocks production** until measured, reviewed evidence exists |

### T2 · Guest-to-control-plane access

| Aspect | Analysis |
|---|---|
| Path | Guest code scans/reaches control-plane endpoints (API, database, outbox) |
| Implemented (code) | The `PlaneRequest` field set is the *entire* handoff contract — no secrets, no URLs, no tokens (`execution/domain/plane.py:18–30`); the production plane object returns `unavailable` and opens no socket; worker holds control-plane secrets and intentionally does not open the host ledger (`plane.py:92–94`) |
| Proposed | No network device at all on v1 runs (vsock lives on a separate virtio device and needs none); host firewall denies control-plane/range addresses as defense-in-depth; MMDS left unconfigured |
| Required evidence | `control_plane_endpoint_denied` (and `metadata_endpoint_denied`, T4) observed from the guest |
| Residual risk | Misconfiguration at launch time (someone adds a NIC "temporarily"). **Blocks production**; the no-NIC default must itself be a measured property, not an assumption |

### T3 · Host filesystem and credential exposure

| Aspect | Analysis |
|---|---|
| Path | Guest reads host paths via a mount, an injected device, or a credential-bearing environment |
| Implemented (code) | No production process creates any of this; the contract of the artifact store proves the traversal discipline is in place *for artifact keys* (validated owner/org/repo/digest parts, resolved path forced under the root, `artifact_store.py:24–28`); unit tests: `test_artifact_store.py` (13 tests) |
| Proposed | Jailer jail contains exactly `/dev/kvm` and `/dev/net/tun` (vendor-documented); per-run uid/gid; read-only rootfs + minimal per-run scratch; **no bind mounts of any host path**; host must not receive database URL, session secret, or GitHub App private key (EXECUTION-HOST deployment spec) |
| Required evidence | `host_mounts_denied` + `filesystem_scoped`, and all four `credentials_absent` sub-controls (`database_url_absent`, `session_secret_absent`, `github_app_key_absent`, `host_ledger_key_absent`) measured from the guest |
| Residual risk | The jailer treats **all its inputs as trusted** (vendor docs) — a host-side bug in the launcher is the exposure class, not the guest. **Blocks production** |

### T4 · Network egress, cloud metadata, and DNS

| Aspect | Analysis |
|---|---|
| Path | Guest exfiltrates code/data, probes the host's private network, or reads the cloud metadata service |
| Implemented (code) | `--network=none` exists **only** inside the unit-test `ContainerSandbox` argument builder, whose own docstring states it "is an argument, not a measured block" (`sandbox.py:10–11`). Production implements **no** egress control — there is nothing to restrict |
| Proposed | v1 design: no NIC (no egress, no DNS question at all); if a NIC is ever required, host `nft`/`iptables` default-deny plus the vendor-documented metadata-block rule (`tap*` → `169.254.169.254` drop) |
| Required evidence | `network_egress_denied` + `metadata_endpoint_denied`, guest-observed, including DNS |
| Residual risk | DNS as exfiltration side-channel if egress is ever allowed; metadata lts reachability on the eventual hosting platform is **Unknown** (no provider chosen). **Blocks production** |

### T5 · Resource exhaustion and quota bypass

| Aspect | Analysis |
|---|---|
| Path | Guest (or a malicious job request) exceeds CPU/memory/PID/disk/output/wall limits, or submits values outside the domain |
| Implemented (code) | Request-side validation is real: `plane_request` bounds WALL 5–120 s, MEMORY 64–2048 MiB, PIDS 8–256 and rejects non-ints/bools (unit tests: `test_plane.py`) — but the code states plainly it "is not host enforcement" (`domain/plane.py:13`). Archive DoS controls implemented and tested: size/file-count/uncompressed caps, symlink/absolute/`..` rejection, declared-vs-actual size check (`sandbox.py:262–304`). Output bounds: 8 KiB result, 1 MiB PNG with magic-byte check. Worker lease = wall+30 s with generation-checked finish |
| Proposed | Firecracker API vcpu/memory values + jailer cgroups (cpu, memory, blkio) + rlimits (`fsize`, `no-file`) + per-device rate limiters + bounded log/serial sinks |
| Required evidence | All six `limits_enforced` sub-controls (`cpu_limit` … `wall_clock_limit`) observed **outside the guest** |
| Residual risk | **Disk quota is not in the request domain at all today** (no field); CPU count is not in the domain either (the unit-test adapter hardcodes `--cpus=1`). Both must be handled host-side or added as design changes (WP-3). **Blocks production** |

### T6 · Guest crashes, hangs, timeouts, and interrupted cleanup

| Aspect | Analysis |
|---|---|
| Path | Run wedges, process dies mid-run, cleanup is interrupted between steps, or a stale worker resurrects state |
| Implemented (code) | Cancel is durable and wins: `_run_watched` polls `cancel_requested` while the runtime client runs, cancel-committed overrides a finishing run, and `finish` is generation-checked so a stale lease cannot rewrite state (unit tests: `test_recovery.py`, 10 tests). Leftover workdirs reaped by TTL (`reap_workdirs`, symlink-safe, symlink-aware) — but it removes **directories only and invokes no runtime**, by design. The production reaper honestly reclaims zero (`ReapResult(0, "runtime_reaper_unavailable")`) |
| Proposed | Launcher-owned kill path sized per Firecracker docs' caveats: an overwatcher that SIGKILLs unresponsive VMM processes (the vendor documents a possible deadlock in its own signal handlers), `--new-pid-ns` (killing the jailer does not otherwise kill Firecracker), unique per-run jail dirs (`notify_on_release` cleanup has documented races — the vendor puts cleanup on the operator), teardown state machine discarding per-run scratch |
| Required evidence | All four `cleanup_verified` sub-controls (`host_cleanup`, `artifact_isolation`, `image_provenance`, `audit_recovery`) |
| Residual risk | Cleanup correctness under abnormal termination is the biggest unmeasured lifecycle area. **Blocks production** |

### T7 · Artifact tampering and plaintext-at-rest exposure

| Aspect | Analysis |
|---|---|
| Path | Guest or attacker data gets persisted, mutated, or read cross-tenant; bytes sit plaintext on disk |
| Implemented (code) | Store is content-addressed and append-immutable: write-to-temp + hard-link that never replaces, read-only files, 0700 dirs, root must be owner-only (mode check enforced at startup); SHA-256 verified on every read and on collision; reads re-validate key shape and org match — "another org is not found" (`reproduction.py:519–549`); the run stores a **canonical minimal result** it generates itself (selector + status), never the sandbox's own JSON, and screenshots pass a magic-byte/size gate (`reproduction.py`, `_evidence`). Unit tests: `test_artifact_store.py` |
| Proposed | Sealing via the existing-but-unwired `AesGcmSealer` + a key manager (D-KMS-1 conditional preference; no vendor; WP-5) |
| Required evidence | `artifact_isolation`; sealing round-trip on real keys (WP-5) once authorized |
| Residual risk | **Plaintext at rest is a known, documented gap** (`artifact_store.py` header) and retention currently deletes nothing — artifacts persist indefinitely until the retention decision (D-RET-2/3/4) exists. **Blocks production** per the readiness assessment |

### T8 · Stale evidence after host, kernel, or runtime updates

| Aspect | Analysis |
|---|---|
| Path | Real measurements taken once are presented as current after the host/kernel/runtime changed |
| Implemented (code) | Evidence reports carry host identity, OS, hypervisor, image digest, and mechanism; `bundle.review` truthfully returns `unavailable()` — nothing reviews bundles because nothing collects them from a real host; the staleness rule is encoded in the runbook: a bundle is stale when host, digest, or measured configuration changes |
| Proposed | Version-pinned runtime + images; a written re-collection trigger on any host/kernel/runtime/system update (operational, WP-3/WP-8 slice) |
| Required evidence | Mechanism-level reports whose fingerprint matches the *current* host — verified at review time, not collection time (review path must eventually make that comparison; **Unknown** which component owns it post-hoc) |
| Residual risk | Process-dependent (human vigilance). **Blocks production** — evidence without fingerprints is not evidence |

### T9 · Malicious or compromised host operators

| Aspect | Analysis |
|---|---|
| Path | The named host operator (root on the execution host) or a compromised operator session reads/alters runs, artifacts, or evidence |
| Implemented (code) | Trust model is explicit that the operator is trusted-side (ADR-008); what technical containment exists runs the other way: the control plane's reaper and handoff refuse host-side claims (`RefusedHostHandoff` accepts *no* id, writes nothing), and HMAC possession alone is never permission — no frame can choose a host path, program, or container id (runbook). Control-plane audit records exist for executions |
| Proposed | Audit trail that supports incident recovery (`audit_recovery` control); host-side actions logged and reconcilable; single named operator (D-HOST-2) with the least privilege the host allows |
| Required evidence | `audit_recovery` measurement; operator procedure review |
| Residual risk | A root operator is largely **outside** the technical boundary — a host root can read guest memory and artifacts. This is an accepted trust boundary (the owner decides who holds it), not an implementable fix; mitigations are procedural. Does not soften any `IsolationChecks`; **blocks production** in the sense that D-HOST-2 (named operator) must exist first |

### T10 · GitHub App credential and token exposure

| Aspect | Analysis |
|---|---|
| Path | Installation token or App private key reaches the guest, the host, logs, or a result payload |
| Implemented (code) | Minting is control-plane-only and scope-minimized: installation tokens are `contents:read` for one repository, held in the minter object until expiry (`integrations/infrastructure/installation.py:3,78`); the composition root substitutes `GitHubCodeHost(None)` whenever credentials are absent or invalid (`reproduction.py:390–403`) — a static contents bearer is not used. The host contract forbids GitHub App keys and tokens on the host; **live minting is NOT VERIFIED** (AGENTS.md — say so, nothing claims it). Leakage scanning exists in the evidence collector: `_SECRET_PATTERNS` includes `ghs_`, `ghp_`, `github_pat_` (`ops/isolation/evidence_report.py:110–117`) |
| Proposed | Never pass tokens into the guest or the host channel; the guest receives only the archive and the selector |
| Required evidence | `github_app_key_absent` guest-observed; token-pattern scan of persisted artifacts/results |
| Residual risk | Control-plane key compromise (KMS/content-encryption is a separate, unwired decision); token blast radius is already limited by scope. **Blocks production** only insofar as any credential path must be measured before guests run |

## 2. Five most consequential gaps (ranked)

1. **Host-side enforcement does not exist — quotas are validated, not
   enforced.** WALL/MEMORY/PIDS bounds are request validation only
   (`domain/plane.py:13`: *"not host enforcement"*); enforcement would live
   entirely in a launcher/jailer that is a sketch. Disk and CPU limits are
   **absent from the request domain entirely**. Smallest safe next step
   (local, no-cost): extend the WP-3 design slice with the launcher's
   quota-mapping and kill-path specification, including the missing
   disk/CPU fields as an explicit design change request.
2. **No measured network/egress control anywhere.** The only `--network=none`
   in the tree is a unit-test argument, labeled by the code itself as
   unmeasured. The no-NIC + vsock design removes the question for v1 but
   has never been tried. Smallest safe step: a written egress/metadata
   measurement plan (what the guest observation for `network_denied` and
   `metadata_denied` will look like, and the host firewall rules that must
   pre-exist), added to the feasibility assessment's planned measurement
   plan.
3. **Plaintext artifacts persist indefinitely.** Documented in the store's
   own header; sealing is unwired and blocked on the KMS ecosystem
   condition, deletion on the retention approval (D-RET-2/3/4). Smallest
   safe step: none beyond what is recorded — this is owner-blocked, not
   engineering-blocked; keep the recommendation honest in the decision
   sheet (already done).
4. **Cleanup under abnormal termination is unmeasured and partially
   unimplemented by policy.** `reap_workdirs` removes only directories; the
   production reaper reclaims zero; jailer-level cleanup races are
   documented by the vendor. Smallest safe step: a teardown state-machine
   design (normal / SIGKILL / host-reboot paths) for the WP-3 slice,
   including the overwatcher and PID-namespace requirements from the
   vendor docs.
5. **No independent security validation exists at any level.** Vendor docs
   and production anecdotes are not independent evidence; the reviewer
   (D-RUNTIME-5) is unnamed; the review contract (what WP-9 reviews) is
   unwritten. Smallest safe step: a reviewer briefing package — this threat
   model, the feasibility assessment, the runbook contract, and the control
   grid — as the concrete artifact a reviewer would review once one exists.

Shared property of all five: every one of them can be advanced **only as
local design/planning documentation** under the current constraints; none
can be closed technically without the blocked host (zero budget, residency
`UNDECIDED`, no provider/operator).

## 3. Explicit boundaries of this analysis

- No proposed Firecracker control above (jailer, seccomp, rate limiters,
  overwatcher, no-NIC, firewall rules) is a FlakePilot capability. Every
  capability table cites the code file where an implemented mitigation
  lives, and only there.
- "Tested" means unit-level behavior coverage (`test_plane.py`,
  `test_sandbox.py`, `test_artifact_store.py`, `test_recovery.py`,
  `test_execution_host.py`). No test has ever executed a microVM, a
  container, or untrusted repository code, and none was run for this
  document (docs-only change).
- The laptop logs cited by the runbook remain `shared_kernel_container`
  measurements that satisfy no stronger level.
- Production untrusted-code execution remains disabled; every safeguard
  inspected (`ClosedSandbox`, `UnavailableExecutionPlane`,
  `control_plane_reaper`, `RefusedHostHandoff`, `UnavailableIsolation`,
  plaintext-store policy, retention disabled) is unchanged by this work.
- This document is analysis, not authorization: it creates no permission
  to build the host, run a laptop experiment on untrusted code, or change
  any `IsolationChecks`.

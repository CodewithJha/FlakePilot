# Production-readiness assessment

Stage 10J. This is an assessment of the current repository at `2b07d0d0`
against what running untrusted repository code in production would require.
It is documentation, not a change of posture: untrusted execution stays
disabled, `ClosedSandbox` stays in place, and the evidence tooling is not
production authorization. The companion dependency plan is
[PRODUCTION-READINESS-ROADMAP](PRODUCTION-READINESS-ROADMAP.md).

Status vocabulary. `PASS`: directly supported by current code, reproducible
tests, or recorded operational evidence at the required level. `FAIL`: the
requirement is not met, with the gap visible. `BLOCKED`: requires
infrastructure, credentials, or an external decision that does not exist yet.
`UNKNOWN`: not investigated to a conclusion. `NOT_APPLICABLE`: the area has
no production bearing at this stage. A `PASS` never means production-ready
by itself; the go/no-go section at the end is the only overall judgment, and
it is NO-GO.

Every finding cites code or records. CI workflow runs verified for
`2b07d0d0`: run `39911861196`, success. Test suite at that commit: 686
passed with PostgreSQL (594 unit + 92 integration).

## 1. Execution isolation and trust boundaries

**Status: BLOCKED** (software posture verified; the boundary itself does not
exist).

- Verified: `interface/composition_root/reproduction.py` `_sandbox` returns
  `ClosedSandbox()` unconditionally; the control plane cannot start a
  container. `execution/application/plane.py` carries `UnavailableExecutionPlane`
  and `control_plane_reaper`; the worker (`composition_root/worker.py`) uses
  `control_plane_reaper()` and `RefusedHostHandoff` — no host handoff, no
  ledger key. `execution/application/reproduction.py` constructs
  `UnavailableIsolation`, the only production attestation, all-false.
- Verified: tests `test_worker.py`, `test_evidence.py`, `test_capability.py`
  hold this fail-closed behavior.
- Missing: a dedicated host, a per-run boundary with a separate kernel, and
  measured enforcement. Nothing in the repository provides them.
- Risk if unresolved: any accidental wiring of a host adapter would put a
  weaker runtime between the control plane and untrusted code.
- To close: roadmap items B1 and B2, then B4 evidence and review.

## 2. Dedicated execution-host availability

**Status: BLOCKED.**

- Verified: no host exists. `docs/operations/EXECUTION-HOST.md` and
  `EXECUTION-HOST-RUNBOOK.md` document prerequisites and go/no-go gates but
  explicitly record that nothing was provisioned or deployed.
- Verified: candidate evidence exists only from non-eligible environments —
  the developer laptop (Docker Desktop, shared LinuxKit VM) and one
  GitHub-hosted runner probe (`docs/operations/isolation-validation/`,
  October 2026 entries).
- Risk: without a host, every downstream control (egress, mounts, limits,
  cleanup) cannot be measured at any meaningful level.
- To close: a provisioning decision and spend approval by the repository
  owner (roadmap B1). No provider is recorded yet — that is a decision, not
  an assumption.

## 3. Per-run microVM or equivalent isolation runtime

**Status: BLOCKED.**

- Verified: `ops/isolation/run.sh` and `local_mac_driver.py` produce
  `shared_kernel_container`-level evidence only. The domain ladder
  (`execution/domain/capability.py`) places that level below
  `per_run_microvm`, and `attest` ignores weaker-level records.
- Verified: the runbook (§6 evidence ladder) forbids representing Docker
  Desktop's shared LinuxKit VM as a microVM.
- Missing: no runtime (Firecracker, Cloud Hypervisor, QEMU, or other) has
  been selected, tested, or recorded.
- Risk: a shared-kernel container remains exploitable by kernel escape
  classes that a microVM boundary would absorb.
- To close: select and approve a runtime (owner decision), then measure
  on the dedicated host.

## 4. Network egress denial and metadata protection

**Status: BLOCKED** (measurement tooling verified; no compliant network).

- Verified: the evidence report derives `network_egress_denied`,
  `metadata_endpoint_denied`, and `control_plane_endpoint_denied` only from
  guest-observed connect results (`ops/isolation/evidence_report.py`
  `_network`, `_metadata`, `_control_plane`); host-side observation cannot
  produce PASS.
- Verified: measured inside `--network=none` containers on the laptop and
  the hosted runner (`run.sh`, `probe.py`), `blocked errno=` observed.
- Missing: no host firewall policy exists to verify, no dedicated-host
  measurement has been made, and metadata endpoints for the eventual
  provider are unknown.
- Risk: open guest egress would expose the runtime, GitHub installations,
  and junk-boundary services to untrusted code.
- To close: dedicated host with default-deny egress, then guest-observed
  probes per runbook §6–§7.

## 5. Host mounts, credentials, privilege separation, resource limits

**Status: BLOCKED.**

- Verified software side: control-plane quota validation (wall 5–120 s,
  memory 64–2048 MiB, pids 8–256) exists as request validation, and the
  evidence report requires all six limits measured before
  `limits_enforced` becomes PASS (the six limit rules in
  `ops/isolation/evidence_report.py`); a missing result stays
  NOT_MEASURED — verified by test `test_missing_measurements_fail_closed`.
- Verified: credential-absence evidence uses a name census plus token-prefix
  value scan that stores counts and booleans only, never names or values —
  verified by `test_environment_names_are_counted_and_never_reported`; the
  modules contain no `os.environ` access — verified by
  `test_the_probes_never_receive_environment_knowledge`.
- Verified measured (shared-kernel, laptop): disk cap ENOSPC before 2 MiB,
  CPU ratio 0.5, memory killed by cgroup OOM (exit 137), 14 concurrent pids,
  output and wall caps, sockets absent, tenant files unreachable —
  `isolation-validation/2026-10-22-laptop-measured.md`. These do not
  transfer to a dedicated host.
- Risk: without host-side enforcement and guest-observed proof, any limit
  can be silently exceeded by untrusted code.
- To close: roadmap B3 (limits enforced outside the guest) and B4
  (guest-observed evidence).

## 6. Guest lifecycle, cleanup, tenant separation

**Status: BLOCKED** (logic verified in simulation; real-host behavior pending).

- Verified: ledger state machine distinguishes cancel from completion
  (`FileLedger` with phase marks), refuses a second terminal mark, treats
  expiry via host clock, and selects nothing on authority unavailability —
  `execution/infrastructure/ledger.py` and `test_file_ledger.py`; commits
  `f688273` (host restart, rotation, concurrent terminal marks) and `86ece04`
  (cancel and completion as distinct marks) hold in simulation.
- Verified measured (laptop): orphan cleanup after launcher SIGKILL and a
  second sweep removing nothing new — same validation entry.
- Missing: cleanup on a dedicated host across reboot, and multi-tenant
  artifacts on shared infrastructure (artifact isolation is only enforced in
  the application read path via org comparison —
  `composition_root/reproduction.py:read_execution_artifact`).
- Risk: leaked containers/images/disks grow into resource exhaustion or
  cross-tenant reads.
- To close: roadmap B3-B4; reboot recovery is an explicit runbook checklist
  item still unchecked.

## 7. Evidence integrity, provenance, review, revocation

**Status: PASS as tooling; BLOCKED as production review.**

- Verified: schema-version-1 reports require an identity fingerprint that
  changes with host/kernel/runtime/digest (`identity_fingerprint`,
  `is_current`), a recomputation check that prevents tampered check tables
  (`validate_report`, test `test_tampered_checks_are_refused`), a bytes hash
  (`report_sha256`, test `test_the_hash_follows_the_bytes`), secret-shaped
  values rejected (test), oversized input rejected (test), simulated
  mechanisms unable to pass any check (test), and a mechanism→level map that
  no caller argument or flag can override (tests). No mechanism constructs
  `production_approved`.
- Verified: the domain reviewer refuses every bundle —
  `execution/domain/bundle.py:review` returns `unavailable()` unconditionally.
- Missing: an independent human reviewer and review records for a real host.
  The tool's hash proves integrity of bytes, not truth of claims.
- Risk if unresolved: well-formed tooling warps into a rubber stamp without
  a reviewer.
- To close: roadmap B4/B9 — review is a named-person obligation, not code.

## 8. Artifact encryption and external key management

**Status: BLOCKED.**

- Verified: production constructs only `LocalFsArtifactStore`
  (`composition_root/reproduction.py` `_artifacts`); no sealer is wired;
  `UnconfiguredSealer` and `UnavailableKeyProvider` fail closed
  (`integrations/application/sealing.py`); `AesGcmSealer` (v1 blob format,
  versioned key ids) is implemented and unit-tested, including tamper and
  rotation (`integrations/infrastructure/aes_gcm.py`).
- Verified: `integrations/infrastructure/sealed_store.py` exists but the
  composition root does not construct it; local files stay plaintext, and
  `UnconfiguredSealer` fails closed if anything ever tried to seal.
- Missing: a real external key manager outside the guest and worker, tested
  application wiring, and deployed ciphertext round-trips. Storing plaintext
  in `FLAKEPILOT_ARTIFACT_DIR` continues.
- Risk: test reports (org code, pull-request paths, internal names) sit
  unencrypted on disk; a host compromise reads them plainly.
- To close: roadmap B5. Vendor selection is an owner decision; the wiring
  and tests are local work.

## 9. Retention and audit requirements

**Status: BLOCKED** (fail-closed correct; policy absent).

- Verified: `integrations/application/retention.py` accepts only
  `policy="disabled"`, selects no deletion targets, separates artifact keys
  from audit and outbox keys, raises `retention_unconfigured` for any other
  policy; audit package never updates or deletes rows (`audit/__init__.py`).
- Missing: an approved retention duration, a tested idempotent/restart-safe
  deletion implementation, and tenant-scoped selection.
- Risk: unbounded artifact growth on the first real host; conversely, an
  improvised policy could destroy audit evidence.
- To close: roadmap B6. The duration is a product/legal/security approval,
  not a code decision.

## 10. GitHub App authentication and least privilege

**Status: FAIL** (server-side design; no live proof).

- Verified: installation tokens are minted as `contents:read` for one
  repository and are never placed in the guest path
  (`integrations/infrastructure/installation.py`); the composition root
  uses the installation minter or `GitHubCodeHost(None)` — statically no
  bearer token.
- Verified: sign-in flow is server-side with org-bound grants (ADR-011).
- Missing: no App ID/private key exists, so live minting has never been
  performed. Sharing, refresh, revocation, and expiry-handling against the
  real GitHub API remain untested.
- Risk: shipping the feature without a live test risks shipping incorrect
  scope handling or storage of long-lived state.
- To close: roadmap B7 — provisioning credentials is an owner action; the
  exhaustive live test is local once they exist.

## 11. Worker lifecycle, retries, failure handling, recovery

**Status: NOT_APPLICABLE for untrusted execution** (the worker exists for
jobs/outbox) — with genuine gaps.

- Verified: the worker is a second process, claims leases, finishes a
  claimed batch on SIGINT/SIGTERM, and uses `control_plane_reaper()`
  (`composition_root/worker.py`, DURABLE-JOBS.md); outbox deliver includes
  retry class ordering (ADR-012).
- Missing: no retention or stage job is registered (README Status); a
  production execution worker — lease across host restarts, backoff,
  dead-lettering — does not exist yet because the plane does not exist.
- Risk: moderate while execution is disabled; critical the moment a host
  lands.
- To close: design the production worker as part of roadmap B3 with the
  first dedicated host.

## 12. Monitoring, incident response, operational ownership, rollback

**Status: FAIL.**

- Verified: no monitoring/alerting sink exists in the repository (no
  metrics, alerts, or on-call documentation). The runbook lists alerting
  requirements (authority unavailable, unreadable ledger, repeated rejected
  macs, cleanup that cannot run) as proposed only.
- Verified: safe-incident behavior is specified (stop `register`, keep
  authenticated cancel/complete, cleanup stopped, audit/outbox retained) in
  the runbook §13 — as a plan, not a deployment.
- Missing: dashboards, log aggregation, a named operator, and a rollback
  story for the whole deployment. "TLS and request limits are your job"
  (README) currently extends to all of production.
- Risk: failures and intrusions go unseen; recovery decisions happen
  ad hoc under pressure.
- To close: roadmap B8 — an operational decision and actual infrastructure,
  neither of which exists in the repository.

## 13. Test coverage, CI, dependency security, reproducibility

**Status: PASS** (as development infrastructure).

- Verified: 686 tests pass locally against PostgreSQL (594 unit + 92
  integration creating throwaway databases); CI run `39911861196` success
  for `2b07d0d0` covering pytest with a PostgreSQL service, ruff format +
  check, strict mypy (201 files), 21 import-linter contracts, locked deps
  (`uv.lock --check`), dev and runtime licence gates, and migration model
  checks.
- Verified: the harness `/tests/unit/execution/test_isolation_harness.py`
  pins image digests and asserts secrets and credentials never reach the
  probe workflow.
- Limitations: coverage measures software, not hosts — a green suite proves
  nothing about isolation (stated in AGENTS.md and here as a standing
  rule). Runtime licence review for shipped images is separate (ADR-007).
- To close: keep gates; no new dependency needed for 10J.

## 14. Documentation and operational readiness

**Status: PASS** for a pre-production project; FAIL for operating a
production service.

- Verified: the documentation set is honest — README lists known gaps; ADR
  001–014; EXECUTION, EXECUTION-HOST, EXECUTION-HOST-RUNBOOK, DURABLE-JOBS;
  isolation-validation records carry measured values with commit, run id,
  and scope; AGENTS.md fixes workflow rules; the two stage-10I/10J
  documents map requirements to owners.
- Limitations: runbooks describe procedures that have not run; API docs
  cover ingestion only; IAM administration is a bootstrap command.

## Overall go/no-go

**NO-GO for any production execution of untrusted repository code.**
Unresolved critical blockers: no dedicated host, no per-run microVM or
equivalent boundary, no independently reviewed isolation evidence, no
external key manager, no approved retention policy, no live GitHub App
test, no monitoring or operation ownership. Any single one of these is
disqualifying by the invariant that a missing control cannot be annotated
away.

Per-use decisions:

- **Local development and testing: GO.** The API, ingestion, classification,
  IAM, audit, and diagnostics run with PostgreSQL; 686 tests; documented
  setup. Untrusted execution stays closed.
- **Shared-kernel container experimentation: GO with recorded limits.**
  Only through the existing probes (`ops/isolation/run.sh`,
  `local_mac_probe.sh`), labelled as `shared_kernel_container` evidence that
  never satisfies higher levels, and never on the control plane.
- **Dedicated-host experimentation: NO-GO until B1.** No host, no approval,
  no evidence chain; the runbook exists but has not executed once.
- **Production execution of untrusted repository code: NO-GO without
  reservation.** All roadmap B-gates and the independent review are
  outstanding; passing tests and the evidence tooling do not establish
  production isolation.

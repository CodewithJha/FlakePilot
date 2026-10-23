# Execution-host runbook

Stage 10I. This page is the prepared procedure for dedicating a machine to
FlakePilot execution. It is a plan for an operator, not an accomplishment: no
host was provisioned or deployed when this page was written, untrusted
execution stays disabled, and `IsolationChecks` stay false. Every item below
is labelled **PROPOSED** (a requirement the operator must satisfy and verify),
**MEASURED** (an observation recorded in
[isolation-validation](isolation-validation/)), or **DEPLOYED** (present and
verified in the deployed system). Today only the control-plane side and the
simulation exist, so almost nothing is DEPLOYED.

The binding decisions remain [ADR-004](../adr/ADR-004-control-plane-and-execution-plane.md)
and [ADR-008](../adr/ADR-008-deployment-and-trust-model.md). The status of the
execution plane is [EXECUTION](EXECUTION.md); the standing contract for the
host is [EXECUTION-HOST](EXECUTION-HOST.md).

## 1. Purpose, scope, non-goals

**Purpose.** Provide a safe, reproducible sequence to (a) dedicate a machine
to FlakePilot execution, (b) measure the isolation controls the product
requires, (c) record the measurements as evidence, and (d) submit the
evidence for review. Execution stays disabled until review approves.

**In scope.** Host selection criteria, provisioning prerequisites, network
and metadata denial verification, credential isolation, resource-limit and
cleanup proofs, evidence collection with the tools in `ops/isolation/`,
review and go/no-go.

**Non-goals.** This runbook does not provision anything, does not enable
production untrusted-code execution, does not replace `ClosedSandbox`,
`UnavailableExecutionPlane`, `control_plane_reaper`, or `RefusedHostHandoff`,
does not change retention, artifact encryption, or GitHub App configuration,
and does not select a cloud provider or operating environment. The choice of
provider, host size, and runtime is a separate, later decision.

## 2. Trust boundaries

**PROPOSED.** The control plane holds the database URL, the session secret,
and any GitHub App private key. It never runs repository code. The execution
host holds the host ledger key and the runtime control socket and nothing
from the control-plane list. The guest holds none of: host ledger key,
runtime socket, control-plane secrets, artifact keys, broad host mounts,
privileged capabilities.

**DEPLOYED (control-plane side).** The composition root returns
`ClosedSandbox`, the worker uses `control_plane_reaper()` and refuses host
handoff, and the only production isolation attestation is
`UnavailableIsolation` — every check false, with no environment-variable
override. Isolation values surface only through the domain model:
`IsolationChecks` has exactly seven boolean fields (`network_denied`,
`metadata_denied`, `host_mounts_denied`, `credentials_absent`, `unprivileged`,
`limits_enforced`, `cleanup_verified`), and eligibility (`evidence.accept`)
stays ineligible when any of them is false.

## 3. Host eligibility and minimum requirements

**PROPOSED.**

1. The host is a separate machine from the control plane, not a shared
   laptop, and not a CI runner. A laptop running Docker Desktop was measured
   and is not eligible for anything above `shared_kernel_container`: its
   containers share a LinuxKit kernel inside a shared VM.
2. The host can be rebuilt from a definition, its OS and kernel versions are
   recorded, and a change to host, kernel, runtime, image, or policy
   invalidates collected evidence and requires re-measurement.
3. Administrative access is personal to one named operator, SSH key based,
   root only through a recorded sudo policy that is never shared, and
   audited.
4. The host clock is NTP-synchronized, because the ledger expires on the host
   clock.

**MEASURED (laptop, not-eligible observations, proceed with caution).**
`isolation-validation/2026-10-22-laptop-measured.md` — disk cap 67108864
bytes, short write stopped at the cap, CPU ratio 0.500, memory killed with
exit 137, pid count 14, and a second cleanup pass removed nothing new. That
day's earlier run left four FAIL lines (no disk result, no pid count, OOM
exit 0). Docker Desktop version and LinuxKit kernel are recorded there. These
facts belong to a shared-kernel container on a laptop. They do not transfer.

## 4. Provisioning prerequisites and approval gates

**PROPOSED.** Before a host is created, an operator records, in a dated
`isolation-validation/` entry: the intended host, the tenancy, who approved
the spend, the OS and kernel, the runtime and version, and the immutable
image digest to be pulled. Approval of the spend is a written decision by
repo owner. The repository does not authorize it; `ops/isolation/` expects the
host to already exist. Nothing here provisions.

## 5. Administrative access and patching

**PROPOSED.**

- One operator account, key-based SSH, root only via `sudo -i` with a
  recorded reason; the account is not used by the control plane.
- OS patching on a schedule that the operator records in the validation
  entry; the schedule and the date are part of host eligibility.
- The runtime socket is local to the host. No remote control-plane endpoint,
  no SSH forwarding of the runtime socket, and no remote Docker contexts.

## 6. Network egress denial

**PROPOSED.** Guest egress is default deny. A host firewall (nftables or
equivalent) denies all traffic by default; per-run networks receive at most
the rules the runtime declares. Denial is verified from *inside* the guest —
connect attempts that fail with a blocked errno — never from the host side,
and never by reading a firewall declaration.

**MEASURED (shared-kernel container).** `run.sh` connects to `1.1.1.1` and
`8.8.8.8` from inside a `--network=none` container and observes
`blocked errno=` lines.

**What suffices for PASS.** The evidence value `net_1_1_1_1=blocked` and
`net_8_8_8_8=blocked` observed inside the guest (`measured_from=guest`,
unsimulated) with the errno values recorded. A single endpoint is
`NOT_MEASURED`, not PASS. An `open` value is an instant FAIL; identify the
leak before anything else on the host.

## 7. Metadata-service protection

**PROPOSED.** Block `169.254.169.254` (AWS/GCP IMDS) and `168.63.129.16`
(Azure) from the guest networks. Block any route to the control plane
addresses and its database. On platforms where metadata access has a
separate switch (IMDSv2 hop limit), require that switch to be set and record
it.

**What suffices for PASS.** `meta_imds=blocked` *and* `meta_azure=blocked`
from the guest. A cloud without a metadata service must record why in the
run's notes; the collector rule refuses both missing keys.

## 8. Filesystem and host-mount restrictions

**PROPOSED.** No host directory is mounted into the guest except a per-run
scratch. The runtime socket, `/var/lib/docker`, and the operator home never
appear in guest mount tables or paths. Bind-mount provenance is host-reaped
and verified.

**MEASURED (shared-kernel).** `probe.py mounts` checks the host marker file,
`docker.sock` presence, `/var/lib/docker` presence, and graph driver paths
from inside the guest.

**What suffices for PASS.** From the guest: `sock_docker=0`, `sock_run=0`,
`docker_dir=0`, `graph_path=0`, `marker_content` not `HOST`. Any `1` or
`HOST` is a FAIL: the ownership boundary leaks.

## 9. Credential isolation and secret handling

**PROPOSED.**

- The control plane never sends the database URL, session secret, GitHub App
  private key, or artifact keys to the host and never through the guest.
- The host launcher script is reviewed; it exports none of those into the
  guest environment.
- The ledger key lives only on the host, outside the guest (`host_ledger_key`
  fact).
- Installation tokens for repository reads stay in the control plane and are
  scoped to `contents:read` on one repository.

**MEASURED (shared-kernel).** `probe.py creds` censuses environment variable
*names* (never values) for `DATABASE`, `SECRET`, `GITHUB`, `PRIVATE_KEY`, and
scans values for known token prefixes. The collector stores only counts and
booleans, never names or values.

**What suffices for PASS.**

- `bad=none` (no control-plane-like name present in guest environment);
- `leak=none` (no known token-prefix value in any guest environment value);
- no container runtime sockets in the guest;
- `host_ledger_key=absent` in the guest.

A name with a sensitive word or any leaked token prefix is a FAIL. Absence by
name census is evidence about the launcher at this moment in time; it does
not prove the host cannot receive secrets another way, and it cannot detect a
secret under an innocuous name. The runbook therefore treats this control as
necessary, not sufficient, and the report says so in its notes.

## 10. Unprivileged execution and resource limits

**PROPOSED.** The guest always runs unprivileged: non-root uid, no elevated
capabilities, `NoNewPrivs` on, privileged-mode absent, and every kernel mount
and namespace operation refused. CPU, memory, pid, disk, output, and wall
limits are enforced *outside* the guest, measured by observing each resource
hitting its cap, not by reading configuration.

**MEASURED (laptop/shared-kernel).** The values in §3: 67108864-byte disk
cap, 0.500 CPU ratio, memory exit 137, pid count 14, plus the reaper timings,
on the laptop's shared LinuxKit VM. A dedicated host must re-measure all six;
the laptop numbers do not carry over.

**What suffices for PASS (per limit).**

- CPU: `cpu_max` in `quota period` integer form (cgroup v2), not `max`.
- Memory: the allocation was terminated by the kernel OOM inside the guest
  cap (`memory_oom=cgroup_oom`), *never* by a timeout or by the tool.
- PIDs: `fork` refused with `stop_errno` before the 48-fork bound.
- Disk: `ENOSPC` (errno 28) before the 2 MB probe target.
- Output: the output cap rejected an oversized result (`output_limit=capped`).
- Wall: the deadline expired before the probe finished (`wall_limit=capped`).

A missing measurement is NOT_MEASURED for the whole `limits_enforced` field,
never a partial pass. A timeout that kills a probe is an ERROR of the tool,
not proof the guest resource was capped.

## 11. Runtime and image digest recording

**PROPOSED.** Every record names the exact runtime command and the immutable
digest (`runtime_name`, `runtime_version`, `image_provenance=digest_pinned`
with a `sha256:` digest recorded in identity). Tag-fashioned references are
rejected. Pulling and running attach the same digest, and the HostGate
comparator requires it.

**MEASURED (references).** `run.sh` pins
`python@sha256:e8a38829…` and checks that the reference and pull target are
identical; the Mac probe pins `alpine@sha256:d9e853e8…`.

## 12. Evidence collection, review, invalidation

**TOOLS DEPLOYED.** `ops/isolation/evidence_report.py` (schema v1) plus
`ops/isolation/collect_report.py` (identity and report subcommands). The
collector reads one probe section's `RESULT` lines, derives each control
observation with an explicit rule, records statuses (`PASS`, `FAIL`,
`NOT_MEASURED`, `ERROR`), and derives seven field statuses that match the
domain's `IsolationChecks` field names and control mapping. The evidence
*level* is derived, never chosen: `in_process` derives `simulation`;
`mac_docker_desktop` derives `shared_kernel_container` and the report is
bounded there. `production_approved` is not constructible by this tool at
all — no caller argument, no environment variable, no configuration flag,
and no self-promotion. A hash (`report_sha256`) follows the exact bytes; an
identity fingerprint (`identity_fingerprint` / `is_current`) binds a report
to its host and invalidates it when the host, kernel, runtime, or digest
changes.

**Collection procedure (PROPOSED once a host exists).**

1. `python3 ops/isolation/collect_report.py identity --mechanism …` writes
   the observed identity — `os`, `arch`, `kernel`, `runtime`,
   `runtime_version` — with the mechanism. An unobservable fact is an
   `ERROR` marker, and an incomplete identity makes the report step refuse
   rather than fill a guess.
2. Run one probe section inside the guest and pass its output:
   `python3 collect_report.py report --identity … --results -`.
   The mechanism is outside the guest; the tool records `measured_from`.
3. Record `report_sha256` and the identity fingerprint with the collected
   report and the guest and host log excerpts in
   `isolation-validation/YYYY-MM-DD-<scope>.md`.

**What cannot be verified here (NOT MEASURED).**

- Credential absence beyond launch-time environment names.
- Kernel-level security properties or a specific cloud provider's identity
  boundaries.
- End-user privacy properties not covered by the probes.
- Anything about the control plane's production readiness.

**Review (PROPOSED gate).** A reviewer that is not the collector:

- re-derives the control mapping against `FIELD_TO_CONTROLS`;
- reads the raw log excerpts against the reported PASS/FAIL statuses;
- confirms the mechanism-derived level is inside the allowed ladder
  (`in_process` → `simulation`, `mac_docker_desktop` and
  `github_hosted_runner_docker` → `shared_kernel_container`,
  `linux_host_docker` → `dedicated_host_container`, `per_run_microvm` →
  `per_run_microvm`);
- confirms every `IsolationChecks` field's controls are observed, not
  simulated, guest-side;
- writes the review decision in a dated validation entry.

**Invalidation (PROPOSED).** A report is invalid when:

- the identity fingerprint changes (any kernel, runtime, or digest change);
- the mechanism-derived level no longer admits the claimed level — the tool
  refuses, and the reviewer's authorization lapses;
- the guest's image digest changes;
- the runtime version changes;
- the collector's schema version grows — always re-collect (v1 reports are
  never rewritten silently).

## 13. Incident response, retirement, evidence revocation

**PROPOSED.**

- A suspected leak stops new `register` on the host. The host keeps
  accepting authenticated `cancel` and `complete`, cleanup stays stopped,
  and nothing deletes audit or outbox rows. The ledger and the identity
  survive, so evidence remains addressable after the incident.
- Host retirement: revoke the host's copy of the ledger key (rotate first if
  needed), drop all guest images, mounts, and forwards, deactivate the
  operator's SSH account, and record the retirement date in the validation
  entries. All previously approved evidence from that host lapses on
  retirement.
- Evidence revocation: it is revocation-by-invalidation in this design. Any
  report can be tested against its identity fingerprint and marked stale;
  no unreadable evidence stays invisible after the host is gone — the HOST
  identity is what binds, so revocation means "do not trust identity X from
  now on".

## 14. Go/no-go criteria

**The go decision (PROPOSED) requires, all together:**

1. A dedicated host with no control-plane secrets, verified by the secret
   census and the vault review.
2. Every one of the seven checks observed inside the guest, unsimulated, on
   the dedicated host.
3. The full `FIELD_TO_CONTROLS` mapping satisfied: 20 control observations,
   including the full six resource limits and four cleanup controls.
4. Defense in depth, not the gate itself: the reviewer received the bundle
   hash, the log excerpts, and the cross-check, and signed off in a dated
   entry.
5. The reviewer's explicit, separate confirmation at the
   `production_approved` level — which, by design, no collector run can
   produce.

**Security review is mandatory when** a new runtime mechanism appears, a
provider consumes metadata differently, a guide record changes the identity
(§12 invalidation) or a control failure is observed. In each case, evidence
stays at or below the previous level and re-collection from the new
configuration is required.

**The no-go default.** No host, no review, no control observations, no
approved level: untrusted execution stays disabled, `IsolationChecks` stay
false, and the composition root keeps `ClosedSandbox`, `control_plane_reaper`
and refusal of host handoff. A passing test suite is not a go.

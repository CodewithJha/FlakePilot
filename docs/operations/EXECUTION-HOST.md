# Dedicated execution host

Stage 10G. This page is the contract for a future host. No host was provisioned, no virtual machine was created, and nothing here was deployed. Untrusted execution stays disabled. `IsolationChecks` stay false. The reference adapter in `flakepilot.execution.application.reference_host` is an in-process simulation. A passing test of that adapter is not a microVM and is not production isolation.

The binding rules remain [ADR-004](../adr/ADR-004-control-plane-and-execution-plane.md) and [ADR-008](../adr/ADR-008-deployment-and-trust-model.md). The control-plane behavior is [EXECUTION](EXECUTION.md).

## Threat assumptions

The control plane holds the database URL, the session secret, and any GitHub App private key. It is not a safe place to run repository code. The execution host is a different machine. It may hold the host ledger key and the runtime control socket. It must not hold control-plane secrets. The guest, the repository, its tests, and its dependencies are untrusted. A container that shares the host kernel is not a separate kernel boundary.

The control plane must not receive the host ledger key, a copy of the ledger file, or an unrestricted runtime socket. The host must not receive the database URL, the session secret, or the GitHub App private key. Artifact encryption keys, when a key manager exists, stay outside the guest and outside the execution worker. Installation tokens stay in the control plane.

Repository names, numeric ids, branch names, artifact keys, and callback bodies are untrusted input. They do not grant an operation by themselves.

## Protocol version 1

Version 1 is the message grammar below. It is not a socket protocol, and it is not production-ready transport. There is no mTLS listener, no framed codec, and no deployed peer. A later transport has to carry these bytes unchanged and has to be tested as that transport before anyone calls it ready.

| Operation | Message bytes | Effect |
|---|---|---|
| `register` | `register <execution id> <unix expiry>` | Record one run. The same bytes may be repeated. A different expiry for the same id is rejected. An expiry at or before the host clock is rejected. |
| `cancel` | `cancel <execution id>` | Terminal mark. Repeating it is the same mark. |
| `complete` | `complete <execution id>` | Same terminal mark as cancel. The outcome text stays on the control plane. The id cannot be registered again. |
| `health` | `health` | Protocol `1`, authority `available` or `unavailable`, runtime `simulated` or `unavailable`, isolation `simulation`. |
| `cleanup` | Not a signed message. The host reads its own ledger. | Selects container ids the ledger says to remove. Selection is not removal. |

The mac is HMAC-SHA256 of the exact message bytes, hex-encoded, compared in constant time. The key is at least 32 bytes. `HostRejected` always surfaces as `rejected`. A bad mac, a malformed message, an unknown operation, or a message whose first word is not the named operation writes nothing.

`ReferenceHost` is not constructed by the worker. `runtime_available=True` only lets the simulation record a registration and select ids. Health isolation cannot be set to `per_run_microvm` or `production_approved`. Register is refused when the runtime flag is off. Cancel and complete still update the ledger when the authority is available, so a terminal mark is not dropped because no runtime is running. Cleanup returns `runtime_unavailable` or `authority_unavailable` and selects nothing in those cases. An unreadable ledger selects nothing.

Idempotency is the ledger rule: the same register is a no-op, cancel and complete of a terminal id are no-ops, and cleanup of the same pairs returns the same ids without deleting ledger rows. A process that opens `FileLedger` on the same path sees those rows. That reopen is a simulated restart of the host object. It is not a reboot of a machine. The control-plane worker still uses `control_plane_reaper` and `RefusedHostHandoff`.

Retry of a rejected request does not create a row. Retry of an accepted register with the same expiry does not create a second row. There is no automatic retry loop in this repository.

## Isolation evidence

Levels, weakest first:

1. `simulation` — an in-process test. The reference host is this level.
2. `shared_kernel_container` — a container on a shared kernel, including Docker Desktop's LinuxKit VM.
3. `dedicated_host_container` — a container on a machine that is not the control plane, still one kernel.
4. `per_run_microvm` — a kernel boundary enforced separately for that run.
5. `production_approved` — that microVM plus reviewed resource, network, storage, and lifecycle evidence.

Every level requires the same controls. Evidence collected at a weaker level does not count for a stronger one. `evidence_satisfies` is false when the observed rank is lower than the required rank. `attest` ignores those records and leaves the mapped `IsolationChecks` field false when any required control is missing. A truthy non-bool does not count. `attest` is a pure function for the ladder. Production code does not call it. The only production attestation remains `unavailable()`, with every check false.

Controls, and the check they feed:

| Check | Controls that must all be observed at the required level |
|---|---|
| `network_denied` | Default-deny egress |
| `metadata_denied` | Cloud metadata endpoints blocked, and control-plane endpoints blocked |
| `host_mounts_denied` | No broad host mounts, and filesystem access read-only or tightly scoped |
| `credentials_absent` | No database URL, session secret, GitHub App private key, or host ledger key in the guest |
| `unprivileged` | The guest is not privileged |
| `limits_enforced` | CPU, memory, PID, disk, output, and wall-clock limits enforced outside the guest |
| `cleanup_verified` | Host-side cleanup after launcher or guest death, per-run artifact isolation, immutable image digest, and an audit trail that supports incident recovery |

A configuration flag or a unit test that returns true is not one of these observations. The laptop logs from 21 and 22 October 2026 are `shared_kernel_container` measurements. They do not satisfy `dedicated_host_container`, `per_run_microvm`, or `production_approved`.

## Deployment specification

This section is what a later host has to meet. None of it was performed.

### Host

- A machine that is not the control plane and does not have the database URL, the session secret, or the GitHub App private key.
- Linux. The architecture is declared and matches the image. Emulation is not the isolation boundary.
- A virtualization interface for a per-run microVM, used only on this host. Installing a monitor on the control plane does not meet this requirement. No monitor was installed for this stage.
- The guest has no Docker socket, no host network, no privileged mode, and no broad host mount.

### Credentials

- Host ledger key: at least 32 bytes, file mode `0600`, owned by the host user, outside the guest tree. The reference adapter keeps it off `repr`. A real host must also keep it out of child environments, logs, and crash dumps.
- Ledger key rotation is not implemented. The procedure, when built, is: install the new key, reject macs from the old key after in-flight messages drain, and destroy the old key. Do not copy either key to the control plane.
- Artifact keys: supplied by a key manager to `AesGcmSealer`. Absent today. See below.
- Image-pull credentials, if a registry requires them, stay on the host. The guest does not receive them.
- GitHub App private keys and installation tokens stay in the control plane. The token scope already implemented there is `contents:read` for one repository. Live minting against GitHub is not verified.

### Network

- Guest egress is default deny.
- Block cloud metadata addresses, including `169.254.169.254`, and block any route to the control plane, its database, and its metadata service.
- The host may have a separate, reviewed path for an image pull and for authenticated control-plane messages. That path does not exist yet.
- Do not publish the runtime socket to the guest or to the control plane.

### Resources and lifecycle

- Enforce CPU, memory, PID, disk, output size, and wall clock outside the guest. Control-plane quota checks (`wall` 5–120 seconds, `memory` 64–2048 MiB, `pids` 8–256) are request validation. They are not host enforcement.
- The image reference is an immutable digest. A mutable tag is not provenance.
- The host, not the guest, reaps the run after cancel, expiry, completion, or launcher death. Cleanup is idempotent and does not delete ledger rows, audit rows, or outbox rows.
- On host boot, a missing or unreadable ledger is not an empty authority. `FileLedger` already refuses that file. The control-plane worker must not be the process that opens it.

### Observability

- Log the operation, the execution id, and the status. Do not log keys, macs, artifact bytes, tokens, or database URLs.
- Audit the register, cancel, complete, cleanup selection, health failure, and ledger rejection. Keep those records across cleanup.
- Alert on authority unavailable, runtime unavailable, an unreadable ledger, repeated rejected macs, and a cleanup that cannot run. No alert sink was configured.

### Patch, backup, incident

- Patch the host OS on the execution host. Rebuild the guest image from a reviewed definition and pin the digest. Record the digest that ran. None of this was done.
- Back up the ledger only to storage the guest and the control plane cannot read. Do not back the plaintext artifact directory up as if it were ciphertext.
- Incident containment: stop `register`, keep accepting authenticated `cancel` and `complete`, leave cleanup unselected while the runtime is unavailable, and do not delete audit or outbox rows. Safe shutdown is that refusal. It was not exercised on a host.

## Artifact encryption

`AesGcmSealer` writes version 1 blobs: a version byte, a key identifier, a 12-byte nonce, and AES-GCM ciphertext with its tag. `seal` uses the current key id. `open` uses the id inside the blob, so a retired key still decrypts while it remains in the map. An unknown id, a wrong key, or a bad tag raises `integrity`. A blob that is not this layout raises `invalid_request`. Tests cover tamper, the wrong key, a truncated blob, a bad version, rotation, path traversal, a symlink escape, and a file whose stored bytes are not the plaintext.

That is a tested sealer. It is not encryption at rest for this deployment. The composition root still constructs `LocalFsArtifactStore`. `UnconfiguredSealer` still refuses to write when a sealed store has no key. No key is in source, and the tests generate key bytes at runtime. A key manager outside the workers and the guest is still required before the app may construct `AesGcmSealer`.

## Retention

`select_for_deletion` accepts only `policy="disabled"` and then returns no keys. Any other policy, including a duration or the name `approved`, raises `retention_unconfigured`. There is no deleter, so there is no deletion retry and no deletion restart to recover.

The decision that is still required is one product, legal, and security approval of a retention duration, plus an explicit rule that audit rows and outbox rows are not artifact keys. This repository does not choose the duration. Tenant-scoped deletion can be implemented only after that decision, and it must be idempotent, retryable, and safe across a process restart without deleting audit or outbox evidence.

## GitHub App

Server-side checks still bind a session to an org, a repo grant, a run, and an artifact key before bytes are returned. `read_execution_artifact` compares the org before it calls the store. The installation minter asks for `contents:read` on one repository and keeps the token in the adapter. This stage did not push, open a pull request, or merge. No App id or private key was available to this process. Live authentication is not verified.

## Pre-production acceptance

None of these boxes were checked by this stage.

- [ ] The host is a different machine from the control plane, with none of the control-plane secrets.
- [ ] A per-run microVM starts from an immutable image digest and uses a separate kernel.
- [ ] Default-deny egress, metadata blocking, and control-plane blocking were measured on that host.
- [ ] CPU, memory, PID, disk, output, and wall limits were measured outside the guest.
- [ ] The guest environment was shown not to contain the database URL, session secret, GitHub App private key, or host ledger key.
- [ ] Host-side cleanup ran after the launcher was killed, and a second pass removed nothing new.
- [ ] The ledger survived a host reboot and still refused a missing file.
- [ ] Artifact ciphertext is what the store returns to an authorized caller, and the key manager is outside the guest.
- [ ] An approved retention duration exists, and a deletion pass left audit and outbox rows in place.
- [ ] A live GitHub App installation minted `contents:read` for one repository and did not receive a write scope.
- [ ] `IsolationChecks` are still false until that evidence is reviewed. A green test suite does not check this box.

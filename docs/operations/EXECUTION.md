# Secure browser reproduction

Stage 10. The binding rules are [ADR-004](../adr/ADR-004-control-plane-and-execution-plane.md) and [ADR-008](../adr/ADR-008-deployment-and-trust-model.md). This page is the contract the code implements. It is not a claim that this deployment is a production sandbox. Untrusted execution is not enabled. The default mode remains `closed`. No dedicated execution host is configured.

## Fail closed

The default execution mode is `closed`. In that mode the worker never starts a container, never starts a subprocess for a test, and never runs repository code in the API or worker process. There is no in-process fallback.

`POST /orgs/{org_id}/reproductions` is the operator entry. In `closed` mode it stores `inconclusive` / `execution_closed` immediately. It does not call GitHub, enqueue a job, or start a runtime. A repeat of the same org, repo, selector, and SHA returns that row. `schedule` itself does not read the mode. A row that is already `scheduled` is still enqueued on worker start, and the handler then records `execution_closed` without fetching source.

`evaluation` is an explicit opt-in and is not production isolation. The operator route still does not call the code host: a live per-user GitHub ACL is not available to this process, so the row is `inconclusive` / `source_unverified` and no job is enqueued. Containers share the host kernel (ADR-008). This repository does not ship a browser image, and this control plane does not embed Playwright. Playwright, when an operator supplies a digest-pinned image that contains it, would run only inside that container. No such container is started here.

A container is not a microVM. ADR-008 requires a microVM-class boundary before a hosted multi-tenant service. That boundary is not implemented. No container runtime, microVM monitor, or verified seccomp profile is installed in this environment, and none was added.

## Trust boundary

Untrusted repository contents, tests, dependencies, and build scripts are the execution plane. The API and the worker are the control plane. They hold the database URL, session secrets, and any GitHub App private key. Untrusted code must not run in those processes and must not run on a host that has those secrets. This repository's process is the control plane. It does not start a sandbox.

| Threat | What the code does | What is still missing |
|---|---|---|
| Malicious repo, tests, dependencies, or build scripts | `closed` returns `execution_closed` and starts nothing. The operator `evaluation` path does not fetch source | A separate execution host, and a runtime that was actually started and measured |
| Host files, credentials, database URLs, internal services | The control plane does not pass its environment into a container, because it does not start one. Job payloads are the execution id only | Proof that a runtime process cannot read the host |
| Cloud metadata and other egress | Not reachable from code that does not run | A default-deny network policy measured against a live runtime, including metadata addresses |
| Runtime sockets, host mounts, privilege, kernel attack surface | No privileged container, socket mount, or seccomp profile is configured, because no runtime is invoked | A reviewed non-root syscall policy on a real runtime, and a microVM boundary where the kernel is shared |
| CPU, memory, PID, disk, process, and wall time | The execution row has a lease and a wall-clock budget. The worker renews the job lease while the handler runs. Argument-list limits exist only as text. `LabelledContainerReaper` removes a labelled container when the host ledger says the deadline has passed or the run was cancelled. The container label is not that deadline. A worker lease is not an input | The control plane does not call that reaper. Host enforcement of disk, CPU, memory, and pids is not claimed for this deployment |
| Malformed or oversized output, path traversal, symlinks | Result JSON is allow-listed. Artifact keys are validated and must stay under the root. Symlinks are not treated as artifact bytes | A runtime that produces those outputs under a real mount policy |
| Worker crash, host restart, cancel, orphans | A stale execution owner or an older generation cannot finish. `finish` locks the running row and stores `cancelled` when `cancel_requested` is already true, so a pass that has not been stored does not win. `fp-` directories older than an hour are removed on worker start. `reconcile` reports orphan blobs and missing keys and deletes neither. `select_for_deletion` with policy `disabled` returns no keys. A labelled container whose wall deadline has passed can be removed by `LabelledContainerReaper` without the client that started it. A second sweep of a container that is already gone removes nothing | The worker calls `control_plane_reaper`, which reports `runtime_reaper_unavailable` and does not open a host ledger. `RefusedHostHandoff` refuses register, cancel, and complete because this process has no host key. A host restart of an execution machine was not performed. Crash of the machine that holds the plaintext artifact disk |
| Cross-tenant reads and artifact disclosure | Artifact bytes require `repo.view` on that execution's org before the store is read. The key must be `exec/<org>/<repo>/sha256/<hex>` for that row. The hash is checked on read. `SealedArtifactStore` refuses to write when the sealer is `UnconfiguredSealer`. `AesGcmSealer` can seal and open a versioned AES-GCM blob in tests, including a retired key id | The composition root still constructs `LocalFsArtifactStore`. There is no key manager, so this disk is plaintext. No retention duration is approved |

Evidence is eligible only when `accept` says so: completed `success` or `test_failure`, canonical org and repo ids, a 40-character lowercase SHA, complete evidence, not superseded, and all seven isolation checks true (`network_denied`, `metadata_denied`, `host_mounts_denied`, `credentials_absent`, `unprivileged`, `limits_enforced`, `cleanup_verified`). Production uses `UnavailableIsolation`, which sets every check false. There is no setting that marks isolation verified. A fixture in unit tests may pass all seven checks; that fixture is not this deployment. Eligibility is not a flakiness classification. One failure is not a flake. `infra_failure` is not a test failure. `success` and `test_failure` stay distinct.

## What may run

One approved command, compiled into the domain, not taken from the request:

```text
/opt/flakepilot/reproduce
```

The request may carry a selector. The selector is a single token (letters, digits, `_`, `.`, `:`, `-`, at most 120 characters, and not `..`). It is passed as an environment variable on the container, never as a shell string. The payload cannot choose a binary, a shell, a tenant, or a handler.

## Planes

| Plane | Process | May do | Must not do |
|---|---|---|---|
| Control | API and `worker` | Authorize, write `exec.executions`, claim a job, validate outputs, store artifacts | Run repo or test code. Start a container. Put a database URL, GitHub key, or token on an execution handoff |
| Execution host | Not provisioned | Nothing in this deployment | Share a machine with control-plane secrets |

The control plane would start every interaction. No execution host is configured, so there is no sandbox to call. The handoff type is `PlaneRequest`: execution id, org, repo, SHA, selector, wall-clock seconds, memory, pids, and the SHA-256 of the source archive. Those are the only fields. `UnavailableExecutionPlane.submit` returns `execution_host_unavailable` and does not connect. Repeating it does not run. The worker's sandbox is `ClosedSandbox` even when `FLAKEPILOT_EXECUTION_MODE=evaluation`.

## Revision pin

A caller-supplied SHA is not evidence. Scheduling authorizes `repo.investigate` (IAM), loads the repository binding (`repo.repositories`, host `github` only), and asks the code host to confirm that exact commit. The host's SHA must equal the claimed SHA. Unsafe URL schemes and redirects are rejected. The token stays in the adapter.

The operator route does not confirm a commit. A repository binding plus a FlakePilot grant is not a live GitHub ACL, and this process cannot ask GitHub whether the caller can read the repository. The route therefore stores `inconclusive` / `source_unverified` and does not call the code host, in `closed` and in `evaluation`. A repeat of that tuple returns the same row. `schedule_registered` calls the host only when its caller returns a confirmed read. The operator route never passes such a caller.

Installation minting, when some other caller does confirm a read, still uses `FLAKEPILOT_GITHUB_APP_ID` and `FLAKEPILOT_GITHUB_APP_PRIVATE_KEY` together. The token is `contents:read` for that one repository, created at call time, kept in the adapter, and never written to a job, an artifact, a log line, or an error. It is not passed to sandbox code. Redirects are refused. If either setting is missing, or the key is not an unencrypted RSA private key, that caller does not call GitHub. `FLAKEPILOT_GITHUB_CONTENTS_TOKEN` is still parsed so an old environment loads, and execution does not send it. There is no live GitHub App credential in this environment. Minting and installation membership were tested with a fake transport only.

Source bytes are fetched by the control plane and extracted into a per-run directory. Archive members that escape the directory, or that exceed the size and count caps, abort the run as `infra_failure` / `invalid_source`. The token is not copied into the container.

## Lifecycle

`exec.executions` is the record. `orch.jobs` (`browser_reproduction`) is the wake-up. The payload is the execution id only.

| State | Outcome | Meaning |
|---|---|---|
| `scheduled` | (none) | Authorized and pinned. A job is enqueued, or will be on the next worker start |
| `running` | (none) | Claimed. `generation` and the lease identify the attempt |
| `completed` | `success` | The validated result said `passed`. This is reproduction evidence, not a verification |
| `completed` | `test_failure` | The validated result said `failed`. The sandbox itself did not fail |
| `completed` | `infra_failure` | The runtime, the archive, or the host failed. Not evidence of flakiness |
| `completed` | `timeout` | The wall clock fired. Not a test failure |
| `completed` | `invalid_result` | Output was malformed, oversized, or tried to name an outcome, tenant, or handler |
| `completed` | `inconclusive` | Nothing ran: closed mode, or the revision was not verified |
| `cancelled` | `cancelled` | Cancel was set before claim, or a live holder observed it and discarded the runtime result. An expired running row with cancel is finished as cancelled and is not claimed again |

Terminal rows are not deleted and are not rewritten. A finish whose `generation` or lease owner does not match changes nothing and rolls back any artifact rows from that attempt, so a stale attempt cannot overwrite a newer result.

One active row per `(org, repo, selector, sha)`. A duplicate schedule returns that row. Two workers claiming it serialize on the row lock; the loser finds it no longer `scheduled`.

The execution lease is the wall-clock limit plus 30 seconds. While a holder runs, it renews that lease and reads `cancel_requested`. After the runtime returns, the handler reads `cancel_requested` once more and, if it is set, stores `cancelled` instead of the runtime report. A pass that is already terminal is not rewritten. A finish uses a fresh clock. A stale owner, generation, or expired lease cannot renew or finish, and a rejected finish rolls artifact rows back. The job lease is `FLAKEPILOT_WORKER_LEASE_SECONDS`. The worker renews it before the handler and again while the handler runs, at one third of the lease and at least one second. A renewal the store rejects is not completed. A second worker that arrives before the execution lease expires is told the row is busy. If renewal stops and the lease expires, a later attempt can claim. Tests advance a clock. They do not sleep. The renewal test uses a fake store, not two machines.

Worker startup deletes direct child directories named `fp-` under `TMPDIR` (or `/tmp`) whose age is at least one hour. Symlinks are left alone. It then records `runtime_reaper_unavailable`. That reaper removes no container, microVM, or other runtime resource, including after `SIGKILL`, and it does not report a cleanup count above zero.

## Evaluation runtime arguments

`ContainerSandbox` is not constructed by the worker. The argument list below is the contract a future execution host would have to meet. A unit test checks the text. No runtime was started. If a test constructs the adapter and the runtime binary is missing, the result is `runtime_unavailable`. The approved command is not run on the host.

The argument list is built in code. It always includes `--network=none`, `--cap-drop=ALL`, `--security-opt=no-new-privileges`, `--read-only`, a non-root `--user`, `--pids-limit`, `--memory`, `--cpus`, a nosuid/nodev/noexec `--tmpfs` for `/tmp`, and the digest-pinned image. It never includes `--privileged`, the runtime socket, host networking, or a payload-chosen command. The process environment of the runtime client is `PATH` only, so control-plane variables are not inherited. The container receives `SELECTOR` and `FLAKEPILOT_SHA` only.

`--network=none` is how metadata endpoints and every other egress are unreachable. Nothing inside the sandbox can change that flag.

`--rm` removes the container when the client exits. A client killed with `SIGKILL` does not run `--rm`, so the container keeps running. The argument list still records `flakepilot.expires` before the runtime is invoked. That label is only a discovery hint. The authority is the host ledger, written before start, on a file the container does not mount. `HostGate` accepts one message, `register <execution id> <unix expiry>` or `cancel <execution id>`, and only when the HMAC-SHA256 matches a key of at least 32 bytes held by the host process. A second expiry for the same id is refused, so a rewritten label or a repeated register cannot extend the run. A past label cannot remove a run the ledger still holds open. An unregistered label is removed. The mac check does not open a socket. The key is not a setting of this process and is not read from the environment. `LabelledContainerReaper` reads the ledger and does not see the key. It does not read a worker lease or a database URL. Its runtime client still gets `PATH` only. Two sweeps are safe: the second finds the container already gone. A ledger file that is missing, empty, a symlink, not mode `0600`, or owned by another user is not treated as an empty authority: the sweep removes nothing. Register and cancel take an exclusive flock on a sibling lock file for the reload and the replace, so one process cannot drop another's cancel or a second id. The lock is not the ledger inode. A lock that is a symlink, is not mode `0600`, or is owned by another user is refused. If the lock cannot be taken, the previous file stays. A runtime client that does not answer is a failed command and removes nothing. The control-plane worker does not construct the reaper, the gate, or the ledger. Startup still reports `runtime_reaper_unavailable`. Wiring them here would put a runtime client in the process that holds the database URL. That is why evaluation mode is not a production guarantee.

## Evidence

Outputs are size-capped. The result must be a JSON object whose only keys are `status` (`passed` or `failed`) and `selector` (equal to the request). Any other key, including `verified`, `flaky`, `outcome`, `tenant`, or `handler`, is `invalid_result`. The control plane stores a canonical JSON object it built, not the sandbox's bytes, and only for `success` and `test_failure`. An optional PNG screenshot is stored in those two outcomes when it has a PNG signature and is within the size cap. The store is the existing artifact store (`owner=exec`). Keys are `exec/<org>/<repo>/sha256/<hex>`. The execution id and the verified SHA live on the execution row, not in the key. A SHA on an `inconclusive` / `source_unverified` row is the caller's claim and is not evidence. There is no unauthenticated artifact route.

Hashes are SHA-256 of the stored bytes. Filenames never come from the sandbox. A failed artifact write still persists the terminal outcome, with `evidence_partial` set. A finish the store rejects rolls the artifact rows back. A file already written to disk can remain as an orphan. `reconcile` lists content-addressed files that have no known key, and known keys that have no regular file. It does not delete either. Temporary files older than an hour are still swept at store startup. Symlinks are not read as objects.

`GET /orgs/{org_id}/reproductions/{execution_id}` returns `evidence_status` and `evidence_reason` from `UnavailableIsolation` and lists artifact names without bytes. `GET .../artifacts/{kind}` returns bytes only for `result` or `screenshot`, only after `repo.view`, and only when the key is under that execution's org and repo and the hash matches. Another org is not found. No artifact directory is not found.

Local artifact files are plaintext, mode `0400`. Production must place that directory on an encrypted volume or replace the adapter with encrypted object storage. This process does not encrypt. No retention duration is defined. Audit rows, outbox rows, and execution rows are not deleted.

## Audit

The execution change and one `exec.outbox` envelope commit together. The relay copies it to `aud.events`. The envelope carries the execution id, the outcome, and a short reason. It does not carry source, logs, or selectors. Delivery stays at-least-once. Polling stays sufficient. `LISTEN/NOTIFY` is not added.

## Not in this stage

Audit query, outbox pruning, retention deletion, investigation workflows beyond this single execution, patch verification, live AI, and a microVM sandbox. Reasons: no approved retention rule, and ADR-008's production host split is an operator deployment this repository does not provision. Polling stays sufficient. No audit or outbox row is deleted.

## What is enforced

| Guarantee | How it is known |
|---|---|
| Closed mode fetches nothing and starts nothing | Integration test |
| A job payload with an extra field, a command, or a non-string id is rejected | Unit test |
| A result that adds `verified` is `invalid_result` and is not stored | Integration test |
| Timeout, a missing archive, and cancel-before-claim are not test results | Integration test |
| An artifact-store failure still records the outcome, with `evidence_partial` | Integration test |
| A rejected finish leaves the attempt running and writes no artifact row | Integration test |
| A second delivery of a finished execution does not call the sandbox again | Integration test |
| A row left without a job is enqueued with a payload of the execution id only | Integration test |
| A stale lease owner cannot finish | Integration test against the store |
| The evaluation argument list is `--network=none`, dropped capabilities, no new privileges, read-only, non-root, pid and memory limits, one CPU, a nosuid tmpfs, a digest-pinned image, `/opt/flakepilot/reproduce`, and, for a UUID execution id, `flakepilot.execution` and `flakepilot.expires` labels. The expiry is the wall deadline, computed before the runtime client is called. The runtime client's environment is `PATH` only | Unit test of the argument list. No container was started by the product tests |
| The kernel actually blocks egress, metadata addresses, and host mounts | Not measured on this control plane or in CI. A developer laptop recorded a Docker Desktop run on 21 October 2026, and a later run on that laptop measured tmpfs caps and an expiry sweep. Those records are not this deployment. See [isolation-validation/2026-10-21-laptop.md](isolation-validation/2026-10-21-laptop.md) and [isolation-validation/2026-10-21-laptop-limits.md](isolation-validation/2026-10-21-laptop-limits.md) |
| A live cancel finishes `cancelled` and does not keep a passing result. An expired cancelled attempt is not rerun | Integration test with a fake runtime. No container was started |
| A pass returned after cancel has committed is stored as `cancelled` and writes no artifact row | Integration test with a fake runtime that does not wait for the watcher. No container was started |
| A renewed execution lease can finish later; a stranger or an expired lease cannot | Integration test against the store |
| The worker renews the job lease while the handler runs, and a rejected renewal is not completed | Unit test with a fake job store. Not two hosts |
| Worker startup removes expired `fp-` directories, leaves other files, and reports `runtime_reaper_unavailable` | Unit test. No container was inspected |
| `evaluation` settings still select `ClosedSandbox` on this process | Unit test |
| A handoff rejects a bad quota, SHA, or selector, has no secret field, and the unavailable adapter returns `execution_host_unavailable` twice without running | Unit test |
| An installation token is `contents:read` for one repository, and a static contents bearer is not used for execution | Unit test with a fake transport and an ephemeral RSA key. No live GitHub call |
| The operator route does not call the code host when the caller has not confirmed a live read. Evaluation stores `source_unverified`, returns the same id on repeat, and enqueues nothing | Unit test with fakes, and an integration test. No live GitHub call |
| `POST /orgs/{org_id}/reproductions` in closed mode stores `execution_closed`, returns the same id on repeat, and enqueues nothing. The response is `ineligible` / `execution_closed`. A session is required. Another org is not found | Integration test over HTTP. No container was started |
| Artifact bytes are returned only for the owning org, and a key for another org is not read | Integration test against the local filesystem. The filesystem is not encrypted |
| `reconcile` reports orphan files and missing keys and deletes nothing | Unit test |
| Evidence is eligible only when every isolation check passed. Production checks are all false | Unit test. The all-true case is a fixture, not this deployment |
| Two processes registering or cancelling the same ledger cannot drop a row. A missing, empty, or untrusted ledger removes nothing. A runtime client that does not answer removes nothing | Unit test, including two processes. No container was started |
| Seccomp, a dedicated execution host, kernel enforcement on that host, and production cleanup | Not provisioned. The worker does not reap containers. `LabelledContainerReaper` is unit-tested with a fake CLI and is not selected here. A manual runner job may execute it against that runner's Docker; that job is not this deployment and does not mark isolation checks passed |

## Production prerequisites

Checked again before Stage 9 code: `docker`, `podman`, `firecracker`, and `qemu-system-x86_64` are not installed. There is no container runtime socket. `/dev/kvm` exists and is group `ssl-cert`; this user is not in that group, so it is not usable. This machine holds the repository and the test database password, so it is a control-plane host, not an execution host. No runtime was installed. CI does not start a runtime. Untrusted execution stays disabled. The default mode remains `closed`.

| Kind | What it covers |
|---|---|
| Enforced and tested | Closed mode does not fetch or start a process. The operator route does not call GitHub and does not enqueue, in closed mode and in evaluation, because no live ACL is confirmed. Payloads, selectors, and result JSON are validated in code. A stale finish writes nothing. Artifact rows roll back with a rejected finish. Execution-lease renewal and cancel are enforced in the store and the handler. A committed cancel before persistence discards a pass. The job lease is renewed during the handler in a unit test. Expired `fp-` directories are removed without a runtime. Installation-token requests ask for `contents:read` on one repository, or they do not call the network. The evaluation argument list is checked as text. Evidence from this process is ineligible. Artifact download checks the org and the hash. |
| Configuration-dependent | `evaluation` is still a legal setting and still requires a digest-pinned image so an old environment loads. The worker does not use that image. Installation minting runs only when both app settings are present and a caller has confirmed a live read. The operator route never confirms that read. |
| Untested assumptions | That the laptop's Docker Desktop VM is a sufficient boundary for a hosted multi-tenant service. It is not treated as one. That a dedicated host would contain a kernel escape. That GitHub honors the permission set on a real installation. That a microVM would survive a kernel bug in a container. |
| Missing | A dedicated execution host. A reviewed seccomp profile. Encryption of the local artifact directory. An approved retention duration. Live GitHub ACL and live installation-token minting. Selecting the labelled reaper on a host that is allowed to have a runtime. The control plane must not be that host. |
| Disabled | Production sandbox use. MicroVM isolation. Selecting `LabelledContainerReaper` from the worker. Patch generation, application, push, and merge. Using a static contents bearer for execution. Any setting that would mark isolation checks as passed. |

A passing unit test of an argument list is not a sandbox integration test. Do not treat it as one.

## Operator route

`POST /orgs/{org_id}/reproductions` and `GET /orgs/{org_id}/reproductions/{execution_id}` require a session. Unsafe requests also require `Origin` or `Referer` equal to `FLAKEPILOT_PUBLIC_ORIGIN`. The org is the URL, loaded from IAM for that session. The body may contain only `repo_id`, `selector`, and a 40-character lowercase SHA. `repo.investigate` is required to create; `repo.view` is enough to read. A principal who cannot see the repo, or a different org, gets the same not-found response. The status response lists artifact kind, key, and size, plus `evidence_status` and `evidence_reason`. It does not return bytes.

`GET /orgs/{org_id}/reproductions/{execution_id}/artifacts/{kind}` returns bytes after the same view check. `kind` is `result` or `screenshot`. The key must belong to that execution's org and repo, and the stored hash must match. There is no unauthenticated download.

The client cannot supply a code-host permission. The route does not ask GitHub. `evaluation` does not confirm the SHA and does not enqueue. It is not a verified sandbox.

## Stage 10 provisioning blocker

A dedicated execution host was not provisioned. The exact gap:

- This process is the control plane. It has the repository and the database URL. Putting a runtime on it would not satisfy ADR-008.
- No second machine, hypervisor account, or cloud project is available. No credentials for a free-tier virtual machine were present, and none were created. Paid cloud resources are out of scope.
- `docker`, `podman`, `firecracker`, `qemu-system-x86_64`, and `cloud-hypervisor` are not installed. There is no runtime socket. `/dev/kvm` is group `ssl-cert` and is not usable here. Installing those tools on this host was not done, because the host holds control-plane secrets.
- No encrypted volume and no object-store credentials are available. Local artifacts stay plaintext. Evidence stays ineligible. No retention duration was invented.
- No GitHub App id, private key, or installation is configured, so live token minting and a live per-user ACL stay unverified.

The ten isolation checks were not executed on a FlakePilot execution host, and they are not passed for this deployment. Two measurements are recorded and neither one is that host.

A developer laptop ran benign probes on 21 October 2026 inside Docker Desktop's LinuxKit VM. That record is [isolation-validation/2026-10-21-laptop.md](isolation-validation/2026-10-21-laptop.md). It is a laptop, a Docker Desktop VM, and a container. It is not a dedicated host and it is not a microVM. Stale-worker overwrite and tenant artifact authorization were not executed there. SIGKILL of the docker client left the container running until an explicit label removal.

The same class of laptop ran the chunked disk probe and an expiry sweep later that day. The record is [isolation-validation/2026-10-21-laptop-limits.md](isolation-validation/2026-10-21-laptop-limits.md). It is still that laptop, still not the approved host, and still not a microVM. No FlakePilot database or GitHub App key was on it. Disk was **PASS** there for `size=1m`, `size=1024k`, `size=1048576`, and the product's `size=64m`: stored bytes stopped at the cap and the next write failed. SIGKILL of the docker client left the container running. A separate host shell, not `LabelledContainerReaper`, then removed the expired container, left the active container running, removed nothing on a repeat, and removed the expired container once when two sweeps ran together. A sweep strictly before expiry was not shown. Host restart, worker death with the product reaper, and cancellation inside a runtime were not run.

A GitHub-hosted runner ran the repository harness. The first pull did not return. The second run, [actions/runs/39645626084](https://github.com/CodewithJha/FlakePilot/actions/runs/39645626084), finished the checks and failed the job. Its disk line was `disk_errno=0` after one `write` of 2 MB. That call can return a short count without raising, so the line does not prove `size=1m` was ignored. The runner has not re-run the chunked probe, so disk on the runner is **not re-measured**. The same run showed that Docker left a sleep running after 5 seconds, and that SIGKILL of the client left the container running. The record is [isolation-validation/2026-10-21-github-runner.md](isolation-validation/2026-10-21-github-runner.md). The runner is a GitHub-managed VM, not a microVM. Stale-worker overwrite and application tenant authorization were not executed there.

Those results do not set `IsolationChecks`. The harness writes until 2 MB is stored or the filesystem returns `ENOSPC`, and it tries `size=1m`, `size=1024k`, and `size=1048576`. Wall time is not a Docker deadline. The harness starts two labelled containers, one already expired and one not, and runs `LabelledContainerReaper` in a separate process from the client that started them. The manual workflow is `workflow_dispatch` only, so a failed measurement does not run on every push. Its assertions are unchanged in kind: a limit that does not fail the write, or a reaper that removes a container whose deadline is still ahead, is a failure. Pushing that harness did not start the job. A dispatch event returned HTTP 403. The runner still has no log of the corrected probes. The laptop log above is not that job and is not this deployment.

`LabelledContainerReaper` lists containers labelled `flakepilot.execution` and removes one when the host ledger's deadline has passed, the ledger says cancelled, or the host never registered that id. `flakepilot.expires` on the container does not decide. The worker does not construct the reaper. Startup still reports `runtime_reaper_unavailable`. Unit tests drive it with a fake CLI and do not start a container. No runtime check was executed for this rule. A passing manual run on a GitHub-hosted runner would still not be a production execution host.

Stage 10D did not provision a dedicated execution host or a per-run microVM. The missing prerequisite is a machine that is not this control plane, with no database URL, session secret, or GitHub App private key, and a microVM boundary rather than a shared kernel. Until that machine exists, untrusted execution stays disabled. The laptop and runner logs above are earlier measurements. They are not a run of this reaper, and they do not set `IsolationChecks`.

Stage 10E keeps that rule. The file ledger now fails closed when two processes update it together, and when the file or its lock cannot be trusted. `ops/isolation/local_mac_probe.sh` is the probe a developer can run on a Mac with Docker Desktop. It imports `FileLedger`, `HostGate`, and `LabelledContainerReaper` directly. It does not start the worker, and it does not read a database URL or an application key. A developer laptop ran that probe twice on 22 October 2026. Both runs are that laptop and Docker Desktop's shared LinuxKit VM. Neither is a per-run microVM, and neither is this deployment. The 12:19 run, from commit `110a3e6`, is [isolation-validation/2026-10-22-laptop-reaper.md](isolation-validation/2026-10-22-laptop-reaper.md). Twenty-one CHECK lines passed, including `FileLedger`, `HostGate`, and `LabelledContainerReaper` against real containers. Four failed because the probe captured no disk result, no pid count, and an OOM exit of 0. The 12:39 rerun, from commit `4898aa7`, is [isolation-validation/2026-10-22-laptop-measured.md](isolation-validation/2026-10-22-laptop-measured.md). Every CHECK line passed. Disk stored exactly 67108864 bytes at `size=65536k`, the short write stopped at the same cap, CPU ratio was 0.500, memory was `true 137`, and the pid count was 14. The independent supervisor reclaimed the container after SIGKILL of the client. `IsolationChecks` stay false.

Stage 10F keeps that boundary. The worker runtime cleanup is `control_plane_reaper`, which returns `runtime_reaper_unavailable` and does not construct `LabelledContainerReaper`, `HostGate`, or `FileLedger`. `RefusedHostHandoff` is the control-plane side of register, cancel, and complete: a valid id is answered `host_authority_unavailable`, and nothing is written. The host key stays on a process this control plane does not run. `finish` stores `cancelled` when the locked row already has `cancel_requested`, including when the caller passed a success decision. `SealedArtifactStore` exists so a later key manager can sit in front of a store; `UnconfiguredSealer` fails closed, and the app still constructs `LocalFsArtifactStore`. `select_for_deletion` under `disabled` returns no keys and has no path to audit or outbox rows. No retention duration was chosen. Live GitHub App authentication remains unverified. These interfaces do not set `IsolationChecks`.

Stage 10G adds the host contract in [EXECUTION-HOST](EXECUTION-HOST.md). Protocol version 1 is the in-process message grammar. It is not a network transport and it was not deployed. `ReferenceHost` accepts only an authenticated allowlist: `register`, `cancel`, `complete`, `health`, and `cleanup`. A bad mac, a malformed message, an unknown operation, an expired registration, or a second expiry for the same id writes nothing. The same registration, cancellation, and completion can be repeated. Cleanup selects ledger ids and does not start a runtime. When the runtime flag is off, or the ledger cannot be read, cleanup selects nothing. Health isolation is always `simulation`. The worker does not construct `ReferenceHost`. Isolation evidence is a ladder: `simulation`, `shared_kernel_container`, `dedicated_host_container`, `per_run_microvm`, `production_approved`. A lower observation does not satisfy a higher level. `attest` is not called by the composition root. Production checks stay the all-false `unavailable()` value. `AesGcmSealer` persists ciphertext under `SealedArtifactStore` in tests. The app does not construct it. Retention still accepts only `disabled`. No host was provisioned. Untrusted execution stays disabled.

Untrusted execution remains disabled. Patch generation, push, pull requests, and merge are not in this stage.

## Patch proposals

Not in this stage. A later proposal may exist only when the evidence is an authorized repository, a verified immutable SHA, a validated execution, and an outcome of `success` or `test_failure`, with provenance that the isolation was actually enforced. `infra_failure`, `timeout`, `invalid_result`, `inconclusive`, `execution_closed`, and `source_unverified` are not evidence of a flake or a break. `success` and `test_failure` stay different, and neither one proves a patch. Generation stays behind an application port, AI stays off (`NullAiAssist`), and nothing applies, pushes, opens a pull request, or merges without the existing human-approval boundary. This stage does not add that boundary call, because it performs no external action.

## Deferred from Stage 6

Authorized audit-log query, outbox pruning, `LISTEN/NOTIFY`, investigation jobs beyond this execution, and `int_secrets.outbox` stay deferred. No retention duration was chosen. `select_for_deletion` returns no keys. No audit or outbox row is deleted. Polling remains the delivery mechanism. `int_secrets.outbox` has no caller.

## Deviations

- ADR-008 requires a dedicated execution host in production and a TTL reaper. This repository provisions neither. The default mode is `closed`, which is the fail-closed behavior for production and for untrusted use. `evaluation` is an explicit opt-in and is not a microVM.
- ADR-008 says execution workers have no database access, and that control-plane processes do not drive sandbox workloads. The process that runs `browser_reproduction` is the control-plane worker. It uses the database. It does not exec `docker` or `podman`. No separate execution host exists to receive the handoff.
- ADR-008's hardening list includes a syscall filter. The argument list does not pass a seccomp profile.
- ADR-009's code host has a read tier and an opt-in write tier. Execution can mint `contents:read` only, and only for one repository, and only for a caller that confirmed a live read. The operator route does not. There is no write tier, no branch prefix, and no merge. The static contents bearer is ignored. Live GitHub ACL is not queried, and the route fails closed.
- Patch proposals are not implemented. This deployment cannot produce an eligible result: isolation checks are unavailable, and the operator route does not run a sandbox. `success` and `test_failure` would still not be a flakiness claim.
- Playwright is not a dependency. The control plane does not import it. An operator may put it in the digest-pinned image. That image is not built or run by this repository.

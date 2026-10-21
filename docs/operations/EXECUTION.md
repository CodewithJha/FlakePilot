# Secure browser reproduction

Stage 7. The binding rules are [ADR-004](../adr/ADR-004-control-plane-and-execution-plane.md) and [ADR-008](../adr/ADR-008-deployment-and-trust-model.md). This page is the contract the code implements. It is not a claim that this deployment is a production sandbox.

## Fail closed

The default execution mode is `closed`. In that mode the worker never starts a container, never starts a subprocess for a test, and never runs repository code in the API or worker process. There is no in-process fallback.

`schedule` does not read the mode. A pinned revision is stored as `scheduled`. On startup the worker enqueues any scheduled row that has no job. The handler then records `inconclusive` with reason `execution_closed`, without fetching source and without starting a runtime. There is no HTTP route that schedules a run.

`evaluation` is an explicit opt-in. It is not production isolation. Containers share the host kernel (ADR-008). This repository does not ship a browser image, and this control plane does not embed Playwright. Playwright, when an operator supplies a digest-pinned image that contains it, runs only inside that container.

A container is not a microVM. ADR-008 requires a microVM-class boundary before a hosted multi-tenant service. That boundary is not implemented.

## What may run

One approved command, compiled into the domain, not taken from the request:

```text
/opt/flakepilot/reproduce
```

The request may carry a selector. The selector is a single token (letters, digits, `_`, `.`, `:`, `-`, at most 120 characters, and not `..`). It is passed as an environment variable on the container, never as a shell string. The payload cannot choose a binary, a shell, a tenant, or a handler.

## Planes

| Plane | Process | May do | Must not do |
|---|---|---|---|
| Control | API and `worker` | Authorize, pin a SHA, write `exec.executions`, claim a Stage 6 job, start and stop the runtime client, validate outputs, store artifacts | Run repo or test code. Parse a pass/fail claim as flakiness |
| Runtime client | `docker` or `podman`, only in evaluation mode | `run` / `rm` with the fixed argument list below | Inherit control-plane secrets into the container. Mount the runtime socket |
| Sandbox | One fresh container per run | The approved command, the pinned source tree, a writable empty `/tmp` and output dir | Control-plane credentials, the database, metadata routes, host mounts of secrets |

The control plane starts every interaction. The sandbox has no route back.

## Revision pin

A caller-supplied SHA is not evidence. Scheduling authorizes `repo.investigate` (IAM), loads the repository binding (`repo.repositories`, host `github` only), and asks the code host to confirm that exact commit. The host's SHA must equal the claimed SHA. Unsafe URL schemes and redirects are rejected. The token stays in the adapter.

If the host is not configured, or the commit cannot be confirmed, the execution is stored as `inconclusive` / `source_unverified` and no job is enqueued. No sandbox starts. The contents credential is an optional static bearer, `FLAKEPILOT_GITHUB_CONTENTS_TOKEN`, sent only as a header inside the adapter. It is not a GitHub App installation token, and this repository does not mint one. Empty means the transport is not called.

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
| `cancelled` | `cancelled` | Cancel was set on a scheduled row before claim. The handler does not poll cancel during a run, and it does not pass a cancel flag into the runtime |

Terminal rows are not deleted and are not rewritten. A finish whose `generation` or lease owner does not match changes nothing and rolls back any artifact rows from that attempt, so a stale attempt cannot overwrite a newer result.

One active row per `(org, repo, selector, sha)`. A duplicate schedule returns that row. Two workers claiming it serialize on the row lock; the loser finds it no longer `scheduled`.

The execution lease is the wall-clock limit plus 30 seconds. The job lease is `FLAKEPILOT_WORKER_LEASE_SECONDS` and is renewed once before the handler, not while the run is in progress. A second worker that picks up the job before the execution lease expires is told the row is busy and retries. If the execution lease expires while a run is still going, a later attempt can claim the row. Tests advance a clock. They do not sleep.

## Evaluation runtime arguments

Only when mode is `evaluation` and the runtime binary exists. Otherwise the adapter returns `runtime_unavailable` or `execution_closed` and does not run the approved command on the host.

The argument list is built in code. It always includes `--network=none`, `--cap-drop=ALL`, `--security-opt=no-new-privileges`, `--read-only`, a non-root `--user`, `--pids-limit`, `--memory`, `--cpus`, a nosuid/nodev/noexec `--tmpfs` for `/tmp`, and the digest-pinned image. It never includes `--privileged`, the runtime socket, host networking, or a payload-chosen command. The process environment of the runtime client is `PATH` only, so control-plane variables are not inherited. The container receives `SELECTOR` and `FLAKEPILOT_SHA` only.

`--network=none` is how metadata endpoints and every other egress are unreachable. Nothing inside the sandbox can change that flag.

`--rm` removes the container when the client exits. A worker killed with `SIGKILL` can leave a container behind. A TTL reaper is not implemented. That gap is why evaluation mode is not a production guarantee.

## Evidence

Outputs are size-capped. The result must be a JSON object whose only keys are `status` (`passed` or `failed`) and `selector` (equal to the request). Any other key, including `verified`, `flaky`, `outcome`, `tenant`, or `handler`, is `invalid_result`. The control plane stores a canonical JSON object it built, not the sandbox's bytes, and only for `success` and `test_failure`. An optional PNG screenshot is stored in those two outcomes when it has a PNG signature and is within the size cap. The store is the existing artifact store (`owner=exec`). Keys are `exec/<org>/<repo>/sha256/<hex>`. The execution id and the verified SHA live on the execution row, not in the key. A SHA on an `inconclusive` / `source_unverified` row is the caller's claim and is not evidence. There is no public artifact route.

Hashes are SHA-256 of the stored bytes. Filenames never come from the sandbox. A failed artifact write still persists the terminal outcome, with `evidence_partial` set. A finish the store rejects rolls the artifact rows back. A file already written to disk can remain as an orphan; nothing deletes it.

Artifacts are not encrypted at rest. Nothing in this stage deletes them. Retention durations are not invented here.

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
| The evaluation argument list is `--network=none`, dropped capabilities, no new privileges, read-only, non-root, pid and memory limits, one CPU, a nosuid tmpfs, a digest-pinned image, and `/opt/flakepilot/reproduce`. The runtime client's environment is `PATH` only | Unit test of the argument list. No container was started |
| The kernel actually blocks egress, metadata addresses, and host mounts | Documented only. It depends on `docker` or `podman` honoring those flags. Neither binary is installed in CI or in the environment that produced this stage |
| TTL reaper, seccomp, a dedicated execution host, GitHub App installation-token minting | Not implemented |

## Production prerequisites

Checked in this environment before Stage 8 code: `docker` and `podman` are not installed, and there is no container runtime socket. CI does not start a runtime. A hardened execution host was not provisioned. Untrusted execution stays disabled. The default mode remains `closed`.

| Kind | What it covers |
|---|---|
| Enforced and tested | Closed mode does not fetch or start a process. Payloads, selectors, and result JSON are validated in code. A stale finish writes nothing. Artifact rows roll back with a rejected finish. The evaluation argument list is checked as text. |
| Configuration-dependent | `evaluation` plus a digest-pinned image selects `ContainerSandbox`. That selection is an operator opt-in. It does not prove the kernel applied the flags. |
| Untested assumptions | That `--network=none` blocks egress and metadata addresses. That the runtime drops capabilities and enforces memory, pids, and a read-only root. That a dedicated host would contain a kernel escape. |
| Disabled | Production sandbox use. MicroVM isolation. A TTL reaper for containers. Short-lived GitHub App installation tokens. An operator route that schedules a run. Patch application, push, and merge. |

A passing unit test of an argument list is not a sandbox integration test. Do not treat it as one.

## Deviations

- ADR-008 requires a dedicated execution host in production and a TTL reaper. This repository provisions neither. The default mode is `closed`, which is the fail-closed behavior for production and for untrusted use. `evaluation` is an explicit opt-in and is not a microVM.
- ADR-008 says execution workers have no database access, and that control-plane processes do not drive sandbox workloads. The process that runs `browser_reproduction` is the control-plane worker. It uses the database. In evaluation mode that same process execs `docker` or `podman`. The container does not receive the database URL. The worker process does.
- ADR-008's hardening list includes a syscall filter. The argument list does not pass a seccomp profile.
- ADR-009's code host uses short-lived GitHub App installation tokens and a write tier. This stage has a read-only static bearer and no token minting.
- Playwright is not a dependency. The control plane does not import it. An operator may put it in the digest-pinned image. That image is not built or run by this repository.

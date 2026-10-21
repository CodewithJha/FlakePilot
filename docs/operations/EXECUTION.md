# Secure browser reproduction

Stage 7. The binding rules are [ADR-004](../adr/ADR-004-control-plane-and-execution-plane.md) and [ADR-008](../adr/ADR-008-deployment-and-trust-model.md). This page is the contract the code implements. It is not a claim that this deployment is a production sandbox.

## Fail closed

The default execution mode is `closed`. In that mode the worker never starts a container, never starts a subprocess for a test, and never runs repository code in the API or worker process. A request is recorded as `inconclusive` with reason `execution_closed`. There is no in-process fallback.

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

If the host is not configured, or the commit cannot be confirmed, the execution is stored as `inconclusive` / `source_unverified` and no job is enqueued. No sandbox starts.

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
| `cancelled` | `cancelled` | Cancel was set before the sandbox started, or the run was killed by cancel |

Terminal rows are not deleted and are not rewritten. A finish whose `generation` or lease owner does not match changes nothing, so a stale attempt cannot overwrite a newer result.

One active row per `(org, repo, selector, sha)`. A duplicate schedule returns that row. Two workers claiming it serialize on the row lock; the loser finds it no longer `scheduled`.

Backoff and leases are the Stage 6 job runtime. The execution lease is the same length. Tests advance a clock. They do not sleep.

## Evaluation runtime arguments

Only when mode is `evaluation` and the runtime binary exists. Otherwise the adapter returns `runtime_unavailable` or `execution_closed` and does not run the approved command on the host.

The argument list is built in code. It always includes `--network=none`, `--cap-drop=ALL`, `--security-opt=no-new-privileges`, `--read-only`, a non-root `--user`, `--pids-limit`, `--memory`, `--cpus`, a nosuid/nodev/noexec `--tmpfs` for `/tmp`, and the digest-pinned image. It never includes `--privileged`, the runtime socket, host networking, or a payload-chosen command. The process environment of the runtime client is `PATH` only, so control-plane variables are not inherited. The container receives `SELECTOR` and `FLAKEPILOT_SHA` only.

`--network=none` is how metadata endpoints and every other egress are unreachable. Nothing inside the sandbox can change that flag.

`--rm` removes the container when the client exits. A worker killed with `SIGKILL` can leave a container behind. A TTL reaper is not implemented. That gap is why evaluation mode is not a production guarantee.

## Evidence

Outputs are size-capped. The result must be a JSON object whose only keys are `status` (`passed` or `failed`) and `selector` (equal to the request). Any other key, including `verified`, `flaky`, `outcome`, `tenant`, or `handler`, is `invalid_result`. The control plane stores the canonical JSON it accepted, plus an optional PNG screenshot under the existing artifact store (`owner=exec`). Keys are content-addressed and org/repo scoped. There is no public artifact route.

Hashes are SHA-256 of the stored bytes. Filenames never come from the sandbox. A crash or a failed artifact write still persists the terminal outcome, with `evidence_partial` set when a file was dropped.

Artifacts are not encrypted at rest. Nothing in this stage deletes them. Retention durations are not invented here.

## Audit

The execution change and one `exec.outbox` envelope commit together. The relay copies it to `aud.events`. The envelope carries the execution id, the outcome, and a short reason. It does not carry source, logs, or selectors. Delivery stays at-least-once. Polling stays sufficient. `LISTEN/NOTIFY` is not added.

## Not in this stage

Audit query, outbox pruning, retention deletion, investigation workflows beyond this single execution, patch verification, live AI, and a microVM sandbox. Reasons: no approved retention rule, and ADR-008's production host split is an operator deployment this repository does not provision.

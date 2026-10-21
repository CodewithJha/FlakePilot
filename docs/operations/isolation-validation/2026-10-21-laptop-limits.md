# Isolation measurement, 21 October 2026, developer laptop, disk and expiry

This page records a second run on the same class of machine as [2026-10-21-laptop.md](2026-10-21-laptop.md). It is a developer laptop. It is not the approved production execution host. The boundary is Docker Desktop's LinuxKit VM plus a container. It is not a per-run microVM. `IsolationChecks` stay false. Evidence stays ineligible. Untrusted execution stays disabled.

## What this environment is

| Item | Value |
|---|---|
| Date | 21 October 2026, about 23:35 IST |
| Host | Developer Mac, macOS 26.6.2, arm64 |
| Engine | Docker Desktop 29.5.3 |
| VM | LinuxKit, kernel 6.12.76-linuxkit |
| Image | `alpine@sha256:d9e853e87e55526f6b2917df91a2115c36dd7c696a35be12163d44e6e2a4b6bc` |

This laptop had no FlakePilot database, session secret, or GitHub App key. No repository code ran inside the container. The product worker was not running. `LabelledContainerReaper` was not the process that swept containers. A host shell applied the same rule: remove a container only when `flakepilot.expires` is at or before the sweep clock.

## Flags

```text
--network=none --read-only --user 65534:65534 --cap-drop=ALL
--security-opt no-new-privileges --pids-limit 64 --memory 128m
--memory-swap 128m --cpus 0.5 --label flakepilot.execution=probe
```

The disk runs added `--tmpfs` with the size under test. The product argument list uses `size=64m` on `/tmp`. That spelling was one of the four measured here.

## Disk

The probe appended 64 KiB until a write failed. It did not stop after one `write`.

| tmpfs size | mount line | first failing chunk | bytes stored | df |
|---|---|---|---|---|
| `size=1m` | `size=1024k` | 16 | 1048576 | 100% |
| `size=1024k` | `size=1024k` | 16 | 1048576 | 100% |
| `size=1048576` | `size=1024k` | 16 | 1048576 | 100% |
| `size=64m` (product setting) | `size=65536k` | 1024 | 67108864 | 100% |

Writes past the cap failed. Stored bytes did not exceed the cap. On this runtime, disk is **PASS** for those four settings, including the product's `size=64m`.

The GitHub-hosted runner's earlier line `disk_errno=0` came from one `write` of 2 MB. That call can return a short count without raising, so it did not prove the cap was missing. The runner has not re-run the chunked probe. Disk on the runner stays **not re-measured**.

## Wall time and orphan cleanup

Two containers. A was labelled `flakepilot.expires` about four seconds ahead. B, the active container, was labelled about 3600 seconds ahead. The docker client for A was then killed with SIGKILL. A was still running. That reproduces the orphan.

A separate host shell swept by the expiry label:

- The first sweep removed one container, A. It ran about four seconds in, at or after A's expiry. Container start ate the window, so a sweep strictly before expiry was not shown.
- The second sweep removed zero. About seven seconds in, A was gone and B was still running.
- Another sweep removed zero. The sweep is idempotent.
- Two sweeps at the same time on one expired container: one removed it and the other removed zero. Nothing was left.

On this runtime, client-independent expiry cleanup is **PASS**: the orphan was removed, the active container was left alone, a repeat removed nothing, and two concurrent sweeps removed the expired container once. It is not a test of `LabelledContainerReaper`. It is not a test of a sweep that runs while the deadline is still ahead. Docker itself still has no deadline. The runner has not re-run this supervisor.

## Not tested

- Host restart.
- Worker-process death with the product reaper wired in.
- Cancellation racing completion inside a runtime.
- The GitHub-hosted runner, again.

These results do not set `IsolationChecks`.

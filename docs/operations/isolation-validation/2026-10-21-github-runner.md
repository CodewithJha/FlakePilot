# Isolation measurement, 21 October 2026, GitHub-hosted runner

This page records what a GitHub-hosted runner actually did. It is not a FlakePilot execution host. The runner is a GitHub-managed VM. It has its own network. Only a container started by the harness would be under the probe flags, and this attempt did not start one. `IsolationChecks` stay false. Evidence stays ineligible. Untrusted execution stays disabled.

## Attempt 1

| Item | Value |
|---|---|
| Workflow | [isolation-validation run 39641727493](https://github.com/CodewithJha/FlakePilot/actions/runs/39641727493) |
| Commit | `0d98f115a897aca62460876aca09ee7e6ca92a32` |
| Conclusion | cancelled |
| Runner kernel | Linux `7.0.0-1017-azure` x86_64, from `uname -srm` in the log |
| Docker engine | 28.0.4, API 1.48, linux/amd64 |
| cgroup | v2, driver systemd |
| Security | apparmor, seccomp profile `builtin`, cgroup namespace |
| Image pull target | `python@sha256:e8a38829209aea82774882e5157e1f7c3e8c8393ff53a697b3580896e57f345c` (linux/amd64 manifest of `python:3.12-slim`) |

The log shows the four image layers downloaded and each line `Pull complete` by 17:12:42 UTC. The `docker pull` process was still running at 17:32:49 UTC, when the job's 20-minute limit cancelled it. The harness then had no container output.

Checks 1 through 10: **not executed.** No probe container was started. The digest was not confirmed with `docker image inspect` because the pull command had not returned.

The job cleared `GITHUB_TOKEN` on the probe step after checkout. That does not remove the credential from the runner agent. No container was available to test.

## Attempt 2

| Item | Value |
|---|---|
| Workflow | [isolation-validation run 39645626084](https://github.com/CodewithJha/FlakePilot/actions/runs/39645626084) |
| Commit | `5a47fe80df3e4653ef41971d6f8746b97a35f6f4` |
| Conclusion | failure, exit code 1. The harness printed `HARNESS complete fail=1` after every check. The red status is those failed checks, not a missing log |
| Runner kernel | Linux `6.17.0-1022-azure` x86_64 |
| Docker engine | 28.0.4, API 1.48, linux/amd64 |
| runc | 1.5.2 |
| containerd | v2.4.1 |
| cgroup | v2, driver systemd |
| Security | apparmor, seccomp profile `builtin`, cgroup namespace |
| Image pull | `pull_exit=0` |
| Repo digest | `python@sha256:e8a38829209aea82774882e5157e1f7c3e8c8393ff53a697b3580896e57f345c` |
| Image id | `sha256:a33c10a97f11005523814c6eee013986119fe41f3122f444518117536675c8e1` |
| Classify interpreter | CPython 3.12.13 on the runner, not inside the container |

The probe step set `GITHUB_TOKEN` empty after checkout. The container's environment keys were `GPG_KEY`, `HOME`, `HOSTNAME`, `LANG`, `PATH`, `PYTHON_SHA256`, and `PYTHON_VERSION`. Those names come from the image plus the two variables the harness set. No token, secret, password, database, or cloud key name was present. `GPG_KEY` is baked into the official Python image. The runner agent outside the container still has its own credential. That was not measured.

Flags for the probes: `--network=none`, `--cap-drop=ALL`, `--security-opt=no-new-privileges`, `--read-only`, `--user=65534:65534`, `--cpus=0.5`, a tmpfs on `/tmp`, and no host mount except the two per-directory mounts in check 10. The repository and the docker socket were not mounted. The probe file was passed on stdin.

1. **Outbound network.** Expected: no connection to a public address. Observed: `1.1.1.1:443` and `8.8.8.8:53` both `blocked errno=101` (network unreachable). Loopback `127.0.0.1:9` was `blocked errno=111` (connection refused), so the loopback interface exists. **PASS** for the `--network=none` policy. A firewall that allows some destinations and denies others was not tested.

2. **Metadata and control-plane endpoints.** Expected: metadata addresses and the control plane are unreachable. Observed: `169.254.169.254:80` and `168.63.129.16:80` both `blocked errno=101`. The harness had no control-plane address to dial. **PARTIAL.** Metadata blocked. Control plane **not executed.**

3. **Host filesystem.** Expected: the host marker, the runner home, and the docker socket are absent. Observed: marker absent, `/home/runner` absent, both docker socket paths absent, `/var/lib/docker` absent. `graph_path=1`: `/proc/self/mountinfo` contained the string `/var/lib/docker` or `docker.sock`. The matching directory and sockets were still absent, so the path string was not a usable mount. **PASS** for access, with that mountinfo limitation.

4. **Credentials and sockets.** Expected: no control-plane secret and no runtime socket. Observed: `bad=none`, `leak=none`, both sockets absent. **PASS** for the container. The runner agent outside the container was not inspected.

5. **Privileges.** Expected: non-root, no capabilities, no new privileges, mount denied. Observed: uid 65534, gid 65534, `CapEff=0000000000000000`, `NoNewPrivs=1`, `setuid` errno 1, `mount` errno 1, mount-namespace `unshare` errno 1, user-namespace `unshare` errno 1. **PARTIAL.** The configured policy held, including denial of a new user namespace on this kernel. Seccomp is Docker's built-in profile, not a reviewed profile. The container shares the runner kernel. This is not a microVM.

6. **Limits.**
   - Disk: tmpfs `size=1m`, then one 2 MB `write`. Observed: `disk_errno=0`. That call can return a short count without raising, so this line does not prove the cap was missing and does not prove it held. The chunked probe was not run on this runner. Disk here is **not re-measured**. A later laptop run of the chunked probe is a different machine. See [2026-10-21-laptop-limits.md](2026-10-21-laptop-limits.md).
   - PIDs: `--pids-limit=16`. Observed: 15 children, then `stop_errno=11` (EAGAIN). **PASS.**
   - CPU: `--cpus=0.5`. Observed: `cpu_max=50000_100000`. **PASS.**
   - Memory: `--memory=64m` and `--memory-swap=64m`. The swap flag was accepted. A 256 MB allocation ended `memory_exit=137` and `OOM true 137`. **PASS.**
   - Wall time: a container running `sleep` for 120 seconds was still `running` after 5 seconds. Docker did not stop it. The harness removed it after recording that state. **FAIL.**

   Overall check 6 on this run: **FAIL**, because wall time was not enforced. Disk is **not re-measured**, not a proven cap failure.

7. **Malformed and oversized output.** The container printed the bytes. The runner called `classify` from `execution.domain.reproduction`. Observed: extra key `verified` -> `invalid_result`; 9071-byte body -> `invalid_result`; `{not-json` -> `invalid_result`; the legal two-key body -> `success passed`. **PASS** for that function. It is not a kernel check.

8. **Cleanup.**
   - `--rm` and a normal exit: container gone. **PASS.**
   - `docker kill` of a detached `--rm` container: container gone. **PASS.**
   - SIGKILL of the docker client while `--rm` was set: container still `running`. **FAIL.** Same orphan the laptop recorded.
   - `docker kill` without `--rm`: container still present, status `exited`. **FAIL.**
   - `docker stop` of a detached `--rm` container: container gone. **PASS.**
   - Host restart: **not executed.**

   The harness then removed names beginning with `fp-probe-`. That removal is the script's own cleanup, not the product reaper. Overall check 8: **FAIL.**

9. **Stale worker.** **Not executed.** The database URL was not placed on the runner.

10. **Tenant scope.** Two containers, each with only its own output directory mounted. The second could not read the first's file (`errno` 2). The host SHA-256 of the written file matched the digest the first container printed: `4f8f8e1265f588fed771cc3c9cb199fd83c87e5f2fd7a56fff38a8ac9053adc0`. **PARTIAL.** Mount isolation and the hash held. Application tenant authorization was **not executed.** There is no FlakePilot API on this runner.

## What this environment is not

A GitHub-hosted runner is not a machine FlakePilot owns. It is not a per-run microVM. A later attempt on the same class of runner, if the pull returns, is still only a measurement of Docker flags on GitHub's VM.

## Remediation not re-run

Commit `0be5da6bb2b16c80d19f76548b189ce6a62873d0` changes the harness and makes the workflow `workflow_dispatch` only. Pushing that commit did not start this workflow. Creating a dispatch event returned HTTP 403. The credential available to this process cannot start the manual job. No new probe log exists.

The disk change writes 2 MB in 64 KB pieces and treats a stored 2 MB file as a failure. Attempt 2 used one `write` of 2 MB. The runner has still not run the chunked probe. A developer laptop later did, and that record is [2026-10-21-laptop-limits.md](2026-10-21-laptop-limits.md). It does not replace this runner log.

The wall-time change does not ask Docker for a deadline. It starts one container whose `flakepilot.expires` is already past and one whose expiry is still ahead, then runs `LabelledContainerReaper` as a separate process. That product reaper was not executed on this runner. The unit test uses a fake CLI. Attempt 2 remains the evidence on this runner that Docker left the sleep running after 5 seconds and that SIGKILL of the client left the container running. The laptop page records a host shell that swept by the same expiry rule. That shell was not this harness.

`IsolationChecks` stay false.

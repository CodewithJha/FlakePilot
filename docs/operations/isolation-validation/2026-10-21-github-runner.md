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

## What this environment is not

A GitHub-hosted runner is not a machine FlakePilot owns. It is not a per-run microVM. A later attempt on the same class of runner, if the pull returns, is still only a measurement of Docker flags on GitHub's VM.

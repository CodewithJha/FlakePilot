# Isolation measurement, 22 October 2026, developer laptop, reaper

This page records one run of `ops/isolation/local_mac_probe.sh`. It is a developer laptop. Docker Desktop's shared LinuxKit VM is the boundary. It is not a per-run microVM. It is not production. `IsolationChecks` stay false. Evidence stays ineligible. Untrusted execution stays disabled.

The product worker was not running. The probe imports `FileLedger`, `HostGate`, and `LabelledContainerReaper` in a separate process. It does not read a database URL or an application key. The log below is what that process printed. The four FAIL lines are kept as FAIL.

## What this environment is

| Item | Value |
|---|---|
| Date | 22 October 2026, 12:19:03–12:24:04 IST |
| Commit | `110a3e63449c60dc01bd2e76156c8a82528f3371`, fresh clone |
| Command | `bash ops/isolation/local_mac_probe.sh` |
| Run | `m17926517442493` |
| Host | Developer laptop, Darwin 25.6.0 arm64, macOS 26.6.2 |
| Engine | Docker Desktop 29.5.3, linux/arm64, emulation=no |
| VM | LinuxKit, kernel 6.12.76-linuxkit, shared by every container |
| Image | `alpine@sha256:d9e853e87e55526f6b2917df91a2115c36dd7c696a35be12163d44e6e2a4b6bc` |

An earlier start the same morning did not reach Docker (`pull_exit=1`). The log below is the run after the daemon was up. After this run the laptop still had six containers that were already there. `foreign_containers_unchanged` counted zero containers labelled `flakepilot.execution` outside this run.

## Log

```text
PROBE run=m17926517442493 microvm=false production=false
PRODUCT execution_enabled=false isolation_checks=false
PROBE microvm=false production=false shared_vm=true
UNAME Darwin 25.6.0 arm64
SW_VERS 26.6.2
DOCKER 29.5.3 linux/arm64 6.12.76-linuxkit
CHECK reaper_register PASS host_gate
pull_exit=0
CHECK image_digest PASS pinned
CHECK image_arm64 PASS image=linux arm64 emulation=no
CHECK reaper_future_kept PASS ledger_future
CHECK reaper_expired_removed PASS reclaimed=1
CHECK reaper_sigkill_client_remains PASS still_running
CHECK reaper_independent_removes_orphan PASS reclaimed=1
CHECK reaper_second_sweep PASS expired_containers_removed
CHECK reaper_concurrent_sweeps PASS reclaimed=1
CHECK reaper_missing_ledger PASS runtime_reaper_unavailable
CHECK reaper_symlink_ledger PASS runtime_reaper_unavailable
CHECK reaper_untrusted_ledger PASS runtime_reaper_unavailable
CHECK reaper_malformed_ledger PASS runtime_reaper_unavailable
CHECK disk_64m_chunked FAIL end=None extra=None loops=None mount=missing
CHECK disk_short_write FAIL end=None short=None last=None extra=None
CHECK cpu_throttled PASS ratio=0.500 cpu_max=50000_100000
CHECK memory_oom_status FAIL true 0
CHECK pids_limited FAIL pids_ok=None
CHECK wall_supervisor_sigkill PASS reclaimed=3
CHECK wall_launcher_death PASS container_remained
CHECK output_small_accepted PASS success
CHECK output_size_cap PASS bytes=9071 exit=0
CHECK output_malformed PASS exit=0
CHECK foreign_containers_unchanged PASS count=0
CHECK reaper_leftovers PASS count=0
PROBE complete fail=4
```

## What passed

Twenty-one CHECK lines passed and four failed. That includes every reaper lifecycle check that uses the real `LabelledContainerReaper`, `FileLedger`, and `HostGate`: register through the host gate, a future container kept, an expired container removed, a client SIGKILL that left the container running, an independent sweep that removed the orphan, a second sweep that removed nothing further, concurrent sweeps that removed one, and a missing, symlink, loose, or malformed ledger that removed nothing. CPU quota was half a core. A small result was accepted, an oversized result was rejected, and a malformed result was rejected. No labelled container from this run was left behind.

`wall_supervisor_sigkill` reclaimed 3. One of those is the wall-clock container. The other two match the disk and short-write clients, which this probe's 40 second deadline killed before they printed a line. `--rm` does not apply once the client is killed, so those containers were still there for the later sweep. That reclaim count is not a reaper failure.

## What failed, and why it is not a boundary result

These four lines are FAIL. They are not relabelled.

| Check | Printed line | What the driver did |
|---|---|---|
| `disk_64m_chunked` | `end=None extra=None loops=None mount=missing` | `mount=missing` is the driver's default when the guest printed no `mount` field. `loops=None` is not a count of zero. No `RESULT` line was captured. |
| `disk_short_write` | `end=None short=None last=None extra=None` | Same. A guest that reached `last=0` would have printed `0`. |
| `memory_oom_status` | `true 0` | Docker's inspect format is `OOMKilled` then `ExitCode`. The cgroup killer ran. Exit code 0 is the shell, because the guest script continued after `dd` and printed `oom_finished=1`. The checker requires the guest not to finish. Exit 0 is why this line is FAIL. |
| `pids_limited` | `pids_ok=None` | The key was absent. A count of zero would have been `pids_ok=0`. Busybox ash aborts the script when `fork` fails, so the `RESULT` line never ran. |

The disk and short-write scripts spawned one `dd` and one `wc` per chunk, about 1100 and 1300 times, on 0.5 CPU. The driver waited 40 seconds and, on timeout, discarded stdout. That is a probe bug. It does not measure the tmpfs cap. The 21 October record on this class of laptop, [2026-10-21-laptop-limits.md](2026-10-21-laptop-limits.md), stored exactly 67108864 bytes at `size=64m` and failed the next write. This run does not repeat that measurement and does not overturn it.

`true 0` is not evidence that the memory limit was absent. It is evidence the killer ran and the script still exited 0. A pass requires `OOMKilled` true and exit 137, with no completion line. This run does not have that.

`pids_ok=None` is not a measured pid count. The limit may have stopped `fork`. This probe did not record how many processes were alive.

## Not claimed

- Disk cap, short write, OOM exit status, and the pid limit are not measured by this log.
- The corrected probe was rerun later the same morning. That measurement is [2026-10-22-laptop-measured.md](2026-10-22-laptop-measured.md). It does not change these four FAIL lines.
- Host restart was not run.
- This is not the GitHub-hosted runner and not a dedicated execution host.

These results do not set `IsolationChecks`.

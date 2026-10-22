# Isolation measurement, 22 October 2026, developer laptop, measured probe

This page records the rerun of `ops/isolation/local_mac_probe.sh`. It is a developer laptop. Docker Desktop's shared LinuxKit VM is the boundary. It is not a per-run microVM. It is not production. `IsolationChecks` stay false. Evidence stays ineligible. Untrusted execution stays disabled.

The product worker was not running. The probe imports `FileLedger`, `HostGate`, and `LabelledContainerReaper` in a separate process. It does not read a database URL or an application key. The earlier log the same morning is [2026-10-22-laptop-reaper.md](2026-10-22-laptop-reaper.md). That log's four FAIL lines stay FAIL. This page is the later run, after the probe was changed to keep a result.

## What this environment is

| Item | Value |
|---|---|
| Date | 22 October 2026, 12:39:01–12:39:22 IST |
| Commit | `4898aa7dd63b3b2c0a1373dcea5d5f4af942973b`, fresh clone |
| Command | `bash ops/isolation/local_mac_probe.sh` |
| Run | `m17926529412864` |
| Host | Developer laptop, Darwin 25.6.0 arm64, macOS 26.6.2 |
| Engine | Docker Desktop 29.5.3, linux/arm64, emulation=no |
| VM | LinuxKit, kernel 6.12.76-linuxkit, shared by every container |
| Image | `alpine@sha256:d9e853e87e55526f6b2917df91a2115c36dd7c696a35be12163d44e6e2a4b6bc` |

Docker Desktop was already running. The laptop had six containers before the run and six after. `foreign_containers_unchanged` counted zero containers labelled `flakepilot.execution` outside this run.

## Log

```text
PROBE run=m17926529412864 microvm=false production=false
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
CHECK disk_64m_chunked PASS end=67108864 extra=0 mount=rw_nosuid_nodev_noexec_relatime_size=65536k
CHECK disk_short_write PASS end=67108864 short=1 last=28864 extra=0
CHECK cpu_throttled PASS ratio=0.500 cpu_max=50000_100000
CHECK memory_oom_status PASS true 137
CHECK pids_limited PASS pids_ok=14
CHECK wall_supervisor_sigkill PASS reclaimed=1
CHECK wall_launcher_death PASS container_remained
CHECK output_small_accepted PASS success
CHECK output_size_cap PASS bytes=9071 exit=0
CHECK output_malformed PASS exit=0
CHECK foreign_containers_unchanged PASS count=0
CHECK reaper_leftovers PASS count=0
PROBE complete fail=0
```

## What this run measured

Every CHECK line passed. The probe printed `fail=0`. There are 25 CHECK lines.

| Check | Result |
|---|---|
| Disk, `size=64m` | Stored exactly 67108864 bytes. The extra byte added 0. The mount line contains `size=65536k`. |
| Short write | Stopped at 67108864. The last chunk was 28864 bytes, `short=1`, and the extra byte added 0. |
| CPU | `cpu.max` was `50000 100000`, ratio 0.500. |
| Memory | Docker reported `OOMKilled=true` and exit 137. |
| PIDs | `--pids-limit 16` left `pids_ok=14` alive. |
| Wall time | After SIGKILL of the client, the independent supervisor reclaimed 1 container. |
| Launcher death | The container remained after the launcher died. |
| Output | A small result was accepted. 9071 bytes were rejected. Malformed JSON was rejected. |
| Reaper | Register, future kept, expired removed, client SIGKILL left the container, the independent sweep removed the orphan, the second sweep removed nothing further, concurrent sweeps removed one, and a missing, symlink, loose, or malformed ledger removed nothing. |
| Cleanup | No container from this run remained. Foreign `flakepilot.execution` containers were unchanged, count 0. |

`wall_supervisor_sigkill` reclaimed 1. The disk and short-write clients finished and were removed by `--rm`, so they were not left for that sweep.

## Not claimed

- Host restart was not run.
- This is not the GitHub-hosted runner. Disk on that runner stays not re-measured.
- This is not a dedicated execution host and not a per-run microVM.

These results do not set `IsolationChecks`.

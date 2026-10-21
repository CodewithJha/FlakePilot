#!/bin/bash
# Disposable measurement of container flags. Not a FlakePilot execution host.
# The product worker does not call this script. Isolation checks stay false.
set -euo pipefail

DIR=$(cd "$(dirname "$0")" && pwd)
ROOT=$(cd "$DIR/../.." && pwd)
PROBE="$DIR/probe.py"
# linux/amd64 manifest of python:3.12-slim. The index digest is
# sha256:cb33f7f771ecec2ef1225cf39b159591a040391bd73e4c01f00fdfc4c47611d2
# and is not the pull target.
IMAGE=python@sha256:e8a38829209aea82774882e5157e1f7c3e8c8393ff53a697b3580896e57f345c
PIN=sha256:e8a38829209aea82774882e5157e1f7c3e8c8393ff53a697b3580896e57f345c
TMPFS=/tmp:rw,nosuid,nodev,noexec,size=64m
# Three spellings of a 1 MB cap. The probe must observe ENOSPC before 2 MB
# is stored. A spelling that does not cap is a failed measurement, not a pass.
DISK_SUFFIX=/tmp:rw,nosuid,nodev,noexec,size=1m
DISK_K=/tmp:rw,nosuid,nodev,noexec,size=1024k
DISK_BYTES=/tmp:rw,nosuid,nodev,noexec,size=1048576
LIVE_EXECUTION=11111111-1111-4111-8111-111111111111
DEAD_EXECUTION=22222222-2222-4222-8222-222222222222
ORPHAN_EXECUTION=33333333-3333-4333-8333-333333333333
STOPPED_EXECUTION=44444444-4444-4444-8444-444444444444
EIGHT_LIVE=55555555-5555-4555-8555-555555555555
LEDGER=/tmp/fp-host-ledger
rm -f "$LEDGER"
fail=0
PY=""

echo "PRODUCT execution_enabled=false isolation_checks=false"
echo "LAB github-hosted-runner separate_from_control_plane=true"

if ! command -v docker >/dev/null 2>&1; then
  echo "HARNESS docker missing"
  exit 1
fi

cleanup() {
  local id
  while read -r id; do
    if [ -n "$id" ]; then
      docker rm -f "$id" >/dev/null 2>&1 || true
    fi
  done < <(docker ps -aq --filter name=fp-probe- || true)
  true
}
trap cleanup EXIT

echo "=== runtime ==="
uname -srm || true
docker version || true
docker info --format 'Server={{.ServerVersion}} Cgroup={{.CgroupVersion}} Driver={{.CgroupDriver}} Security={{json .SecurityOptions}}' || true

echo "=== image ==="
set +e
timeout 90 docker pull -q "$IMAGE"
pull_code=$?
set -e
echo "pull_exit=$pull_code"
if ! docker image inspect "$IMAGE" >/dev/null 2>&1; then
  echo "HARNESS image not present"
  echo "CHECK 1 outbound_network NOT_EXECUTED image_pull_did_not_finish"
  echo "CHECK 2 metadata_endpoints NOT_EXECUTED image_pull_did_not_finish"
  echo "CHECK 3 host_mounts NOT_EXECUTED image_pull_did_not_finish"
  echo "CHECK 4 credentials NOT_EXECUTED image_pull_did_not_finish"
  echo "CHECK 5 privileges NOT_EXECUTED image_pull_did_not_finish"
  echo "CHECK 6 limits NOT_EXECUTED image_pull_did_not_finish"
  echo "CHECK 7 outputs NOT_EXECUTED image_pull_did_not_finish"
  echo "CHECK 8 cleanup NOT_EXECUTED image_pull_did_not_finish"
  echo "CHECK 9 stale_worker NOT_EXECUTED image_pull_did_not_finish"
  echo "CHECK 10 artifact_scope NOT_EXECUTED image_pull_did_not_finish"
  echo "HARNESS complete fail=1"
  exit 1
fi
digests=$(docker image inspect --format '{{json .RepoDigests}}' "$IMAGE")
identity=$(docker image inspect --format '{{.Id}} {{.Os}}/{{.Architecture}}' "$IMAGE")
echo "REPO_DIGESTS $digests"
echo "IMAGE_ID $identity"
case "$digests" in
*"$PIN"*) ;;
*)
  echo "HARNESS digest mismatch"
  exit 1
  ;;
esac

if command -v uv >/dev/null 2>&1; then
  set +e
  uv python install 3.12
  PY=$(uv python find 3.12)
  set -e
fi
if [ -z "${PY}" ]; then
  echo "HARNESS python3.12 missing"
else
  echo "PYTHON $PY"
  "$PY" -c 'import sys; print(sys.version)'
fi

printf '%s\n' 'flakepilot-host-only' >/tmp/fp-host-marker
if [ -d /home/runner ]; then
  printf '%s\n' 'runner-only' >/home/runner/fp-host-secret || true
fi

base_args=(
  --pull=never
  --network=none
  --cap-drop=ALL
  --security-opt=no-new-privileges
  --read-only
  --user=65534:65534
  -e PATH=/usr/local/bin:/usr/bin:/bin
  -e HOME=/tmp
)

result() {
  local number=$1
  local name=$2
  local status=$3
  shift 3
  echo "CHECK $number $name $status $*"
  if [ "$status" = "FAIL" ]; then
    fail=1
  fi
}

run_probe() {
  local name=$1
  local mode=$2
  local pids=$3
  local memory=$4
  local tmpfs=$5
  shift 5
  docker rm -f "$name" >/dev/null 2>&1 || true
  timeout 30 docker run -i --name "$name" \
    "${base_args[@]}" \
    --pids-limit="$pids" \
    --memory="$memory" \
    --cpus=0.5 \
    --tmpfs="$tmpfs" \
    "$@" \
    "$IMAGE" \
    python -u - "$mode" <"$PROBE"
}

capture_probe() {
  local errf=/tmp/fp-probe-err
  : >"$errf"
  set +e
  cap_out=$(run_probe "$@" 2>"$errf")
  cap_code=$?
  set -e
  if [ -s "$errf" ]; then
    echo "stderr $(tr '\n' ' ' <"$errf")" >&2
  fi
  echo "exit=$cap_code" >&2
  printf '%s\n' "$cap_out"
}

has() {
  # A pipe into grep -q returns SIGPIPE under pipefail even when the needle matches.
  case "$cap_out" in
    *"$1"*) return 0 ;;
    *) return 1 ;;
  esac
}

disk_capped() {
  # ENOSPC, and the 2 MB target was not stored. A short write with errno 0 is a fail.
  case "$cap_out" in
    *'disk_errno=28'*) ;;
    *) return 1 ;;
  esac
  case "$cap_out" in
    *'written=2097152'*|*'size=2097152'*) return 1 ;;
  esac
  case "$cap_out" in
    *'written='*) return 0 ;;
  esac
  return 1
}

host_register() {
  local execution=$1
  local expires=$2
  if [ -z "$PY" ]; then
    echo "ledger skipped" >&2
    return 0
  fi
  PYTHONPATH="$ROOT/src" "$PY" - "$LEDGER" "$execution" "$expires" <<'PY'
import sys
from pathlib import Path

from flakepilot.execution.infrastructure.ledger import FileLedger

FileLedger(Path(sys.argv[1])).register(sys.argv[2], int(sys.argv[3]))
PY
}

reap_now() {
  if [ -z "$PY" ]; then
    echo "reaped=skipped reason=no_python"
    return 0
  fi
  set +e
  PYTHONPATH="$ROOT/src" "$PY" - "$LEDGER" <<'PY'
import sys
import time
from pathlib import Path

from flakepilot.execution.infrastructure.ledger import FileLedger
from flakepilot.execution.infrastructure.reaper import LabelledContainerReaper

result = LabelledContainerReaper(
    now=lambda: int(time.time()),
    authority=FileLedger(Path(sys.argv[1])),
).reclaim()
print(f"reaped={result.reclaimed} reason={result.reason}")
PY
  code=$?
  set -e
  if [ "$code" -ne 0 ]; then
    echo "reaped=error reason=python_exit_$code"
  fi
}

start_labelled() {
  local name=$1
  local execution=$2
  local expires=$3
  local _i state
  host_register "$execution" "$expires"
  docker rm -f "$name" >/dev/null 2>&1 || true
  docker run -d --name "$name" \
    "${base_args[@]}" \
    --pids-limit=32 \
    --memory=64m \
    --cpus=0.5 \
    --tmpfs="$TMPFS" \
    --label "flakepilot.execution=$execution" \
    --label "flakepilot.expires=$expires" \
    "$IMAGE" \
    python -c 'import time; time.sleep(120)' >/dev/null
  for _i in 1 2 3 4 5 6 7 8 9 10; do
    state=$(docker inspect -f '{{.State.Running}}' "$name" 2>/dev/null || true)
    if [ "$state" = "true" ]; then
      return 0
    fi
    sleep 1
  done
  echo "labelled $name did not start" >&2
}

echo "=== check 1 outbound network ==="
capture_probe fp-probe-net network 32 128m "$TMPFS" >/tmp/fp-check1.out
cat /tmp/fp-check1.out
if has 'net_1_1_1_1=blocked' && has 'net_8_8_8_8=blocked'; then
  result 1 outbound_network PASS "policy=network_none selective_firewall=not_tested lo_observed=see_log"
else
  result 1 outbound_network FAIL "external_connect_was_not_blocked"
fi

echo "=== check 2 metadata ==="
capture_probe fp-probe-meta metadata 32 128m "$TMPFS" >/tmp/fp-check2.out
cat /tmp/fp-check2.out
if has 'meta_imds=blocked' && has 'meta_azure=blocked'; then
  result 2 metadata_endpoints PARTIAL "metadata=blocked control_plane=NOT_EXECUTED no_control_plane_address policy=network_none"
else
  result 2 metadata_endpoints FAIL "metadata_address_was_reachable"
fi

echo "=== check 3 host mounts ==="
capture_probe fp-probe-mounts mounts 32 128m "$TMPFS" >/tmp/fp-check3.out
cat /tmp/fp-check3.out
if has 'marker_content=absent' && has 'sock_docker=0' && has 'sock_run=0' \
  && has 'home_runner=0' && has 'runner_file=0' && has 'docker_dir=0'; then
  result 3 host_mounts PASS "host_marker_and_sockets_absent graph_path_in_mountinfo=see_log"
else
  result 3 host_mounts FAIL "host_path_or_socket_visible"
fi

echo "=== check 4 credentials ==="
capture_probe fp-probe-creds creds 32 128m "$TMPFS" >/tmp/fp-check4.out
cat /tmp/fp-check4.out
if has 'bad=none' && has 'leak=none' && has 'sock_docker=0' && has 'sock_containerd=0'; then
  result 4 credentials PASS "container_env_had_no_token_or_socket runner_agent_holds_its_own_credential_outside_this_measurement"
else
  result 4 credentials FAIL "sensitive_env_or_socket_visible"
fi

echo "=== check 5 privileges ==="
capture_probe fp-probe-privs privs 32 128m "$TMPFS" >/tmp/fp-check5.out
cat /tmp/fp-check5.out
priv_caps=0
case "$cap_out" in
*'capeff=0000000000000000'*) priv_caps=1 ;;
esac
while IFS= read -r line; do
  if [ "$line" = "RESULT capeff=0" ]; then
    priv_caps=1
  fi
done < <(printf '%s\n' "$cap_out")
if [ "$priv_caps" -eq 1 ] && has 'uid=65534' && has 'nonewprivs=1' \
  && has 'setuid_errno=1' && has 'mount=errno=1'; then
  result 5 privileges PARTIAL "configured_policy_held seccomp=docker_default_unreviewed userns=see_log shared_kernel=true"
else
  result 5 privileges FAIL "uid_caps_or_mount_not_reduced"
fi

echo "=== check 6 disk ==="
disk_status=FAIL
disk_suffix=FAIL
disk_k=FAIL
disk_bytes=FAIL
capture_probe fp-probe-disk disk 32 128m "$DISK_SUFFIX" >/tmp/fp-check6d.out
cat /tmp/fp-check6d.out
if disk_capped; then
  disk_suffix=PASS
  disk_status=PASS
fi
echo "disk_size_1m=$disk_suffix"
capture_probe fp-probe-diskk disk 32 128m "$DISK_K" >/tmp/fp-check6dk.out
cat /tmp/fp-check6dk.out
if disk_capped; then
  disk_k=PASS
  disk_status=PASS
fi
echo "disk_size_1024k=$disk_k"
capture_probe fp-probe-diskb disk 32 128m "$DISK_BYTES" >/tmp/fp-check6db.out
cat /tmp/fp-check6db.out
if disk_capped; then
  disk_bytes=PASS
  disk_status=PASS
fi
echo "disk_size_1048576=$disk_bytes"
echo "disk=$disk_status"

echo "=== check 6 pids ==="
capture_probe fp-probe-pids pids 16 128m "$TMPFS" >/tmp/fp-check6p.out
cat /tmp/fp-check6p.out
if has 'stop_errno=11' || has 'stop_errno=12' || [ "$cap_code" -eq 137 ]; then
  pids_status=PASS
else
  pids_status=FAIL
fi
echo "pids=$pids_status code=$cap_code"

echo "=== check 6 cpu ==="
capture_probe fp-probe-cpu cpu 32 128m "$TMPFS" >/tmp/fp-check6c.out
cat /tmp/fp-check6c.out
if has 'cpu_max=50000_100000' || has 'cpu_quota=50000'; then
  cpu_status=PASS
else
  cpu_status=FAIL
fi
echo "cpu=$cpu_status"

echo "=== check 6 memory ==="
swap_args=()
set +e
timeout 20 docker run --rm --name fp-probe-swap --pull=never --network=none --cap-drop=ALL \
  --security-opt=no-new-privileges --user=65534:65534 \
  --memory=64m --memory-swap=64m --pids-limit=32 \
  "$IMAGE" python -c 'print("swap-ok")' >/tmp/fp-swap.out 2>/tmp/fp-swap.err
swap_code=$?
set -e
if [ "$swap_code" -eq 0 ]; then
  swap_args=(--memory-swap=64m)
  echo "MEMORY_SWAP supported"
else
  echo "MEMORY_SWAP unsupported"
  cat /tmp/fp-swap.err || true
fi
docker rm -f fp-probe-mem >/dev/null 2>&1 || true
set +e
timeout 30 docker run -i --name fp-probe-mem \
  "${base_args[@]}" \
  --pids-limit=64 \
  --memory=64m \
  "${swap_args[@]}" \
  --cpus=0.5 \
  --tmpfs="$TMPFS" \
  "$IMAGE" \
  python -u - memory <"$PROBE" >/tmp/fp-mem.out 2>/tmp/fp-mem.err
mem_code=$?
set -e
echo "memory_exit=$mem_code"
cat /tmp/fp-mem.out || true
cat /tmp/fp-mem.err || true
oom=unknown
if docker inspect fp-probe-mem >/dev/null 2>&1; then
  oom=$(docker inspect --format '{{.State.OOMKilled}} {{.State.ExitCode}}' fp-probe-mem || true)
  echo "OOM $oom"
fi
memory_status=FAIL
case "$oom" in
'true '*) memory_status=PASS ;;
esac
if [ "$mem_code" -eq 137 ]; then
  memory_status=PASS
fi
docker rm -f fp-probe-mem >/dev/null 2>&1 || true

echo "=== check 6 wall time ==="
# Docker does not stop a container when a label says it should. The reaper does.
now_s=$(date +%s)
start_labelled fp-probe-wall-live "$LIVE_EXECUTION" "$((now_s + 600))"
start_labelled fp-probe-wall-dead "$DEAD_EXECUTION" "$((now_s - 30))"
sleep 2
live_before=$(docker inspect --format '{{.State.Status}}' fp-probe-wall-live 2>/dev/null || echo missing)
dead_before=$(docker inspect --format '{{.State.Status}}' fp-probe-wall-dead 2>/dev/null || echo missing)
echo "docker_native live=$live_before dead=$dead_before deadline_already_passed_for_dead=true"
echo "=== reaper wall ==="
reap_now
live_after=$(docker inspect --format '{{.State.Status}}' fp-probe-wall-live 2>/dev/null || echo missing)
dead_after=$(docker inspect --format '{{.State.Status}}' fp-probe-wall-dead 2>/dev/null || echo missing)
echo "supervisor live=$live_after dead=$dead_after"
echo "=== reaper wall again ==="
reap_now
live_again=$(docker inspect --format '{{.State.Status}}' fp-probe-wall-live 2>/dev/null || echo missing)
echo "supervisor_second live=$live_again"
wall_status=FAIL
if [ "$live_before" = "running" ] && [ "$dead_before" = "running" ] \
  && [ "$live_after" = "running" ] && [ "$dead_after" = "missing" ] \
  && [ "$live_again" = "running" ]; then
  wall_status=PASS
fi
docker rm -f fp-probe-wall-live >/dev/null 2>&1 || true
echo "wall=$wall_status docker_native=no_deadline supervisor_keeps_future_expiry=$live_after"

if [ "$disk_status" = "PASS" ] && [ "$pids_status" = "PASS" ] \
  && [ "$cpu_status" = "PASS" ] && [ "$memory_status" = "PASS" ] \
  && [ "$wall_status" = "PASS" ]; then
  result 6 limits PASS "disk=$disk_status suffix=$disk_suffix k=$disk_k bytes=$disk_bytes pids=$pids_status cpu=$cpu_status memory=$memory_status wall=$wall_status docker_native=no_deadline"
else
  result 6 limits FAIL "disk=$disk_status suffix=$disk_suffix k=$disk_k bytes=$disk_bytes pids=$pids_status cpu=$cpu_status memory=$memory_status wall=$wall_status docker_native=no_deadline"
fi

echo "=== check 7 outputs ==="
classify_one() {
  local mode=$1
  local name=$2
  if [ -z "$PY" ]; then
    echo "classify $mode skipped" >&2
    cap_out=""
    return 1
  fi
  capture_probe "$name" "$mode" 32 128m "$TMPFS" >/tmp/fp-emit.out
  if [ "$mode" = "emit-big" ]; then
    echo "emit-big bytes=${#cap_out}" >&2
  else
    echo "stdout $(printf '%s' "$cap_out" | tr '\n' ' ')" >&2
  fi
  got=$(printf '%s' "$cap_out" | PYTHONPATH="$ROOT/src" "$PY" "$DIR/check_output.py")
  echo "classify $mode -> $got" >&2
  printf '%s\n' "$got"
}

ok_bad=$(classify_one emit-bad fp-probe-bad || true)
ok_big=$(classify_one emit-big fp-probe-big || true)
ok_mal=$(classify_one emit-malformed fp-probe-mal || true)
ok_ok=$(classify_one emit-ok fp-probe-ok || true)
if [ "$ok_bad" = "invalid_result invalid_result" ] \
  && [ "$ok_big" = "invalid_result invalid_result" ] \
  && [ "$ok_mal" = "invalid_result invalid_result" ] \
  && [ "$ok_ok" = "success passed" ]; then
  result 7 outputs PASS "extra_key=invalid_result oversized=invalid_result malformed=invalid_result legal_shape=success"
else
  result 7 outputs FAIL "bad=$ok_bad big=$ok_big malformed=$ok_mal legal=$ok_ok"
fi

gone() {
  if docker inspect "$1" >/dev/null 2>&1; then
    return 1
  fi
  return 0
}

wait_gone() {
  local name=$1
  local _i
  for _i in 1 2 3 4 5 6 7 8 9 10; do
    if gone "$name"; then
      return 0
    fi
    sleep 1
  done
  return 1
}

echo "=== check 8 cleanup ==="
docker rm -f fp-probe-8a >/dev/null 2>&1 || true
docker run --rm --name fp-probe-8a \
  "${base_args[@]}" \
  --pids-limit=32 \
  --memory=64m \
  --cpus=0.5 \
  --tmpfs="$TMPFS" \
  "$IMAGE" \
  python -c 'print("done")' >/tmp/fp-8a.out
if gone fp-probe-8a; then
  c8a=PASS
else
  c8a=FAIL
fi
echo "8a_completion_rm=$c8a"

docker rm -f fp-probe-8b >/dev/null 2>&1 || true
docker run -d --rm --name fp-probe-8b \
  "${base_args[@]}" \
  --pids-limit=32 \
  --memory=64m \
  --cpus=0.5 \
  --tmpfs="$TMPFS" \
  "$IMAGE" \
  python -c 'import time; time.sleep(120)' >/tmp/fp-8b.id
docker kill fp-probe-8b >/dev/null 2>&1 || true
if wait_gone fp-probe-8b; then
  c8b=PASS
else
  c8b=FAIL
fi
echo "8b_kill_rm=$c8b"

docker rm -f fp-probe-8c >/dev/null 2>&1 || true
host_register "$ORPHAN_EXECUTION" 1
docker run --rm --name fp-probe-8c \
  "${base_args[@]}" \
  --pids-limit=32 \
  --memory=64m \
  --cpus=0.5 \
  --tmpfs="$TMPFS" \
  --label "flakepilot.execution=$ORPHAN_EXECUTION" \
  --label flakepilot.expires=1 \
  "$IMAGE" \
  python -c 'import time; time.sleep(120)' >/tmp/fp-8c.out 2>/tmp/fp-8c.err &
client=$!
c8c_started=0
for _i in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15; do
  state=$(docker inspect -f '{{.State.Running}}' fp-probe-8c 2>/dev/null || true)
  if [ "$state" = "true" ]; then
    c8c_started=1
    break
  fi
  sleep 1
done
kill -9 "$client" >/dev/null 2>&1 || true
wait "$client" >/dev/null 2>&1 || true
sleep 3
if [ "$c8c_started" -ne 1 ]; then
  c8c=NEVER
  echo "8c_client_sigkill=NEVER container_never_started"
elif gone fp-probe-8c; then
  c8c=ABSENT
  echo "8c_client_sigkill=ABSENT"
else
  c8c_state=$(docker inspect -f '{{.State.Status}}' fp-probe-8c 2>/dev/null || echo missing)
  c8c=LEFT
  echo "8c_client_sigkill=LEFT status=$c8c_state"
fi

docker rm -f fp-probe-8d >/dev/null 2>&1 || true
host_register "$STOPPED_EXECUTION" 1
docker run -d --name fp-probe-8d \
  "${base_args[@]}" \
  --pids-limit=32 \
  --memory=64m \
  --cpus=0.5 \
  --tmpfs="$TMPFS" \
  --label "flakepilot.execution=$STOPPED_EXECUTION" \
  --label flakepilot.expires=1 \
  "$IMAGE" \
  python -c 'import time; time.sleep(120)' >/tmp/fp-8d.id
docker kill fp-probe-8d >/dev/null 2>&1 || true
sleep 2
if gone fp-probe-8d; then
  c8d=ABSENT
  echo "8d_no_rm=ABSENT"
else
  c8d_state=$(docker inspect -f '{{.State.Status}}' fp-probe-8d 2>/dev/null || echo missing)
  c8d=LEFT
  echo "8d_no_rm=LEFT status=$c8d_state"
fi

docker rm -f fp-probe-8e >/dev/null 2>&1 || true
docker run -d --rm --name fp-probe-8e \
  "${base_args[@]}" \
  --pids-limit=32 \
  --memory=64m \
  --cpus=0.5 \
  --tmpfs="$TMPFS" \
  "$IMAGE" \
  python -c 'import time; time.sleep(120)' >/tmp/fp-8e.id
docker stop -t 1 fp-probe-8e >/dev/null 2>&1 || true
if wait_gone fp-probe-8e; then
  c8e=PASS
else
  c8e=FAIL
fi
echo "8e_docker_stop=$c8e"

now_s=$(date +%s)
start_labelled fp-probe-8live "$EIGHT_LIVE" "$((now_s + 600))"
sleep 1
echo "=== reaper orphans ==="
reap_now
c8c_after=missing
c8d_after=missing
if ! gone fp-probe-8c; then
  c8c_after=$(docker inspect -f '{{.State.Status}}' fp-probe-8c 2>/dev/null || echo missing)
fi
if ! gone fp-probe-8d; then
  c8d_after=$(docker inspect -f '{{.State.Status}}' fp-probe-8d 2>/dev/null || echo missing)
fi
live_kept=$(docker inspect --format '{{.State.Status}}' fp-probe-8live 2>/dev/null || echo missing)
echo "reaper_after 8c=$c8c_after 8d=$c8d_after live=$live_kept"
reaper_expired=FAIL
if [ "$c8c" = "LEFT" ] && [ "$c8d" = "LEFT" ] \
  && [ "$c8c_after" = "missing" ] && [ "$c8d_after" = "missing" ]; then
  reaper_expired=PASS
fi
reaper_live=FAIL
if [ "$live_kept" = "running" ]; then
  reaper_live=PASS
fi
docker rm -f fp-probe-8live >/dev/null 2>&1 || true

if [ "$c8a" = "PASS" ] && [ "$c8b" = "PASS" ] && [ "$c8e" = "PASS" ] \
  && [ "$reaper_expired" = "PASS" ] && [ "$reaper_live" = "PASS" ]; then
  result 8 cleanup PARTIAL "completion=$c8a kill_rm=$c8b client_sigkill=$c8c no_rm=$c8d docker_stop=$c8e reaper_expired=$reaper_expired reaper_live=$reaper_live host_restart=NOT_EXECUTED"
else
  result 8 cleanup FAIL "completion=$c8a kill_rm=$c8b client_sigkill=$c8c no_rm=$c8d docker_stop=$c8e reaper_expired=$reaper_expired reaper_live=$reaper_live host_restart=NOT_EXECUTED"
fi

echo "=== check 9 stale worker ==="
result 9 stale_worker NOT_EXECUTED "postgresql_lease_not_placed_on_this_runner"

echo "=== check 10 tenant mounts ==="
rm -rf /tmp/fp-tenant-a /tmp/fp-tenant-b
mkdir -p /tmp/fp-tenant-a /tmp/fp-tenant-b
chmod 777 /tmp/fp-tenant-a /tmp/fp-tenant-b
capture_probe fp-probe-10a tenant-write 32 128m "$TMPFS" \
  --volume /tmp/fp-tenant-a:/data:rw >/tmp/fp-10a.out
cat /tmp/fp-10a.out
probe_hash=$(printf '%s\n' "$cap_out" | sed -n 's/.*sha256=\([0-9a-f][0-9a-f]*\).*/\1/p')
if [ -f /tmp/fp-tenant-a/marker ]; then
  host_hash=$(sha256sum /tmp/fp-tenant-a/marker | awk '{print $1}')
else
  host_hash=""
fi
echo "host_sha256=$host_hash probe_sha256=$probe_hash"
capture_probe fp-probe-10b tenant-read 32 128m "$TMPFS" \
  --volume /tmp/fp-tenant-b:/data:rw >/tmp/fp-10b.out
cat /tmp/fp-10b.out
if [ -n "$probe_hash" ] && [ "$probe_hash" = "$host_hash" ] \
  && has 'other=errno_2' && has 'host=errno_2'; then
  result 10 artifact_scope PARTIAL "mount_isolation=PASS integrity=PASS app_authorization=NOT_EXECUTED two_output_dirs_only"
else
  result 10 artifact_scope FAIL "mount_or_hash_mismatch"
fi

echo "HARNESS complete fail=$fail"
exit "$fail"

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
DISK_TMPFS=/tmp:rw,nosuid,nodev,noexec,size=1m
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
docker pull "$IMAGE"
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
  docker run -i --name "$name" \
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
capture_probe fp-probe-disk disk 32 128m "$DISK_TMPFS" >/tmp/fp-check6d.out
cat /tmp/fp-check6d.out
if has 'disk_errno=28'; then
  disk_status=PASS
else
  disk_status=FAIL
fi
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
docker run --rm --name fp-probe-swap --pull=never --network=none --cap-drop=ALL \
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
docker run -i --name fp-probe-mem \
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
docker rm -f fp-probe-wall >/dev/null 2>&1 || true
docker run -d --name fp-probe-wall \
  "${base_args[@]}" \
  --pids-limit=32 \
  --memory=64m \
  --cpus=0.5 \
  --tmpfs="$TMPFS" \
  "$IMAGE" \
  python -c 'import time; time.sleep(120)' >/tmp/fp-wall.id
sleep 5
wall_state=$(docker inspect --format '{{.State.Status}}' fp-probe-wall 2>/dev/null || echo missing)
echo "wall_state_after_5s=$wall_state"
if [ "$wall_state" = "running" ]; then
  wall_status=FAIL
else
  wall_status=PASS
fi
docker rm -f fp-probe-wall >/dev/null 2>&1 || true

if [ "$disk_status" = "PASS" ] && [ "$pids_status" = "PASS" ] \
  && [ "$cpu_status" = "PASS" ] && [ "$memory_status" = "PASS" ] \
  && [ "$wall_status" = "PASS" ]; then
  result 6 limits PASS "disk=$disk_status pids=$pids_status cpu=$cpu_status memory=$memory_status wall=$wall_status"
else
  result 6 limits FAIL "disk=$disk_status pids=$pids_status cpu=$cpu_status memory=$memory_status wall=$wall_status runtime_has_no_deadline"
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
docker run --rm --name fp-probe-8c \
  "${base_args[@]}" \
  --pids-limit=32 \
  --memory=64m \
  --cpus=0.5 \
  --tmpfs="$TMPFS" \
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
  c8c=FAIL
  echo "8c_client_sigkill=FAIL container_never_started"
elif gone fp-probe-8c; then
  c8c=PASS
  echo "8c_client_sigkill=PASS absent"
else
  c8c_state=$(docker inspect -f '{{.State.Status}}' fp-probe-8c 2>/dev/null || echo missing)
  c8c=FAIL
  echo "8c_client_sigkill=FAIL remains status=$c8c_state"
fi

docker rm -f fp-probe-8d >/dev/null 2>&1 || true
docker run -d --name fp-probe-8d \
  "${base_args[@]}" \
  --pids-limit=32 \
  --memory=64m \
  --cpus=0.5 \
  --tmpfs="$TMPFS" \
  "$IMAGE" \
  python -c 'import time; time.sleep(120)' >/tmp/fp-8d.id
docker kill fp-probe-8d >/dev/null 2>&1 || true
sleep 2
if gone fp-probe-8d; then
  c8d=PASS
  echo "8d_no_rm=PASS absent"
else
  c8d_state=$(docker inspect -f '{{.State.Status}}' fp-probe-8d 2>/dev/null || echo missing)
  c8d=FAIL
  echo "8d_no_rm=FAIL remains status=$c8d_state"
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

if [ "$c8a" = "PASS" ] && [ "$c8b" = "PASS" ] && [ "$c8c" = "PASS" ] \
  && [ "$c8d" = "PASS" ] && [ "$c8e" = "PASS" ]; then
  result 8 cleanup PARTIAL "completion=$c8a kill_rm=$c8b client_sigkill=$c8c no_rm=$c8d docker_stop=$c8e host_restart=NOT_EXECUTED"
else
  result 8 cleanup FAIL "completion=$c8a kill_rm=$c8b client_sigkill=$c8c no_rm=$c8d docker_stop=$c8e host_restart=NOT_EXECUTED harness_removed_names_after_record"
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

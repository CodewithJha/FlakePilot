#!/bin/bash
# Developer-laptop probe for Docker Desktop. Not a production execution host.
# Not a per-run microVM. Isolation checks stay false. Execution stays disabled.
# macOS starts this file with zsh unless bash is named. Re-exec bash either way.
if [ -z "${BASH_VERSION:-}" ]; then
  exec /bin/bash "$0" "$@"
fi

set -u
set -o pipefail

DIR=$(cd "$(dirname "$0")" && pwd)
ROOT=$(cd "$DIR/../.." && pwd)
cd "$ROOT"

RUN="m$(date +%s)$$"
LEDGER_DIR=$(mktemp -d "${TMPDIR:-/tmp}/fp-mac-ledger.XXXXXX")
chmod 700 "$LEDGER_DIR"

cleanup() {
  if command -v docker >/dev/null 2>&1; then
    ids=$(docker ps -aq --filter "label=flakepilot.macrun=${RUN}" 2>/dev/null || true)
    for id in $ids; do
      docker rm -f "$id" >/dev/null 2>&1 || true
    done
  fi
  rm -rf "$LEDGER_DIR"
}
trap cleanup EXIT

if ! command -v uv >/dev/null 2>&1; then
  echo "CHECK probe NOT EXECUTED uv_missing"
  exit 1
fi

echo "PROBE run=$RUN microvm=false production=false"

uv run python "$DIR/local_mac_driver.py" --run "$RUN" --ledger-dir "$LEDGER_DIR" &
pid=$!
(
  sleep 300
  kill -TERM "$pid" 2>/dev/null || true
  sleep 5
  kill -KILL "$pid" 2>/dev/null || true
) &
watch=$!
wait "$pid"
code=$?
kill "$watch" 2>/dev/null || true
wait "$watch" 2>/dev/null || true
exit "$code"

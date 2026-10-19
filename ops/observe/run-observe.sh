#!/usr/bin/env bash
# One local observation run: `python -m src.runner.main --scout-once` against
# the isolated observation database (macOS, started by launchd or by hand).
#
#   ops/observe/run-observe.sh           one run (skipped while STOP exists)
#   ops/observe/run-observe.sh --check   validate the profile, run nothing
#
# The scout records discovered pools, watches them and reads market history;
# it never forms a trade case, an order or a position. Everything else that
# could trade stays off because the scout starts with an empty environment
# plus the allowlisted profile (see common.sh): no inherited variable and no
# repository .env reaches it.
#
# Exit: the scout's own code; 0 skipped (STOP present or a run in progress);
# 78 the profile or the checkout was refused (reason in the log).
set -euo pipefail

# shellcheck source=common.sh
source "$(dirname "${BASH_SOURCE[0]}")/common.sh"

LOG="$LOG_DIR/observe.log"
MAX_BYTES=$((5 * 1024 * 1024))
KEEP=3

mkdir -p "$LOG_DIR" "$STATE_DIR"
chmod 0700 "$STATE_DIR"
if [[ -f "$LOG" ]] && (($(wc -c <"$LOG") > MAX_BYTES)); then
  for ((i = KEEP - 1; i >= 1; i--)); do
    [[ -f "$LOG.$i" ]] && mv "$LOG.$i" "$LOG.$((i + 1))"
  done
  mv "$LOG" "$LOG.1"
fi

say() { echo "$(date -u +%FT%TZ) observe: $*" | tee -a "$LOG"; }
refuse() { say "refused $1"; exit 78; }

reason="$(observe_load)" || refuse "$reason"
observe_load >/dev/null
reason="$(observe_check)" || refuse "$reason"
# Only committed code runs: no tracked change and no untracked file in the
# code the scout loads (ops/ and backend/src); .venv and logs are elsewhere.
[[ -z "$(git -C "$REPO" status --porcelain -- ops backend/src)" ]] &&
  git -C "$REPO" diff --quiet HEAD -- || refuse OBSERVE_WORKTREE_MODIFIED

if [[ "${1:-}" == "--check" ]]; then
  say "profile ok (database $(observe_db), commit $(git -C "$REPO" rev-parse --short HEAD))"
  exit 0
fi
[[ $# -eq 0 ]] || refuse OBSERVE_UNKNOWN_ARGUMENT

if [[ -e "$STOP_FILE" ]]; then
  say "skipped STOP_FILE_PRESENT"
  exit 0
fi
if running="$(observe_running_pid)"; then
  say "skipped RUN_IN_PROGRESS pid $running"
  exit 0
fi

env_args=()
while IFS= read -r line; do env_args+=("$line"); done < <(observe_env_args)

child=""
signalled=""
# A non-interactive shell starts background commands with SIGINT ignored, so
# the scout is stopped with TERM; every stage it already committed stays.
stop_child() {
  signalled=yes
  [[ -n "$child" ]] && kill -TERM "$child" 2>/dev/null || true
}
trap stop_child TERM INT
trap 'rm -f "$PID_FILE"' EXIT
echo $$ >"$PID_FILE"

say "start (database $(observe_db), commit $(git -C "$REPO" rev-parse --short HEAD))"
status=0
cd "$REPO/backend"
env -i "${env_args[@]}" "$PYTHON" -m src.runner.main --scout-once >>"$LOG" 2>&1 &
child=$!
wait "$child" || status=$?
# A signal interrupts the wait, not the scout: wait for the scout's own code.
if [[ -n "$signalled" ]]; then
  status=0
  wait "$child" || status=$?
fi
say "exit $status"
exit "$status"

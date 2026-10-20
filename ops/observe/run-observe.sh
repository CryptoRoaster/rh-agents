#!/usr/bin/env bash
# One local observation run: `python -m src.runner.main --scout-once` against
# the isolated observation database (macOS, started by launchd or by hand).
#
#   ops/observe/run-observe.sh           one run (skipped while STOP exists)
#   ops/observe/run-observe.sh --check   validate the profile, run nothing
#   ops/observe/run-observe.sh --preflight  the runner preflight under the profile
#                                        (writes nothing, calls no provider)
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

say() { echo "$(date -u +%FT%TZ) observe: $*" >>"$LOG"; }
refuse() { say "refused $1"; echo "OBSERVE_REFUSED: $1" >&2; exit 78; }

reason="$(observe_load)" || refuse "$reason"
observe_load >/dev/null
reason="$(observe_check)" || refuse "$reason"
observe_code_clean || refuse OBSERVE_WORKTREE_MODIFIED
# Checked on every run, not only at start: a checkout moved to another commit
# after loading the agent never runs unmerged code.
observe_code_merged || refuse OBSERVE_COMMIT_NOT_ON_MAIN

observe_pycache || refuse OBSERVE_PYCACHE_UNAVAILABLE
trap observe_drop_pycache EXIT
env_args=()
while IFS= read -r line; do env_args+=("$line"); done < <(observe_env_args)

case "${1:-}" in
  --check)
    say "profile ok (database $(observe_db), commit $(git -C "$REPO" rev-parse --short HEAD))"
    echo "profile ok (database $(observe_db), commit $(git -C "$REPO" rev-parse --short HEAD))"
    exit 0
    ;;
  --preflight)
    cd "$REPO/backend"
    status=0
    env -i "${env_args[@]}" "$PYTHON" -m src.runner.main --preflight || status=$?
    exit "$status"
    ;;
  "") ;;
  *) refuse OBSERVE_UNKNOWN_ARGUMENT ;;
esac

# The lock first, then STOP: a stop that sets STOP either sees this run's
# lock or this run sees STOP.
if ! observe_lock; then
  say "skipped RUN_IN_PROGRESS pid $(observe_running_pid || echo UNKNOWN)"
  exit 0
fi
trap 'observe_unlock; observe_drop_pycache' EXIT
if [[ -e "$STOP_FILE" ]]; then
  say "skipped STOP_FILE_PRESENT"
  exit 0
fi

child=""
signalled=""
# A non-interactive shell starts background commands with SIGINT ignored, so
# the scout is stopped with TERM; every stage it already committed stays.
stop_child() {
  signalled=yes
  [[ -n "$child" ]] && kill -TERM "$child" 2>/dev/null || true
}
trap stop_child TERM INT

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

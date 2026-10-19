#!/usr/bin/env bash
# Stop the local observation without losing data (macOS).
#
#   ops/observe/stop.sh           STOP file, wait for the run in progress, unload
#   ops/observe/stop.sh --now     STOP file, interrupt the run in progress, unload
#
# The STOP file makes every later run skip at once, even if the agent were
# loaded again by hand; start.sh clears it. A run in progress is waited for (up
# to RH_AGENTS_OBSERVE_STOP_WAIT_SECONDS, default 600) because every stage
# commits on its own and the next run continues from the database; an
# interrupted run loses only its uncommitted stage. Logs and data stay.
set -euo pipefail

# shellcheck source=common.sh
source "$(dirname "${BASH_SOURCE[0]}")/common.sh"

mode="${1:-}"
[[ -z "$mode" || "$mode" == "--now" ]] || { echo "usage: stop.sh [--now]" >&2; exit 2; }
WAIT="${RH_AGENTS_OBSERVE_STOP_WAIT_SECONDS:-600}"
[[ "$WAIT" =~ ^[0-9]{1,4}$ ]] || { echo "RH_AGENTS_OBSERVE_STOP_WAIT_SECONDS must be whole seconds" >&2; exit 2; }

mkdir -p "$STATE_DIR"
chmod 0700 "$STATE_DIR"
touch "$STOP_FILE"

if pid="$(observe_running_pid)"; then
  # Identity checked again right before the signal, never on a stale reading.
  if [[ "$mode" == "--now" ]] && [[ "$(observe_running_pid)" == "$pid" ]]; then
    kill -TERM "$pid" 2>/dev/null || true
  fi
  waited=0
  while observe_running_pid >/dev/null && ((waited < WAIT)); do
    sleep 1
    waited=$((waited + 1))
  done
  if observe_running_pid >/dev/null; then
    echo "OBSERVE_STOP_TIMEOUT: run $pid still active after ${WAIT}s (STOP file set; retry or use --now)" >&2
    exit 1
  fi
fi

"$LAUNCHCTL" bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
if "$LAUNCHCTL" print "gui/$(id -u)/$LABEL" >/dev/null 2>&1; then
  echo "OBSERVE_STOP_FAILED: $LABEL is still loaded (STOP file set, so no run starts)" >&2
  exit 1
fi
rm -f "$PLIST"
echo "stopped $LABEL (STOP file kept; data and logs kept)"

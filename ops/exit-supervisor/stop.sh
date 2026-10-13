#!/usr/bin/env bash
# Stop the exit-only supervisor.
#
#   ops/exit-supervisor/stop.sh --reason CODE
#       Regular stop. Refused unless no position is open, no exit or entry run
#       is going, and entries are durably blocked (an entry halt or the pause):
#       see `python -m src.supervisor.main stop-check`.
#
#   ops/exit-supervisor/stop.sh --emergency --reason CODE --i-understand-exit-monitoring-stops
#       Emergency stop. Not guarded: it ends exit monitoring even with positions
#       open, and nothing closes them until the supervisor runs again. Audited.
#       It never sells anything and never liquidates.
#
# Both unload the agent and mark it disabled, so a login does not bring it back.
set -euo pipefail
# shellcheck source=common.sh
source "$(dirname "${BASH_SOURCE[0]}")/common.sh"

emergency="" acknowledged="" reason=""
while (($#)); do
  case "$1" in
    --emergency) emergency=1 ;;
    --i-understand-exit-monitoring-stops) acknowledged=1 ;;
    --reason) reason="${2:-}"; shift ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
  shift
done
if [[ -z "$reason" ]]; then
  echo "refusing: stop.sh needs --reason CODE" >&2
  exit 2
fi
resolve_uv

if [[ -n "$emergency" ]]; then
  if [[ -z "$acknowledged" ]]; then
    echo "refusing: an emergency stop needs --i-understand-exit-monitoring-stops" >&2
    exit 2
  fi
  echo "WARNING: emergency stop. Exit monitoring ends now; open PAPER positions are left" >&2
  echo "WARNING: without automatic exits until the supervisor is started again." >&2
  # Recorded first. An audit that cannot be written does not block an emergency.
  supervisor audit --action emergency-stop --reason "$reason" ||
    echo "WARNING: the emergency stop could not be audited" >&2
  "$LAUNCHCTL" bootout "$SERVICE" 2>/dev/null || true
  "$LAUNCHCTL" disable "$SERVICE"
  if is_loaded; then
    echo "FAILED: $LABEL is still loaded; exit monitoring has NOT stopped" >&2
    exit 1
  fi
  echo "emergency-stopped $LABEL"
  exit 0
fi

if ! supervisor stop-check; then
  echo "refusing: regular stop not allowed now (see refusals above)" >&2
  exit 2
fi
# A tick may have started since the check: never cut a running exit job short.
if is_running; then
  echo "refusing: a tick is running; try again when it has finished" >&2
  exit 2
fi
supervisor audit --action stop --reason "$reason"
"$LAUNCHCTL" bootout "$SERVICE" 2>/dev/null || true
"$LAUNCHCTL" disable "$SERVICE"
if is_loaded; then
  echo "failed: $LABEL is still loaded (the stop was audited, but it did not take effect)" >&2
  exit 1
fi
echo "stopped $LABEL"

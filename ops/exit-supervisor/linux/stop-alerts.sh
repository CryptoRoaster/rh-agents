#!/usr/bin/env bash
# Stop the alert pass and the heartbeat (Linux).
#
#   ops/exit-supervisor/linux/stop-alerts.sh --reason CODE
#
# Refused while the supervisor timer is anything but stopped: exit supervision
# never runs without alerting. Audited. With the heartbeat stopped the dead-man
# receiver, if one is set up, reports this host as down: tell it first.
set -euo pipefail
# shellcheck source=common.sh
source "$(dirname "${BASH_SOURCE[0]}")/common.sh"

reason=""
while (($#)); do
  case "$1" in
    --reason) reason="${2:-}"; shift ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
  shift
done
if [[ -z "$reason" ]]; then
  echo "refusing: stop-alerts.sh needs --reason CODE" >&2
  exit 2
fi
if ! is_inactive "$SUPERVISOR_TIMER"; then
  echo "refusing: $SUPERVISOR_TIMER is $(unit_state "$SUPERVISOR_TIMER"); stop the supervisor first (stop.sh)" >&2
  exit 2
fi
supervisor audit --action alerts-stop --reason "$reason"
"$SYSTEMCTL" disable --now "$ALERT_TIMER" "$HEARTBEAT_TIMER" || true
for timer in "$ALERT_TIMER" "$HEARTBEAT_TIMER"; do
  if ! is_inactive "$timer"; then
    echo "failed: $timer is still $(unit_state "$timer")" >&2
    exit 1
  fi
done
echo "stopped $ALERT_TIMER and $HEARTBEAT_TIMER"

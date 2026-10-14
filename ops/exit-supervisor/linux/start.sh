#!/usr/bin/env bash
# Start the exit-only supervisor (Linux): alerts first, then the exit tick.
#
#   ops/exit-supervisor/linux/start.sh --confirm --reason CODE [--with-heartbeat]
#
# Refused without its confirmation and a reason, outside the runtime checkout
# on main, without installed units, while the supervisor timer is not stopped,
# and without alert credentials: exit supervision never starts without a way
# to raise an alarm. Audited. The alert timer is enabled before the
# supervisor's, so the supervisor never runs unwatched. Enabled timers start
# again at every boot.
set -euo pipefail
# shellcheck source=common.sh
source "$(dirname "${BASH_SOURCE[0]}")/common.sh"

confirm="" reason="" heartbeat=""
while (($#)); do
  case "$1" in
    --confirm) confirm=1 ;;
    --reason) reason="${2:-}"; shift ;;
    --with-heartbeat) heartbeat=1 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
  shift
done
if [[ -z "$confirm" || -z "$reason" ]]; then
  echo "refusing: start.sh needs --confirm and --reason CODE" >&2
  exit 2
fi
require_runtime_main
for unit in "${UNITS[@]}"; do
  if [[ ! -f "$UNIT_DIR/$unit" ]]; then
    echo "refusing: $unit is not installed (install.sh)" >&2
    exit 1
  fi
done
if ! is_inactive "$SUPERVISOR_TIMER"; then
  echo "refusing: $SUPERVISOR_TIMER is $(unit_state "$SUPERVISOR_TIMER")" >&2
  exit 1
fi
for credential in telegram-token telegram-chat-id; do
  if [[ ! -f "$CREDENTIALS_DIR/$credential" ]]; then
    echo "refusing: no alert credential $credential; exit supervision never starts without alerting" >&2
    exit 1
  fi
done
if [[ -n "$heartbeat" ]]; then
  for credential in heartbeat-key heartbeat-ssh-key heartbeat-known-hosts; do
    if [[ ! -f "$CREDENTIALS_DIR/$credential" ]]; then
      echo "refusing: no heartbeat credential $credential" >&2
      exit 1
    fi
  done
fi

# One pass of a oneshot unit, synchronously: did it succeed?
passes() {
  "$SYSTEMCTL" start "$1" &&
    [[ "$("$SYSTEMCTL" show "$1" --property=Result --value 2>/dev/null)" == "success" ]]
}

supervisor audit --action start --reason "$reason"
# The alert path is proven before anything is supervised: one pass now must
# succeed (credentials readable and valid, the message delivered — while the
# supervisor is still stopped that is a real STOPPED alert), else nothing starts.
if ! "$SYSTEMCTL" enable --now "$ALERT_TIMER" || ! is_active "$ALERT_TIMER" ||
  ! passes "$ALERT_SERVICE"; then
  "$SYSTEMCTL" disable --now "$ALERT_TIMER" || true
  echo "failed: the alert path does not work (see journalctl -u $ALERT_SERVICE); the supervisor was not started" >&2
  exit 1
fi
if [[ -n "$heartbeat" ]]; then
  if ! "$SYSTEMCTL" enable --now "$HEARTBEAT_TIMER" || ! is_active "$HEARTBEAT_TIMER" ||
    ! passes "$HEARTBEAT_SERVICE"; then
    "$SYSTEMCTL" disable --now "$HEARTBEAT_TIMER" || true
    echo "failed: the heartbeat does not get through; the supervisor was not started" >&2
    exit 1
  fi
fi
if ! "$SYSTEMCTL" enable --now "$SUPERVISOR_TIMER" || ! is_active "$SUPERVISOR_TIMER"; then
  # Never leave it enabled but not running: the next boot would start it unaudited.
  "$SYSTEMCTL" disable --now "$SUPERVISOR_TIMER" || true
  echo "failed: $SUPERVISOR_TIMER did not start; left disabled (alerts keep running)" >&2
  exit 1
fi
echo "started $SUPERVISOR_TIMER (exit job every 60 seconds) with alerts; check with health.sh"

#!/usr/bin/env bash
# Stop job timers (Linux). Never touches the exit supervisor: exits keep running.
#
#   ops/jobs/linux/stop.sh --job index|scout|entry|all --reason CODE
#
# Disables the timer (a boot does not bring it back) and stops a running run
# (SIGINT, then the control group after 30 s; every write is idempotent by
# its key, so an interrupted run leaves nothing half-booked). Stopping a job
# is always allowed. To stop entries at the database level as well use
# `python -m src.runner.control --halt-entries --reason CODE`.
set -euo pipefail
# shellcheck source=common.sh
source "$(dirname "${BASH_SOURCE[0]}")/common.sh"

job="" reason=""
while (($#)); do
  case "$1" in
    --job) job="${2:-}"; shift ;;
    --reason) reason="${2:-}"; shift ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
  shift
done
case "$job" in
  all) jobs=(entry scout index) ;;  # entries first
  index | scout | entry) jobs=("$job") ;;
  *) echo "refusing: --job must be index, scout, entry or all" >&2; exit 2 ;;
esac
if [[ ! "$reason" =~ $REASON_RE ]]; then
  echo "refusing: stop.sh needs --reason CODE (UPPER_SNAKE_CASE)" >&2
  exit 2
fi

audit_job stop "$job" "$reason" || echo "WARNING: the stop could not be audited" >&2
failed=0
for name in "${jobs[@]}"; do
  timer="$(job_timer "$name")" service="$(job_service "$name")"
  "$SYSTEMCTL" disable --now "$timer" || true
  "$SYSTEMCTL" stop "$service" || true
  if ! is_inactive "$timer"; then
    echo "FAILED: $timer is still $(unit_state "$timer")" >&2
    failed=1
  fi
  state="$(unit_state "$service")"
  if [[ "$state" != "inactive" && "$state" != "failed" ]]; then
    echo "FAILED: $service is still $state" >&2
    failed=1
  fi
done
((failed == 0)) || exit 1
echo "stopped: ${jobs[*]}"

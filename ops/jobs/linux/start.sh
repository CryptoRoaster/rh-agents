#!/usr/bin/env bash
# Start one job's timer (Linux): explicit, audited, verified.
#
#   ops/jobs/linux/start.sh --job index|scout|entry --confirm --reason CODE
#
# Refused (exit 1, nothing changed) unless the units are installed, the
# job's timer is inactive and -- for the entry job -- the exit supervisor and
# its alert pass are active: entries never start before exits are watched
# and alarmed. The entry job's own run checks this again every time, and
# every BUY is gated on a recent ready exit run (EXIT_JOB_STALE) besides.
# Run as root from the runtime checkout on main.
set -euo pipefail
# shellcheck source=common.sh
source "$(dirname "${BASH_SOURCE[0]}")/common.sh"

job="" confirmed="" reason=""
while (($#)); do
  case "$1" in
    --job) job="${2:-}"; shift ;;
    --confirm) confirmed=1 ;;
    --reason) reason="${2:-}"; shift ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
  shift
done
timer="$(job_timer "$job")"
service="$(job_service "$job")"
if [[ -z "$timer" ]]; then
  echo "refusing: --job must be index, scout or entry" >&2
  exit 2
fi
if [[ -z "$confirmed" || ! "$reason" =~ $REASON_RE ]]; then
  echo "refusing: start.sh needs --confirm and --reason CODE (UPPER_SNAKE_CASE)" >&2
  exit 2
fi
require_runtime_main

for unit in "$service" "$timer"; do
  [[ -f "$UNIT_DIR/$unit" ]] || { echo "refusing: $unit is not installed (install.sh)" >&2; exit 1; }
done
if ! is_inactive "$timer"; then
  echo "refusing: $timer is $(unit_state "$timer"), not inactive" >&2
  exit 1
fi
if [[ "$job" == "entry" ]] && ! exits_supervised; then
  echo "refusing: the exit supervisor and its alert pass must be active before any entry job" >&2
  exit 1
fi

audit_job start "$job" "$reason" || { echo "refusing: the start could not be audited" >&2; exit 1; }
if ! "$SYSTEMCTL" enable --now "$timer" || ! is_active "$timer"; then
  "$SYSTEMCTL" disable --now "$timer" || true
  echo "failed: $timer did not become active; it is disabled again" >&2
  exit 1
fi
echo "started $timer"

#!/usr/bin/env bash
# Remove the job units (Linux). Refused while any job timer is not inactive
# or any job service is not inactive or failed (stop.sh first). Leaves the exit supervisor, state and the database alone.
set -euo pipefail
# shellcheck source=common.sh
source "$(dirname "${BASH_SOURCE[0]}")/common.sh"

for timer in "${JOB_TIMERS[@]}"; do
  if ! is_inactive "$timer"; then
    echo "refusing: $timer is $(unit_state "$timer"); stop.sh --job all first" >&2
    exit 1
  fi
done
for service in "${JOB_SERVICES[@]}"; do
  state="$(unit_state "$service")"
  if [[ "$state" != "inactive" && "$state" != "failed" ]]; then
    echo "refusing: $service is $state; a running or unknown job keeps its unit file" >&2
    exit 1
  fi
done
for timer in "${JOB_TIMERS[@]}"; do
  if [[ "$(unit_property "$timer" LoadState)" == "loaded" ]]; then
    "$SYSTEMCTL" disable "$timer" || { echo "failed: could not disable $timer" >&2; exit 1; }
  fi
done
for unit in "${JOB_UNITS[@]}"; do
  rm -f "${UNIT_DIR:?}/${unit:?}"
done
"$SYSTEMCTL" daemon-reload
echo "UNINSTALLED: ${#JOB_UNITS[@]} job units"

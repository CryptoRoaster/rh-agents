#!/usr/bin/env bash
# Remove the supervisor's systemd units (Linux). Refused while any of its
# timers is anything but stopped: stop.sh, then stop-alerts.sh. State, audit
# log and credentials are left in place.
set -euo pipefail
# shellcheck source=common.sh
source "$(dirname "${BASH_SOURCE[0]}")/common.sh"

for timer in "${TIMERS[@]}"; do
  if ! is_inactive "$timer"; then
    echo "refusing: $timer is $(unit_state "$timer"); stop it first" >&2
    exit 1
  fi
done
# Disabled before the files go: no enablement link outlives its unit file
# and comes back to life with a later install.
for timer in "${TIMERS[@]}"; do
  if [[ "$(unit_property "$timer" LoadState)" == "loaded" ]]; then
    "$SYSTEMCTL" disable "$timer" || {
      echo "refusing: could not disable $timer; nothing was removed" >&2
      exit 1
    }
  fi
done
for unit in "${UNITS[@]}"; do
  rm -f -- "${UNIT_DIR:?}/${unit:?}"
done
"$SYSTEMCTL" daemon-reload
echo "removed ${#UNITS[@]} units"

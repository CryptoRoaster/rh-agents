#!/usr/bin/env bash
# Remove the exit-only supervisor's plist. Refused while it is loaded: stop it
# first (stop.sh), so an uninstall can never end exit monitoring. Logs, state
# and the audit log are left in place; the service stays marked disabled.
set -euo pipefail
# shellcheck source=common.sh
source "$(dirname "${BASH_SOURCE[0]}")/common.sh"

if is_loaded; then
  echo "refusing: $LABEL is loaded; stop it with stop.sh first" >&2
  exit 1
fi
rm -f "$TARGET"
echo "removed $LABEL"

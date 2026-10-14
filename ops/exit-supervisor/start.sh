#!/usr/bin/env bash
# Start the exit-only supervisor: an explicit operator action, audited.
#
#   ops/exit-supervisor/start.sh --confirm --reason CODE
#
# Enables and loads the installed agent; the first tick runs at once, then one
# every 60 seconds, and again after every login until stop.sh. It changes no
# configuration, starts no entry job and permits no trade: the supervisor only
# ever runs `--exits-once`.
set -euo pipefail
# shellcheck source=common.sh
source "$(dirname "${BASH_SOURCE[0]}")/common.sh"

confirm="" reason=""
while (($#)); do
  case "$1" in
    --confirm) confirm=1 ;;
    --reason) reason="${2:-}"; shift ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
  shift
done
if [[ -z "$confirm" || -z "$reason" ]]; then
  echo "refusing: start.sh needs --confirm and --reason CODE" >&2
  exit 2
fi
resolve_uv
require_runtime_main
if [[ ! -f "$TARGET" ]]; then
  echo "refusing: $LABEL is not installed (install.sh)" >&2
  exit 1
fi
if is_loaded; then
  echo "refusing: $LABEL is already loaded" >&2
  exit 1
fi
supervisor audit --action start --reason "$reason"
"$LAUNCHCTL" enable "$SERVICE"
if ! "$LAUNCHCTL" bootstrap "$DOMAIN" "$TARGET"; then
  # Never leave it enabled but unloaded: the next login would start it unaudited.
  "$LAUNCHCTL" disable "$SERVICE"
  echo "failed: $LABEL could not be loaded; left disabled" >&2
  exit 1
fi
echo "started $LABEL (exit job every 60 seconds); check with health.sh"

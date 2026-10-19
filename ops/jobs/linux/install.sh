#!/usr/bin/env bash
# Install the job units (Linux): V4 pool index, scout, entry -- disabled and stopped.
#
#   ops/jobs/linux/install.sh [--index-max-requests N]
#   ops/jobs/linux/install.sh --dry-run [...]   print the rendered units only
#
# The same order and guarantees as ops/exit-supervisor/linux/install.sh, which
# installs the exit supervisor (install that first; this script never touches
# it): every job timer and service known and idle, every loaded timer disabled
# before any file changes, the files replaced atomically, the timers disabled
# again after the reload, and the result verified. INSTALL_REFUSED (exit 1)
# while nothing changed, INSTALL_FAILED (exit 3) afterwards. Nothing is
# enabled, started or stopped.
#
# --index-max-requests bounds each 5-minute index increment (default 50,
# 1..2000); the backfill itself is a separate, approved manual run.
#
# Run as root (system units) from the runtime checkout on main.
set -euo pipefail
# shellcheck source=common.sh
source "$(dirname "${BASH_SOURCE[0]}")/common.sh"

dry_run="" index_max_requests="50"
while (($#)); do
  case "$1" in
    --dry-run) dry_run=1 ;;
    --index-max-requests) index_max_requests="${2:-}"; shift ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
  shift
done
if [[ ! "$index_max_requests" =~ ^[1-9][0-9]{0,3}$ ]] || ((index_max_requests > 2000)); then
  echo "refusing: --index-max-requests must be 1..2000" >&2
  exit 2
fi
if [[ ! "$REPO" =~ ^/[A-Za-z0-9._/-]+$ ]]; then
  echo "refusing: $REPO is not a plain absolute path" >&2
  exit 2
fi
if [[ ! "$SERVICE_USER" =~ ^[a-z_][a-z0-9_-]{0,31}$ ]]; then
  echo "refusing: invalid service user" >&2
  exit 2
fi

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

render() {
  sed \
    -e "s|__REPO__|$REPO|g" \
    -e "s|__USER__|$SERVICE_USER|g" \
    -e "s|__INDEX_MAX_REQUESTS__|$index_max_requests|g" \
    "$1"
}

source_of() {
  if [[ -f "$HERE/$1.template" ]]; then
    printf '%s\n' "$HERE/$1.template"
  else
    printf '%s\n' "$HERE/$1"
  fi
}

if [[ -n "$dry_run" ]]; then
  for unit in "${JOB_UNITS[@]}"; do
    echo "# ---- $unit"
    render "$(source_of "$unit")"
  done
  exit 0
fi
require_runtime_main

refuse() {
  echo "INSTALL_REFUSED: $*; nothing was changed" >&2
  exit 1
}
fail() {
  echo "INSTALL_FAILED: $*" >&2
  echo "INSTALL_FAILED: check every timer with: systemctl is-enabled ${JOB_TIMERS[*]}" >&2
  exit 3
}

# 1. Known and idle, every job unit.
for timer in "${JOB_TIMERS[@]}"; do
  state="$(unit_state "$timer")"
  [[ "$state" == "inactive" ]] ||
    refuse "$timer is $state; a running or unknown job is never replaced by an install"
  load="$(unit_property "$timer" LoadState)"
  [[ "$load" == "loaded" || "$load" == "not-found" ]] || refuse "$timer has load state $load"
  file_state="$(unit_property "$timer" UnitFileState)"
  if [[ "$load" == "loaded" ]]; then
    [[ "$file_state" == "enabled" || "$file_state" == "disabled" ]] ||
      refuse "$timer has unit file state ${file_state:-none}"
  fi
done
for service in "${JOB_SERVICES[@]}"; do
  state="$(unit_state "$service")"
  [[ "$state" == "inactive" || "$state" == "failed" ]] ||
    refuse "$service is $state; a running or unknown unit is never replaced by an install"
done

# 2. Disabled before any file changes.
for timer in "${JOB_TIMERS[@]}"; do
  if [[ "$(unit_property "$timer" LoadState)" == "loaded" ]]; then
    "$SYSTEMCTL" disable "$timer" || refuse "could not disable $timer"
    [[ "$(unit_property "$timer" UnitFileState)" == "disabled" ]] ||
      refuse "$timer is still $(unit_property "$timer" UnitFileState) after disable"
  fi
done

# 3. Replace the files, each atomically; then let systemd re-read them.
mkdir -p "$UNIT_DIR" || fail "cannot create $UNIT_DIR"
for unit in "${JOB_UNITS[@]}"; do
  { render "$(source_of "$unit")" >"$UNIT_DIR/$unit.tmp" &&
    chmod 0644 "$UNIT_DIR/$unit.tmp" &&
    mv "$UNIT_DIR/$unit.tmp" "$UNIT_DIR/$unit"; } 2>/dev/null ||
    fail "could not write $unit (timers are disabled)"
done
"$SYSTEMCTL" daemon-reload || fail "daemon-reload failed (timers are disabled)"

# 4. Disabled again, now that every timer has its file.
"$SYSTEMCTL" disable "${JOB_TIMERS[@]}" || fail "could not disable the timers after the reload"

# 5. Verify: nothing enabled, nothing running.
for timer in "${JOB_TIMERS[@]}"; do
  file_state="$(unit_property "$timer" UnitFileState)"
  state="$(unit_state "$timer")"
  [[ "$file_state" == "disabled" && "$state" == "inactive" ]] ||
    fail "$timer is ${file_state:-none}/$state after the install, expected disabled/inactive"
done
for service in "${JOB_SERVICES[@]}"; do
  state="$(unit_state "$service")"
  [[ "$state" == "inactive" || "$state" == "failed" ]] ||
    fail "$service is $state after the install"
done

echo "INSTALLED: ${#JOB_UNITS[@]} units, every job timer disabled and inactive; nothing started (start.sh --job JOB --confirm --reason CODE)"

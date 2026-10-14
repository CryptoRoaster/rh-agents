#!/usr/bin/env bash
# Install the exit-only supervisor's systemd units (Linux) — disabled and stopped.
#
#   ops/exit-supervisor/linux/install.sh [--host-alias NAME] [--heartbeat-target ALIAS]
#   ops/exit-supervisor/linux/install.sh --dry-run [...]   print the rendered units only
#
# Installing is not starting. In this order, each step verified, stopping at
# the first failure (INSTALL_REFUSED before anything changed, INSTALL_FAILED
# after):
#
#   1. every timer and service has a known state, and none is running;
#   2. every timer that has a unit file is disabled (an enabled but idle one
#      would otherwise start at the next boot with the new units);
#   3. the unit files are replaced, then systemd re-reads them;
#   4. every timer is disabled again (a stale enablement link of a timer that
#      had no file becomes live once its file is back), then
#   5. every timer reads disabled and inactive, every service not running.
#
# Nothing is enabled, started or stopped: a running unit is a refusal, never
# a reason to stop it. Starting is a separate operator action:
# start.sh --confirm --reason.
#
# Run as root (system units) from the runtime checkout on main.
set -euo pipefail
# shellcheck source=common.sh
source "$(dirname "${BASH_SOURCE[0]}")/common.sh"

dry_run="" host_alias="rh-vps" heartbeat_target="unset" heartbeat_port="22"
while (($#)); do
  case "$1" in
    --dry-run) dry_run=1 ;;
    --host-alias) host_alias="${2:-}"; shift ;;
    --heartbeat-target) heartbeat_target="${2:-}"; shift ;;
    --heartbeat-port) heartbeat_port="${2:-}"; shift ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
  shift
done
if [[ ! "$host_alias" =~ ^[a-z0-9][a-z0-9-]{0,31}$ ]]; then
  echo "refusing: --host-alias must be a short lowercase name, never a hostname or address" >&2
  exit 2
fi
if [[ ! "$heartbeat_target" =~ ^([a-z_][a-z0-9_-]{0,31}@)?[A-Za-z0-9][A-Za-z0-9.-]{0,252}$ ]]; then
  echo "refusing: --heartbeat-target must be [user@]host" >&2
  exit 2
fi
if [[ ! "$heartbeat_port" =~ ^[1-9][0-9]{0,4}$ ]] || ((heartbeat_port > 65535)); then
  echo "refusing: --heartbeat-port must be a port number" >&2
  exit 2
fi
# Rendered into unit files as is: plain absolute paths only.
for path in "$REPO" "$CREDENTIALS_DIR"; do
  if [[ ! "$path" =~ ^/[A-Za-z0-9._/-]+$ ]]; then
    echo "refusing: $path is not a plain absolute path" >&2
    exit 2
  fi
done
if [[ ! "$SERVICE_USER" =~ ^[a-z_][a-z0-9_-]{0,31}$ ]]; then
  echo "refusing: invalid service user" >&2
  exit 2
fi

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

render() {
  sed \
    -e "s|__REPO__|$REPO|g" \
    -e "s|__USER__|$SERVICE_USER|g" \
    -e "s|__HOST_ALIAS__|$host_alias|g" \
    -e "s|__HEARTBEAT_TARGET__|$heartbeat_target|g" \
    -e "s|__HEARTBEAT_PORT__|$heartbeat_port|g" \
    -e "s|__CREDENTIALS_DIR__|$CREDENTIALS_DIR|g" \
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
  for unit in "${UNITS[@]}"; do
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
  echo "INSTALL_FAILED: check every timer with: systemctl is-enabled ${TIMERS[*]}" >&2
  exit 3
}

# 1. Known and idle, every unit. A timer must be inactive; a service inactive
#    or failed (a failed oneshot runs nothing). Anything else refuses.
for timer in "${TIMERS[@]}"; do
  state="$(unit_state "$timer")"
  [[ "$state" == "inactive" ]] ||
    refuse "$timer is $state; a running or unknown supervisor is never replaced by an install"
  load="$(unit_property "$timer" LoadState)"
  [[ "$load" == "loaded" || "$load" == "not-found" ]] || refuse "$timer has load state $load"
  file_state="$(unit_property "$timer" UnitFileState)"
  if [[ "$load" == "loaded" ]]; then
    [[ "$file_state" == "enabled" || "$file_state" == "disabled" ]] ||
      refuse "$timer has unit file state ${file_state:-none}"
  fi
done
for service in "$SUPERVISOR_SERVICE" "$ALERT_SERVICE" "$HEARTBEAT_SERVICE"; do
  state="$(unit_state "$service")"
  [[ "$state" == "inactive" || "$state" == "failed" ]] ||
    refuse "$service is $state; a running or unknown unit is never replaced by an install"
done

# 2. Disabled before any file changes.
for timer in "${TIMERS[@]}"; do
  if [[ "$(unit_property "$timer" LoadState)" == "loaded" ]]; then
    "$SYSTEMCTL" disable "$timer" || refuse "could not disable $timer"
    [[ "$(unit_property "$timer" UnitFileState)" == "disabled" ]] ||
      refuse "$timer is still $(unit_property "$timer" UnitFileState) after disable"
  fi
done

# 3. Replace the files, each atomically; then let systemd re-read them.
mkdir -p "$UNIT_DIR" || fail "cannot create $UNIT_DIR"
for unit in "${UNITS[@]}"; do
  { render "$(source_of "$unit")" >"$UNIT_DIR/$unit.tmp" &&
    chmod 0644 "$UNIT_DIR/$unit.tmp" &&
    mv "$UNIT_DIR/$unit.tmp" "$UNIT_DIR/$unit"; } 2>/dev/null ||
    fail "could not write $unit (timers are disabled)"
done
"$SYSTEMCTL" daemon-reload || fail "daemon-reload failed (timers are disabled)"

# 4. Disabled again, now that every timer has its file.
"$SYSTEMCTL" disable "${TIMERS[@]}" || fail "could not disable the timers after the reload"

# 5. Verify: nothing enabled, nothing running.
for timer in "${TIMERS[@]}"; do
  file_state="$(unit_property "$timer" UnitFileState)"
  state="$(unit_state "$timer")"
  [[ "$file_state" == "disabled" && "$state" == "inactive" ]] ||
    fail "$timer is ${file_state:-none}/$state after the install, expected disabled/inactive"
done
for service in "$SUPERVISOR_SERVICE" "$ALERT_SERVICE" "$HEARTBEAT_SERVICE"; do
  state="$(unit_state "$service")"
  [[ "$state" == "inactive" || "$state" == "failed" ]] ||
    fail "$service is $state after the install"
done

if [[ ! -x "$PYTHON" ]]; then
  echo "note: $PYTHON is missing; run uv sync --locked in $REPO/backend before start.sh" >&2
fi
echo "INSTALLED: ${#UNITS[@]} units, every timer disabled and inactive; nothing started (start with start.sh --confirm --reason CODE)"

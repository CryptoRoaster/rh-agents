#!/usr/bin/env bash
# Install the exit-only supervisor's systemd units (Linux) — disabled and stopped.
#
#   ops/exit-supervisor/linux/install.sh [--host-alias NAME] [--heartbeat-target ALIAS]
#   ops/exit-supervisor/linux/install.sh --dry-run [...]   print the rendered units only
#
# Installing is not starting. The units are written, systemd re-reads them and
# every timer is marked disabled; nothing is enabled or started, now or at the
# next boot. Starting is a separate operator action: start.sh --confirm --reason.
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

for timer in "${TIMERS[@]}"; do
  if ! is_inactive "$timer"; then
    echo "refusing: $timer is $(unit_state "$timer"); a running supervisor is never replaced by an install" >&2
    exit 1
  fi
done

mkdir -p "$UNIT_DIR"
for unit in "${UNITS[@]}"; do
  render "$(source_of "$unit")" >"$UNIT_DIR/$unit.tmp"
  chmod 0644 "$UNIT_DIR/$unit.tmp"
  mv "$UNIT_DIR/$unit.tmp" "$UNIT_DIR/$unit"
done
"$SYSTEMCTL" daemon-reload
# Disabled, whatever an earlier install left: a boot starts nothing.
"$SYSTEMCTL" disable "${TIMERS[@]}"
if [[ ! -x "$PYTHON" ]]; then
  echo "note: $PYTHON is missing; run uv sync --locked in $REPO/backend before start.sh" >&2
fi
echo "installed ${#UNITS[@]} units (disabled, not started; start with start.sh --confirm --reason CODE)"

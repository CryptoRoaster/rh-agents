# Shared by the Linux (systemd) exit-supervisor operator scripts. Sourced, never executed.
# shellcheck shell=bash
#
# Layout (see docs/exit-supervisor-linux.md): the runtime checkout owned by the
# service user, its virtualenv at backend/.venv, system units in
# /etc/systemd/system, state under /var/lib/rh-agents, credentials (root-owned,
# 0600) under /etc/rh-agents/credentials. Every path can be overridden for tests.

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
SERVICE_USER="${RH_AGENTS_SERVICE_USER:-rh-agents}"
UNIT_DIR="${RH_AGENTS_SYSTEMD_UNIT_DIR:-/etc/systemd/system}"
SYSTEMCTL="${RH_AGENTS_SYSTEMCTL:-systemctl}"
PGREP="${RH_AGENTS_PGREP:-pgrep}"
STATE_ROOT="${RH_AGENTS_STATE_ROOT:-/var/lib/rh-agents}"
CREDENTIALS_DIR="${RH_AGENTS_CREDENTIALS_DIR:-/etc/rh-agents/credentials}"
PYTHON="${RH_AGENTS_PYTHON:-$REPO/backend/.venv/bin/python}"

SUPERVISOR_SERVICE="rh-agents-exit-supervisor.service"
SUPERVISOR_TIMER="rh-agents-exit-supervisor.timer"
ALERT_SERVICE="rh-agents-exit-alert.service"
ALERT_TIMER="rh-agents-exit-alert.timer"
HEARTBEAT_SERVICE="rh-agents-heartbeat.service"
HEARTBEAT_TIMER="rh-agents-heartbeat.timer"
TIMERS=("$SUPERVISOR_TIMER" "$ALERT_TIMER" "$HEARTBEAT_TIMER")
UNITS=(
  "$SUPERVISOR_SERVICE" "$SUPERVISOR_TIMER"
  "$ALERT_SERVICE" "$ALERT_TIMER"
  "$HEARTBEAT_SERVICE" "$HEARTBEAT_TIMER"
)

export RH_AGENTS_SUPERVISOR_BACKEND=systemd
export RH_AGENTS_SYSTEMCTL="$SYSTEMCTL"
export RH_AGENTS_EXIT_SUPERVISOR_STATE_DIR="$STATE_ROOT/exit-supervisor"
export RH_AGENTS_ALERT_STATE_DIR="$STATE_ROOT/alerts"

# Run as the service user: the database configuration and the state belong to
# it. As root (systemctl needs root for system units), drop to that user.
as_service_user() {
  if [[ "$(id -u)" -eq 0 ]]; then
    runuser -u "$SERVICE_USER" -- "$@"
  else
    "$@"
  fi
}

# The supervisor's own command line, run from the backend of this checkout.
supervisor() {
  (cd "$REPO/backend" && as_service_user "$PYTHON" -m src.supervisor.main "$@")
}

# One property of a unit; `unknown` when systemd cannot say. An empty answer
# is passed on as such (UnitFileState is empty for a unit with no file).
unit_property() {
  local value
  if ! value="$("$SYSTEMCTL" show "$1" --property="$2" --value 2>/dev/null)"; then
    value="unknown"
  fi
  printf '%s\n' "$value"
}

# A unit's ActiveState; `unknown` when systemd cannot say. Guards compare
# against the one state they accept, so an unknown answer always refuses.
unit_state() {
  local state
  state="$("$SYSTEMCTL" show "$1" --property=ActiveState --value 2>/dev/null)" || state=""
  [[ -n "$state" ]] || state="unknown"
  printf '%s\n' "$state"
}

is_inactive() { [[ "$(unit_state "$1")" == "inactive" ]]; }
is_active() { [[ "$(unit_state "$1")" == "active" ]]; }

# Whether a tick runs right now. Unknown counts as running.
tick_running() {
  local state
  state="$(unit_state "$SUPERVISOR_SERVICE")"
  [[ "$state" != "inactive" && "$state" != "failed" ]]
}

require_runtime_main() {
  local branch
  # As root on a checkout the service user owns, git refuses "dubious
  # ownership" unless told this one directory is meant.
  branch="$(git -c safe.directory="$REPO" -C "$REPO" symbolic-ref --quiet --short HEAD || echo DETACHED)"
  if [[ "$branch" != "main" && -z "${RH_AGENTS_ALLOW_NON_MAIN:-}" ]]; then
    echo "refusing: branch $branch; use the runtime checkout on main" >&2
    exit 1
  fi
}

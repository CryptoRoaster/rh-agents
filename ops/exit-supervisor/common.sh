# Shared by the exit-supervisor operator scripts. Sourced, never executed.
# shellcheck shell=bash

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
LABEL="com.rh-agents.exit-supervisor"
TARGET="${RH_AGENTS_LAUNCH_AGENTS:-$HOME/Library/LaunchAgents}/$LABEL.plist"
LOG_DIR="${RH_AGENTS_EXIT_SUPERVISOR_LOG_DIR:-$HOME/Library/Logs/rh-agents}"
STATE_DIR="${RH_AGENTS_EXIT_SUPERVISOR_STATE_DIR:-$HOME/.local/state/rh-agents/exit-supervisor}"
LAUNCHCTL="${RH_AGENTS_LAUNCHCTL:-launchctl}"
DOMAIN="gui/$(id -u)"
SERVICE="$DOMAIN/$LABEL"
export RH_AGENTS_EXIT_SUPERVISOR_STATE_DIR="$STATE_DIR"
export RH_AGENTS_LAUNCHCTL="$LAUNCHCTL"

resolve_uv() {
  UV="${RH_AGENTS_UV:-$(command -v uv || true)}"
  if [[ -z "$UV" ]]; then
    echo "uv not found; set RH_AGENTS_UV to its absolute path" >&2
    exit 1
  fi
}

# The supervisor's own command line, run from the backend of this worktree.
supervisor() {
  (cd "$REPO/backend" && "$UV" run --locked python -m src.supervisor.main "$@")
}

is_loaded() {
  "$LAUNCHCTL" print "$SERVICE" >/dev/null 2>&1
}

is_running() {
  # Read whole, then match: `grep -q` on a pipe would end early and, under
  # pipefail, turn a running tick into "not running".
  local printed line
  printed="$("$LAUNCHCTL" print "$SERVICE" 2>/dev/null || true)"
  while IFS= read -r line; do
    [[ "$line" =~ ^[[:space:]]*state\ =\ running$ ]] && return 0
  done <<<"$printed"
  return 1
}

require_runtime_main() {
  local branch
  branch="$(git -C "$REPO" symbolic-ref --quiet --short HEAD || echo DETACHED)"
  if [[ "$branch" != "main" && -z "${RH_AGENTS_ALLOW_NON_MAIN:-}" ]]; then
    echo "refusing: branch $branch; use the runtime worktree on main" >&2
    exit 1
  fi
}

#!/usr/bin/env bash
# Install the early-discovery scout as a per-user launchd agent (macOS).
#
#   ops/scout/install.sh            render, install and load the agent
#   ops/scout/install.sh --dry-run  print the rendered plist, change nothing
#
# Run it from the dedicated runtime worktree on main (for example
# /Volumes/Coding/rh-agents.worktrees/runtime-main), never from the
# development worktree: the agent executes the code of the worktree it was
# installed from, and run-scout.sh refuses to run once that is not main.
#
# Explicit and reversible: see uninstall.sh. Nothing is installed by tests.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
LABEL="com.rh-agents.scout"
TARGET="${RH_AGENTS_LAUNCH_AGENTS:-$HOME/Library/LaunchAgents}/$LABEL.plist"
LOG_DIR="${RH_AGENTS_SCOUT_LOG_DIR:-$HOME/Library/Logs/rh-agents}"
UV="${RH_AGENTS_UV:-$(command -v uv || true)}"
if [[ -z "$UV" ]]; then
  echo "uv not found; set RH_AGENTS_UV to its absolute path" >&2
  exit 1
fi

rendered="$(sed \
  -e "s|__REPO__|$REPO|g" \
  -e "s|__UV_DIR__|$(dirname "$UV")|g" \
  -e "s|__LOG_DIR__|$LOG_DIR|g" \
  "$REPO/ops/scout/$LABEL.plist.template")"

# Install only from a worktree on main: the agent runs this worktree's code,
# and it must be the dedicated runtime worktree, never a development checkout
# that may later switch branches. The dry run shows the plist either way.
BRANCH="$(git -C "$REPO" symbolic-ref --quiet --short HEAD || echo DETACHED)"
if [[ "${1:-}" != "--dry-run" && "$BRANCH" != "main" && -z "${RH_AGENTS_ALLOW_NON_MAIN:-}" ]]; then
  echo "refusing to install from branch $BRANCH; install from the runtime worktree on main" >&2
  exit 1
fi

if [[ "${1:-}" == "--dry-run" ]]; then
  printf '%s\n' "$rendered"
  exit 0
fi

mkdir -p "$(dirname "$TARGET")" "$LOG_DIR"
printf '%s\n' "$rendered" >"$TARGET"
plutil -lint "$TARGET" >/dev/null
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$TARGET"
echo "installed $LABEL -> $TARGET (every 15 minutes; logs in $LOG_DIR)"

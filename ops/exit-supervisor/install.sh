#!/usr/bin/env bash
# Install the exit-only supervisor as a per-user launchd agent (macOS) — disabled.
#
#   ops/exit-supervisor/install.sh            render and install, start nothing
#   ops/exit-supervisor/install.sh --dry-run  print the rendered plist, change nothing
#
# Installing is not starting. The service is marked disabled *before* its plist
# is written, so neither this script nor the next login loads it. Starting is
# a separate, explicit operator action: start.sh --confirm --reason CODE.
#
# Run it from the runtime worktree on main, never from a development worktree:
# the agent executes the code of the worktree it was installed from.
set -euo pipefail
# shellcheck source=common.sh
source "$(dirname "${BASH_SOURCE[0]}")/common.sh"
resolve_uv

# launchd's PATH: uv, the Codex CLI when one is installed (the runner checks its
# reasoning provider's executable), and the system directories.
path_value="$(dirname "$UV")"
CODEX="${RH_AGENTS_CODEX:-$(command -v codex || true)}"
if [[ -n "$CODEX" && "$(dirname "$CODEX")" != "$path_value" ]]; then
  path_value="$path_value:$(dirname "$CODEX")"
fi
path_value="$path_value:/usr/bin:/bin:/usr/sbin:/sbin"

rendered="$(sed \
  -e "s|__REPO__|$REPO|g" \
  -e "s|__PATH__|$path_value|g" \
  -e "s|__LOG_DIR__|$LOG_DIR|g" \
  -e "s|__STATE_DIR__|$STATE_DIR|g" \
  "$REPO/ops/exit-supervisor/$LABEL.plist.template")"

if [[ "${1:-}" == "--dry-run" ]]; then
  printf '%s\n' "$rendered"
  exit 0
fi
require_runtime_main

if is_loaded; then
  echo "refusing: $LABEL is loaded; a running supervisor is never replaced by an install" >&2
  exit 1
fi

# Disabled first, so there is no moment at which a login could load it.
"$LAUNCHCTL" disable "$SERVICE"
mkdir -p "$(dirname "$TARGET")" "$LOG_DIR"
mkdir -p -m 700 "$STATE_DIR"
printf '%s\n' "$rendered" >"$TARGET"
if command -v plutil >/dev/null 2>&1; then
  plutil -lint "$TARGET" >/dev/null
fi
echo "installed $LABEL -> $TARGET (disabled, not started; start with start.sh --confirm --reason CODE)"

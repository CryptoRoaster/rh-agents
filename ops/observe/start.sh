#!/usr/bin/env bash
# Start the local observation: install and load the launchd agent (macOS).
#
#   ops/observe/start.sh --dry-run   check the profile, print the plist, change nothing
#   ops/observe/start.sh --confirm   check the profile, clear STOP, install and load
#
# The first run starts one interval after loading (RunAtLoad is false). The
# agent runs the code of this checkout: start it from a clean checkout whose
# commit is on origin/main. Reversible with stop.sh.
set -euo pipefail

# shellcheck source=common.sh
source "$(dirname "${BASH_SOURCE[0]}")/common.sh"

refuse() { echo "OBSERVE_START_REFUSED: $1" >&2; exit 78; }

mode="${1:-}"
[[ "$mode" == "--dry-run" || "$mode" == "--confirm" ]] || refuse "pass --dry-run or --confirm"

INTERVAL="${RH_AGENTS_OBSERVE_INTERVAL_SECONDS-300}"
if [[ ! "$INTERVAL" =~ ^[0-9]{3,4}$ ]] || ((10#$INTERVAL < 300 || 10#$INTERVAL > 3600)); then
  refuse "RH_AGENTS_OBSERVE_INTERVAL_SECONDS must be whole seconds between 300 and 3600"
fi
INTERVAL="$((10#$INTERVAL))"

reason="$(observe_load)" || refuse "$reason"
observe_load >/dev/null
reason="$(observe_check)" || refuse "$reason"
# Only committed code runs: no tracked change and no untracked file in the
# code the scout loads (ops/ and backend/src); .venv and logs are elsewhere.
[[ -z "$(git -C "$REPO" status --porcelain -- ops backend/src)" ]] &&
  git -C "$REPO" diff --quiet HEAD -- || refuse OBSERVE_WORKTREE_MODIFIED
if [[ -z "${RH_AGENTS_OBSERVE_ALLOW_UNMERGED:-}" ]] &&
  ! git -C "$REPO" merge-base --is-ancestor HEAD origin/main 2>/dev/null; then
  refuse OBSERVE_COMMIT_NOT_ON_MAIN
fi

rendered="$(sed \
  -e "s|__REPO__|$REPO|g" \
  -e "s|__ENV_FILE__|$ENV_FILE|g" \
  -e "s|__STATE_DIR__|$STATE_DIR|g" \
  -e "s|__LOG_DIR__|$LOG_DIR|g" \
  -e "s|<integer>300</integer>|<integer>$INTERVAL</integer>|" \
  "$REPO/ops/observe/$LABEL.plist.template")"

if [[ "$mode" == "--dry-run" ]]; then
  printf '%s\n' "$rendered"
  exit 0
fi

mkdir -p "$LAUNCH_AGENTS" "$LOG_DIR" "$STATE_DIR"
chmod 0700 "$STATE_DIR"
printf '%s\n' "$rendered" >"$PLIST"
plutil -lint "$PLIST" >/dev/null
rm -f "$STOP_FILE"
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"
echo "started $LABEL (every $INTERVAL s, database $(observe_db), commit $(git -C "$REPO" rev-parse --short HEAD); logs in $LOG_DIR)"

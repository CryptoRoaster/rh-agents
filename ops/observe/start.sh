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
observe_code_clean || refuse OBSERVE_WORKTREE_MODIFIED
observe_code_merged || refuse OBSERVE_COMMIT_NOT_ON_MAIN

# Rendered paths must stay plain: no sed or XML metacharacter, no backslash.
plain_path='^/[-A-Za-z0-9 ._/@+]+$'
for path in "$REPO" "$ENV_FILE" "$STATE_DIR" "$LOG_DIR"; do
  [[ "$path" =~ $plain_path ]] || refuse "OBSERVE_PATH_NOT_PLAIN: $path"
done

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
staged="$(mktemp "$LAUNCH_AGENTS/.$LABEL.XXXXXX")"
printf '%s\n' "$rendered" >"$staged"
"$PLUTIL" -lint "$staged" >/dev/null || { rm -f "$staged"; refuse OBSERVE_PLIST_INVALID; }
mv "$staged" "$PLIST"
rm -f "$STOP_FILE"
"$LAUNCHCTL" bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
"$LAUNCHCTL" bootstrap "gui/$(id -u)" "$PLIST"
echo "started $LABEL (every $INTERVAL s, database $(observe_db), ORBIT reviews $(observe_get EARLY_SCOUT_MAX_ORBIT_REVIEWS_PER_RUN)/run $(observe_get EARLY_SCOUT_MAX_ORBIT_REVIEWS_PER_DAY)/day, commit $(git -C "$REPO" rev-parse --short HEAD); logs in $LOG_DIR)"

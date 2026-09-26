#!/usr/bin/env bash
# Remove the early-discovery scout launchd agent. Logs are left in place.
set -euo pipefail

LABEL="com.rh-agents.scout"
TARGET="${RH_AGENTS_LAUNCH_AGENTS:-$HOME/Library/LaunchAgents}/$LABEL.plist"
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
rm -f "$TARGET"
echo "removed $LABEL"

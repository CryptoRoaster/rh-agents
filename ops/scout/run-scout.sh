#!/usr/bin/env bash
# One scheduled early-discovery scout run: `python -m src.runner.main --scout-once`.
#
# Started by launchd (see install.sh). It runs the scout mode and nothing else:
# never the full PAPER run, no trading, no execution. Overlapping runs are prevented twice:
# launchd never starts a job again while it is still running, and the scout
# itself holds a PostgreSQL advisory lock for the whole run.
#
# Configuration comes from the same place a manual run reads it: the repository
# root `.env` (gitignored). An optional file outside the repository,
# $RH_AGENTS_SCOUT_ENV (default ~/.config/rh-agents/scout.env), is exported
# first, for values such as ANTHROPIC_API_KEY that must not live in any
# committed file. Nothing secret is written here or in the plist.
#
# The scout's exit code is this script's exit code, so launchd and the log both
# show a failed run as failed.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
LOG_DIR="${RH_AGENTS_SCOUT_LOG_DIR:-$HOME/Library/Logs/rh-agents}"
LOG="$LOG_DIR/scout.log"
# Bounded logs: rotate at 5 MB, keep three generations.
MAX_BYTES=$((5 * 1024 * 1024))
KEEP=3

mkdir -p "$LOG_DIR"
if [[ -f "$LOG" ]] && (($(wc -c <"$LOG") > MAX_BYTES)); then
  for ((i = KEEP - 1; i >= 1; i--)); do
    [[ -f "$LOG.$i" ]] && mv "$LOG.$i" "$LOG.$((i + 1))"
  done
  mv "$LOG" "$LOG.1"
fi

ENV_FILE="${RH_AGENTS_SCOUT_ENV:-$HOME/.config/rh-agents/scout.env}"
if [[ -f "$ENV_FILE" ]]; then
  set -a
  # shellcheck disable=SC1090
  source "$ENV_FILE"
  set +a
fi

UV="${RH_AGENTS_UV:-$(command -v uv || true)}"
if [[ -z "$UV" ]]; then
  echo "$(date -u +%FT%TZ) scout: uv not found" >>"$LOG"
  exit 127
fi

cd "$REPO/backend"
status=0
{
  echo "$(date -u +%FT%TZ) scout: start"
  "$UV" run --locked python -m src.runner.main --scout-once || status=$?
  echo "$(date -u +%FT%TZ) scout: exit $status"
} >>"$LOG" 2>&1
exit "$status"

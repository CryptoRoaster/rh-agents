#!/usr/bin/env bash
# One supervised exit tick: `python -m src.supervisor.main tick`.
#
# Started by launchd every 60 seconds (see install.sh, start.sh). The tick runs
# the PAPER exit job (`--exits-once`) as a bounded child and nothing else: no
# entry, no scout, no full run. Overlap is prevented three times: launchd never
# starts the job while it still runs, the tick kills its child past a hard
# wall clock, and the exit job holds its PostgreSQL advisory lock.
#
# Configuration comes from the repository root `.env`, as for a manual run. An
# optional file outside the repository, $RH_AGENTS_EXIT_SUPERVISOR_ENV
# (default ~/.config/rh-agents/exit-supervisor.env), is exported first, for
# values launchd's environment lacks. Nothing secret is written here.
#
# The tick's exit code is this script's exit code.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
LOG_DIR="${RH_AGENTS_EXIT_SUPERVISOR_LOG_DIR:-$HOME/Library/Logs/rh-agents}"
LOG="$LOG_DIR/exit-supervisor.log"
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

# The branch rule comes from the plist and is fixed before the optional env
# file is read: that file can add values, never lift the rule.
REQUIRED_BRANCH="${RH_AGENTS_EXIT_SUPERVISOR_REQUIRE_BRANCH:-}"
ENV_FILE="${RH_AGENTS_EXIT_SUPERVISOR_ENV:-$HOME/.config/rh-agents/exit-supervisor.env}"
if [[ -f "$ENV_FILE" ]]; then
  set -a
  # shellcheck disable=SC1090
  source "$ENV_FILE"
  set +a
fi

UV="${RH_AGENTS_UV:-$(command -v uv || true)}"
if [[ -z "$UV" ]]; then
  echo "$(date -u +%FT%TZ) exit-supervisor: uv not found" >>"$LOG"
  exit 127
fi

# The supervisor runs the code of the worktree it was installed from: only the
# runtime worktree on main, unmodified. A refusal writes no tick, so the
# heartbeat goes stale and health says so.
if [[ -n "$REQUIRED_BRANCH" ]]; then
  current="$(git -C "$REPO" symbolic-ref --quiet --short HEAD || echo DETACHED)"
  if [[ "$current" != "$REQUIRED_BRANCH" ]]; then
    echo "$(date -u +%FT%TZ) exit-supervisor: refused RUNTIME_NOT_ON_$(echo "$REQUIRED_BRANCH" | tr '[:lower:]' '[:upper:]')" >>"$LOG"
    exit 78
  fi
  if ! git -C "$REPO" diff --quiet HEAD --; then
    echo "$(date -u +%FT%TZ) exit-supervisor: refused RUNTIME_WORKTREE_MODIFIED" >>"$LOG"
    exit 78
  fi
fi

cd "$REPO/backend"
status=0
{
  echo "$(date -u +%FT%TZ) exit-supervisor: tick start"
  "$UV" run --locked python -m src.supervisor.main tick || status=$?
  echo "$(date -u +%FT%TZ) exit-supervisor: tick exit $status"
} >>"$LOG" 2>&1
exit "$status"

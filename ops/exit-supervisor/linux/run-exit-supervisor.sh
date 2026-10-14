#!/usr/bin/env bash
# One supervised exit tick under systemd: `python -m src.supervisor.main tick`.
#
# Started by rh-agents-exit-supervisor.timer every 60 seconds. The tick runs the
# PAPER exit job (`--exits-once`) as a bounded child and nothing else: no
# entry, no scout, no full run. Overlap is prevented three times: systemd never
# starts the oneshot unit while it still runs, the tick kills its child past a
# hard wall clock, and the exit job holds its PostgreSQL advisory lock. Past
# TimeoutStartSec systemd stops the whole control group as well.
#
# Configuration comes from the checkout's root `.env` (owner-only), as for a
# manual run. Output goes to the journal: codes only, never a secret.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
PYTHON="${RH_AGENTS_PYTHON:-$REPO/backend/.venv/bin/python}"
REQUIRED_BRANCH="${RH_AGENTS_EXIT_SUPERVISOR_REQUIRE_BRANCH:-}"

# The supervisor runs the code of the checkout it was installed from: only the
# runtime checkout on main, unmodified. A refusal writes no tick, so the
# heartbeat goes stale and health says so.
if [[ -n "$REQUIRED_BRANCH" ]]; then
  current="$(git -c safe.directory="$REPO" -C "$REPO" symbolic-ref --quiet --short HEAD || echo DETACHED)"
  if [[ "$current" != "$REQUIRED_BRANCH" ]]; then
    echo "exit-supervisor: refused RUNTIME_NOT_ON_$(echo "$REQUIRED_BRANCH" | tr '[:lower:]' '[:upper:]')"
    exit 78
  fi
  # Modified tracked files and untracked ones alike: an untracked module can
  # be imported as surely as a tracked one. Ignored files (.env, .venv) are not listed.
  if [[ -n "$(git -c safe.directory="$REPO" -C "$REPO" status --porcelain)" ]]; then
    echo "exit-supervisor: refused RUNTIME_WORKTREE_MODIFIED"
    exit 78
  fi
fi
if [[ ! -x "$PYTHON" ]]; then
  echo "exit-supervisor: refused PYTHON_MISSING"
  exit 127
fi

cd "$REPO/backend"
exec "$PYTHON" -m src.supervisor.main tick

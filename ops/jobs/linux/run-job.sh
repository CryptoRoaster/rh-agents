#!/usr/bin/env bash
# Run one job once, fail closed. Called by the job units only.
#
#   ops/jobs/linux/run-job.sh index|scout|entry
#
# Before anything starts, in this order (exit 78 = refused, nothing ran):
#   1. the checkout is on the required branch and has no modified or untracked
#      file (RH_AGENTS_JOB_REQUIRE_BRANCH, set to main by the units);
#   2. the backend's Python exists (exit 127 otherwise);
#   3. no other run of the same job holds its lock (a second run is skipped,
#      exit 0: systemd already never overlaps a oneshot unit, this also covers
#      a manual run next to the timer); a lock that fails otherwise is refused
#      as LOCK_FAILED;
#   4. entry only: the exit supervisor and its alert pass are active, and the
#      runner's --preflight reports ready (exit 0). Anything else refuses; a
#      stop arriving during these checks ends the wrapper (exit 130) before
#      the entry run.
# Then exactly one command, never another mode:
#   index  python -m src.agents.atlas.v4.index_job --chain robinhood --max-requests N
#   scout  python -m src.runner.main --scout-once
#   entry  python -m src.runner.main --entries-once
set -euo pipefail
# shellcheck source=common.sh
source "$(dirname "${BASH_SOURCE[0]}")/common.sh"

job="${1:-}"
case "$job" in
  index | scout | entry) ;;
  *) echo "run-job: refused UNKNOWN_JOB" >&2; exit 2 ;;
esac

refused() {
  echo "run-job $job: refused $1" >&2
  exit 78
}

if [[ -n "${RH_AGENTS_JOB_REQUIRE_BRANCH:-}" ]]; then
  branch="$(git -c safe.directory="$REPO" -C "$REPO" symbolic-ref --quiet --short HEAD || echo DETACHED)"
  [[ "$branch" == "$RH_AGENTS_JOB_REQUIRE_BRANCH" ]] ||
    refused "RUNTIME_NOT_ON_$(printf '%s' "$RH_AGENTS_JOB_REQUIRE_BRANCH" | tr '[:lower:]' '[:upper:]')"
  dirty="$(git -c safe.directory="$REPO" -C "$REPO" status --porcelain 2>/dev/null)" ||
    refused RUNTIME_WORKTREE_UNREADABLE
  [[ -z "$dirty" ]] || refused RUNTIME_WORKTREE_MODIFIED
fi
if [[ ! -x "$PYTHON" ]]; then
  echo "run-job $job: refused PYTHON_MISSING" >&2
  exit 127
fi

lock_dir="${RH_AGENTS_JOB_LOCK_DIR:-$JOBS_STATE_DIR}"
mkdir -p "$lock_dir" || refused LOCK_DIR_UNAVAILABLE
exec 9>"$lock_dir/$job.lock" || refused LOCK_UNAVAILABLE
# 75 = another run holds the lock: skipped. Any other failure of flock is not
# "already running" -- a job that silently never runs must not look healthy.
lock_status=0
"$FLOCK" -n -E 75 9 || lock_status=$?
if ((lock_status == 75)); then
  echo "run-job $job: skipped ALREADY_RUNNING"
  exit 0
elif ((lock_status != 0)); then
  refused LOCK_FAILED
fi

cd "$REPO/backend"
case "$job" in
  index)
    max="${RH_AGENTS_INDEX_MAX_REQUESTS:-}"
    if [[ ! "$max" =~ ^[1-9][0-9]{0,3}$ ]] || ((max > 2000)); then
      refused INDEX_MAX_REQUESTS_INVALID
    fi
    exec "$PYTHON" -m src.agents.atlas.v4.index_job --chain robinhood --max-requests "$max"
    ;;
  scout)
    exec "$PYTHON" -m src.runner.main --scout-once
    ;;
  entry)
    # A stop (SIGINT/SIGTERM) that arrives while the checks run ends this
    # wrapper; it never goes on to start the entry run afterwards.
    trap 'echo "run-job entry: stopped before the entry run" >&2; exit 130' INT TERM
    exits_supervised || refused EXIT_SUPERVISION_NOT_ACTIVE
    # Fail closed: the entry job starts only on a configuration the preflight
    # calls ready. Its output (codes only) goes to the journal.
    status=0
    "$PYTHON" -m src.runner.main --preflight || status=$?
    ((status == 0)) || refused "PREFLIGHT_NOT_READY_$status"
    exec "$PYTHON" -m src.runner.main --entries-once
    ;;
esac

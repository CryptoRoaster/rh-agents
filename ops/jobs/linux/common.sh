# Shared by the Linux (systemd) job scripts: the V4 pool-index job, the scout
# and the entry job. Sourced, never executed.
# shellcheck shell=bash
#
# Builds on the exit supervisor's layout and helpers (same checkout, service
# user, unit directory, state root, overrides for tests): this file sources
# ops/exit-supervisor/linux/common.sh and adds the job units. It never starts,
# stops or changes the exit supervisor; it only reads its state.

# shellcheck source=../../exit-supervisor/linux/common.sh
source "$(dirname "${BASH_SOURCE[0]}")/../../exit-supervisor/linux/common.sh"

FLOCK="${RH_AGENTS_FLOCK:-flock}"
JOBS_STATE_DIR="$STATE_ROOT/jobs"

INDEX_SERVICE="rh-agents-pool-index.service"
INDEX_TIMER="rh-agents-pool-index.timer"
SCOUT_SERVICE="rh-agents-scout.service"
SCOUT_TIMER="rh-agents-scout.timer"
ENTRY_SERVICE="rh-agents-entry.service"
ENTRY_TIMER="rh-agents-entry.timer"
JOB_TIMERS=("$INDEX_TIMER" "$SCOUT_TIMER" "$ENTRY_TIMER")
JOB_SERVICES=("$INDEX_SERVICE" "$SCOUT_SERVICE" "$ENTRY_SERVICE")
JOB_UNITS=(
  "$INDEX_SERVICE" "$INDEX_TIMER"
  "$SCOUT_SERVICE" "$SCOUT_TIMER"
  "$ENTRY_SERVICE" "$ENTRY_TIMER"
)

# The timer of a job name (index, scout, entry); empty for anything else.
job_timer() {
  case "$1" in
    index) printf '%s\n' "$INDEX_TIMER" ;;
    scout) printf '%s\n' "$SCOUT_TIMER" ;;
    entry) printf '%s\n' "$ENTRY_TIMER" ;;
    *) printf '\n' ;;
  esac
}

job_service() {
  case "$1" in
    index) printf '%s\n' "$INDEX_SERVICE" ;;
    scout) printf '%s\n' "$SCOUT_SERVICE" ;;
    entry) printf '%s\n' "$ENTRY_SERVICE" ;;
    *) printf '\n' ;;
  esac
}

# Exits are watched and alarmed: the exit-supervisor tick and the alert pass
# are both enabled and their timers active. Unknown counts as not watched.
exits_supervised() {
  is_active "$SUPERVISOR_TIMER" && is_active "$ALERT_TIMER"
}

# One audit line per operator action, codes only (no secret ever reaches here).
audit_job() {
  local action="$1" job="$2" reason="$3"
  mkdir -p "$JOBS_STATE_DIR" && chmod 0700 "$JOBS_STATE_DIR" &&
    printf '{"at":"%s","action":"%s","job":"%s","reason":"%s","actor":"%s"}\n' \
      "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$action" "$job" "$reason" "${SUDO_USER:-${USER:-UNKNOWN}}" \
      >>"$JOBS_STATE_DIR/audit.jsonl"
}

REASON_RE='^[A-Z][A-Z0-9_]{2,63}$'

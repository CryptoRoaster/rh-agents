# Shared by the local observation scripts (macOS). Sourced, never executed.
# shellcheck shell=bash
#
# The observation profile is the early-discovery scout (`--scout-once`) against
# an isolated database, with every trading gate left at its code default. This
# file holds the one place that decides whether a profile is acceptable:
# observe_load reads the env file through an allowlist and observe_check
# refuses anything that could reach a BUY, the runtime database or a model.

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
LABEL="com.rh-agents.observe"
ENV_FILE="${RH_AGENTS_OBSERVE_ENV:-$HOME/.config/rh-agents/observe.env}"
STATE_DIR="${RH_AGENTS_OBSERVE_STATE_DIR:-$HOME/Library/Application Support/rh-agents/observe}"
LOG_DIR="${RH_AGENTS_OBSERVE_LOG_DIR:-$HOME/Library/Logs/rh-agents}"
LAUNCH_AGENTS="${RH_AGENTS_LAUNCH_AGENTS:-$HOME/Library/LaunchAgents}"
PLIST="$LAUNCH_AGENTS/$LABEL.plist"
STOP_FILE="$STATE_DIR/STOP"
# A run in progress holds this directory (mkdir is atomic); its pid file names
# the wrapper that owns it.
LOCK_DIR="$STATE_DIR/run.lock"
PID_FILE="$LOCK_DIR/pid"
# The checkout's own virtualenv, as it is: never `uv run`, which would sync it.
PYTHON="${RH_AGENTS_OBSERVE_PYTHON:-$REPO/backend/.venv/bin/python}"
# An ORBIT budget, once approved, is still capped here (the code defaults).
ORBIT_MAX_PER_RUN=4
ORBIT_MAX_PER_DAY=96

# Every key the profile may set. Anything else in the env file is refused, so
# no trading gate, provider key or RPC URL can be switched on through it.
OBSERVE_KEYS=(
  DATABASE_URL TRADING_MODE PAPER_RUNNER_ENABLED EARLY_SCOUT_ENABLED
  MARKET_PROVIDER MARKET_CHAINS GECKOTERMINAL_MAX_REQUESTS
  EARLY_SCOUT_MAX_DISCOVERY_POOLS EARLY_SCOUT_DISCOVERY_PAGE2_ENABLED
  EARLY_SCOUT_MAX_DISCOVERY_PAGE2_PER_DAY EARLY_SCOUT_MAX_NEW_WATCHES_PER_RUN
  EARLY_SCOUT_V4_PREFILTER_ENABLED EARLY_SCOUT_HISTORY_MAX_REQUESTS_PER_RUN
  EARLY_SCOUT_MAX_REFRESH_MARKETS_PER_RUN EARLY_SCOUT_MAX_ORBIT_REVIEWS_PER_RUN
  EARLY_SCOUT_MAX_ORBIT_REVIEWS_PER_DAY REASONING_PROVIDER CODEX_EXECUTABLE
  RH_AGENTS_OBSERVE_MODEL_BUDGET_APPROVED
)

OBSERVE_DB_URL_RE='^postgresql\+asyncpg://[a-z_][a-z0-9_-]{0,62}@(localhost|127\.0\.0\.1)(:[0-9]{1,5})?/rh_agents_observe(_[a-z0-9_]{1,40})?$'

OBSERVE_SEEN=" "

# One profile value, empty when unset.
observe_get() { local name="OBS_$1"; printf '%s' "${!name-}"; }

# Read the env file without sourcing it: KEY=VALUE lines, comments and blank
# lines only. Prints a refusal code and returns 1 on anything else.
observe_load() {
  local line key value
  OBSERVE_SEEN=" "
  [[ -f "$ENV_FILE" ]] || { echo "OBSERVE_ENV_MISSING"; return 1; }
  while IFS= read -r line || [[ -n "$line" ]]; do
    [[ "$line" =~ ^[[:space:]]*(#.*)?$ ]] && continue
    if [[ ! "$line" =~ ^([A-Z][A-Z0-9_]*)=([A-Za-z0-9_.:/@+,-]*)$ ]]; then
      echo "OBSERVE_ENV_MALFORMED"
      return 1
    fi
    key="${BASH_REMATCH[1]}" value="${BASH_REMATCH[2]}"
    if [[ " ${OBSERVE_KEYS[*]} " != *" $key "* ]]; then
      echo "OBSERVE_ENV_KEY_NOT_ALLOWED_$key"
      return 1
    fi
    [[ "$OBSERVE_SEEN" != *" $key "* ]] || { echo "OBSERVE_ENV_KEY_REPEATED_$key"; return 1; }
    OBSERVE_SEEN="$OBSERVE_SEEN$key "
    printf -v "OBS_$key" '%s' "$value"
  done <"$ENV_FILE"
}

# The fixed observation guarantees. Prints the first refusal code and returns 1.
observe_check() {
  local pair key
  for pair in TRADING_MODE=OBSERVE PAPER_RUNNER_ENABLED=false EARLY_SCOUT_ENABLED=true \
    MARKET_PROVIDER=geckoterminal MARKET_CHAINS=robinhood REASONING_PROVIDER=codex; do
    key="${pair%%=*}"
    [[ "$(observe_get "$key")" == "${pair#*=}" ]] || { echo "OBSERVE_REQUIRES_${key}"; return 1; }
  done
  [[ "$(observe_get DATABASE_URL)" =~ $OBSERVE_DB_URL_RE ]] || { echo "OBSERVE_DATABASE_NOT_ISOLATED"; return 1; }
  # A model call costs money: no ORBIT review without an approved budget, and
  # an approved budget is explicit and capped.
  local per_run per_day
  per_run="$(observe_get EARLY_SCOUT_MAX_ORBIT_REVIEWS_PER_RUN)"
  per_day="$(observe_get EARLY_SCOUT_MAX_ORBIT_REVIEWS_PER_DAY)"
  [[ "$per_run" =~ ^[0-9]{1,4}$ && "$per_day" =~ ^[0-9]{1,4}$ ]] ||
    { echo "OBSERVE_MODEL_BUDGET_NOT_EXPLICIT"; return 1; }
  if [[ "$(observe_get RH_AGENTS_OBSERVE_MODEL_BUDGET_APPROVED)" != "yes" ]]; then
    ((10#$per_run == 0 && 10#$per_day == 0)) || { echo "OBSERVE_MODEL_BUDGET_NOT_APPROVED"; return 1; }
  elif ((10#$per_run > ORBIT_MAX_PER_RUN || 10#$per_day > ORBIT_MAX_PER_DAY)); then
    echo "OBSERVE_MODEL_BUDGET_ABOVE_CAP"
    return 1
  fi
  # Settings read `../.env` relative to backend/: a repository .env here would
  # mix the runtime configuration into the observation profile.
  [[ ! -e "$REPO/.env" && ! -L "$REPO/.env" ]] || { echo "OBSERVE_REPOSITORY_ENV_PRESENT"; return 1; }
  [[ "$PYTHON" == /* && -x "$PYTHON" ]] || { echo "OBSERVE_PYTHON_MISSING"; return 1; }
}

# The database name of the checked profile (no credentials are ever printed).
observe_db() { local url; url="$(observe_get DATABASE_URL)"; printf '%s\n' "${url##*/}"; }

# The scout's environment: the profile and nothing inherited but HOME and a
# fixed PATH. The wrapper's own RH_AGENTS_* keys never reach the scout.
observe_env_args() {
  local key
  printf '%s\n' "HOME=$HOME" "PATH=/usr/bin:/bin:/usr/sbin:/sbin" "LANG=en_US.UTF-8"
  for key in $OBSERVE_SEEN; do
    [[ "$key" == RH_AGENTS_* ]] || printf '%s\n' "$key=$(observe_get "$key")"
  done
}

# Only committed code runs: no tracked change, and no untracked or ignored file
# anywhere the scout could import from (ops/ and backend/, whose directory
# leads sys.path under `python -m`), except the virtualenv and tool caches.
observe_code_clean() {
  git -C "$REPO" diff --quiet HEAD -- || return 1
  [[ -z "$(git -C "$REPO" status --porcelain --untracked-files=all --ignored -- ops backend \
    ':(exclude)backend/.venv' ':(exclude)backend/.mypy_cache' ':(exclude)backend/.pytest_cache' \
    ':(exclude)backend/.ruff_cache' ':(exclude)**/__pycache__/**')" ]]
}

# The wrapper that holds the run lock, if it is alive and still a wrapper: a
# pid left behind by a crash and reused by another process is never trusted.
observe_running_pid() {
  local pid
  [[ -f "$PID_FILE" ]] || return 1
  pid="$(cat "$PID_FILE" 2>/dev/null || true)"
  [[ "$pid" =~ ^[0-9]+$ ]] && kill -0 "$pid" 2>/dev/null || return 1
  [[ "$(ps -o command= -p "$pid" 2>/dev/null)" == *run-observe.sh* ]] || return 1
  printf '%s\n' "$pid"
}

# Take the run lock for this shell. Returns 1 while another run holds it; a
# stale lock (a dead or foreign pid) is cleared once. A lock without a pid
# counts as held; status.sh reports one that stays.
observe_lock() {
  local attempt
  for attempt in 1 2; do
    if mkdir "$LOCK_DIR" 2>/dev/null; then
      echo $$ >"$PID_FILE"
      return 0
    fi
    # No pid yet: the holder is between mkdir and writing it. Held.
    [[ -f "$PID_FILE" ]] || return 1
    observe_running_pid >/dev/null && return 1
    rm -f "$PID_FILE"
    rmdir "$LOCK_DIR" 2>/dev/null || true
  done
  return 1
}

# Release the run lock, only if this shell owns it.
observe_unlock() {
  [[ "$(cat "$PID_FILE" 2>/dev/null || true)" == "$$" ]] || return 0
  rm -f "$PID_FILE"
  rmdir "$LOCK_DIR" 2>/dev/null || true
}

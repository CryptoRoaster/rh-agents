# Shared by the local observation scripts (macOS). Sourced, never executed.
# shellcheck shell=bash
#
# The observation profile is the early-discovery scout (`--scout-once`) against
# an isolated database, with every trading gate left at its code default. This
# file holds the one place that decides whether a profile is acceptable:
# observe_load reads the env file through an allowlist and observe_check
# refuses anything that could reach a BUY, the runtime database or a model.

# Every script runs in a clean environment: an inherited BASH_ENV, exported
# function, PATH, GIT_* or PG* variable could otherwise change what the checks
# below see. Until only these names are exported, PATH is the system one and no
# function is defined, the calling script re-executes itself once under
# `env -i` with just them. No inherited variable chooses a command: git and ps
# come from the system PATH, launchctl, plutil and Python are fixed, and psql
# and Codex are absolute paths from the profile. A BASH_ENV file of the shell
# that calls a script is still read once before this re-exec (bash reads it at
# startup); it cannot change the checks, and launchd sets none.
OBSERVE_PATH=/usr/bin:/bin:/usr/sbin:/sbin
OBSERVE_INHERIT_RE='^(HOME|USER|LOGNAME|TMPDIR|LANG|TERM|RH_AGENTS_OBSERVE_(ENV|STATE_DIR|LOG_DIR|INTERVAL_SECONDS|STOP_WAIT_SECONDS))$'
observe_keep=()
observe_dirty=""
[[ -z "$(declare -F)" ]] || observe_dirty=yes
for observe_name in $(compgen -e); do
  if [[ "$observe_name" =~ $OBSERVE_INHERIT_RE ]]; then
    observe_keep+=("$observe_name=${!observe_name}")
  elif [[ "$observe_name" == PATH ]]; then
    [[ "$PATH" == "$OBSERVE_PATH" ]] || observe_dirty=yes
  elif [[ ! "$observe_name" =~ ^(PWD|OLDPWD|SHLVL|_)$ ]]; then
    observe_dirty=yes
  fi
done
if [[ -n "$observe_dirty" ]]; then
  exec /usr/bin/env -i PATH="$OBSERVE_PATH" ${observe_keep[@]+"${observe_keep[@]}"} /bin/bash "$0" "$@"
fi
unset observe_keep observe_dirty observe_name

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
LABEL="com.rh-agents.observe"
ENV_FILE="${RH_AGENTS_OBSERVE_ENV:-$HOME/.config/rh-agents/observe.env}"
STATE_DIR="${RH_AGENTS_OBSERVE_STATE_DIR:-$HOME/Library/Application Support/rh-agents/observe}"
LOG_DIR="${RH_AGENTS_OBSERVE_LOG_DIR:-$HOME/Library/Logs/rh-agents}"
LAUNCH_AGENTS="$HOME/Library/LaunchAgents"
PLIST="$LAUNCH_AGENTS/$LABEL.plist"
STOP_FILE="$STATE_DIR/STOP"
# A run in progress holds this directory (mkdir is atomic); its pid file names
# the wrapper that owns it.
LOCK_DIR="$STATE_DIR/run.lock"
PID_FILE="$LOCK_DIR/pid"
# The checkout's own virtualenv, as it is: never `uv run`, which would sync it.
PYTHON="$REPO/backend/.venv/bin/python"
# launchd and plist tools, by absolute path (the tests replace these two lines
# in their own throwaway copy; nothing inherited can).
LAUNCHCTL=/bin/launchctl
PLUTIL=/usr/bin/plutil
# An ORBIT budget, once approved, is still capped here (the code defaults).
ORBIT_MAX_PER_RUN=4
ORBIT_MAX_PER_DAY=96

# Every key the profile may set. Anything else in the env file is refused, so
# no trading gate, provider key or RPC URL can be switched on through it.
OBSERVE_KEYS=(
  DATABASE_URL TRADING_MODE PAPER_RUNNER_ENABLED EARLY_SCOUT_ENABLED
  MARKET_PROVIDER MARKET_CHAINS GECKOTERMINAL_MAX_REQUESTS
  EARLY_SCOUT_MAX_DISCOVERY_POOLS EARLY_SCOUT_DISCOVERY_PAGE2_ENABLED
  EARLY_SCOUT_MAX_DISCOVERY_PAGE2_PER_DAY EARLY_SCOUT_NETWORK_CACHE_TTL_SECONDS
  EARLY_SCOUT_MAX_NEW_WATCHES_PER_RUN
  EARLY_SCOUT_V4_PREFILTER_ENABLED EARLY_SCOUT_HISTORY_MAX_REQUESTS_PER_RUN
  EARLY_SCOUT_MAX_REFRESH_MARKETS_PER_RUN EARLY_SCOUT_MAX_ORBIT_REVIEWS_PER_RUN
  EARLY_SCOUT_MAX_ORBIT_REVIEWS_PER_DAY REASONING_PROVIDER CODEX_EXECUTABLE
  RH_AGENTS_OBSERVE_MODEL_BUDGET_APPROVED RH_AGENTS_OBSERVE_PSQL
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
  # Commands named in the profile are absolute, never resolved from a cwd.
  for key in CODEX_EXECUTABLE RH_AGENTS_OBSERVE_PSQL; do
    [[ -z "$(observe_get "$key")" || "$(observe_get "$key")" == /* ]] || { echo "OBSERVE_${key}_NOT_ABSOLUTE"; return 1; }
  done
}

# git on the checkout, and on nothing else: no system or global configuration,
# the work tree pinned to the checkout (a configured `core.worktree` could
# point it at a clean copy), and none of the settings that let status or diff
# trust stat data or a monitor instead of looking (`core.fsmonitor`,
# `core.untrackedCache`, `core.trustctime`, `core.checkStat`). Content is
# checked apart, without filters (`observe_code_content`).
# Replace refs and grafts are ignored too: either could swap the tree the
# content check compares against, or make an unmerged commit look merged.
observe_git() {
  GIT_CONFIG_NOSYSTEM=1 GIT_CONFIG_GLOBAL=/dev/null \
    GIT_NO_REPLACE_OBJECTS=1 GIT_GRAFT_FILE=/dev/null git \
    -c advice.graftFileDeprecated=false -c core.fsmonitor=false -c core.untrackedCache=false \
    -c core.trustctime=true -c core.checkStat=default \
    --work-tree="$REPO" -C "$REPO" "$@"
}

# Every file HEAD tracks under ops/ and backend/ has exactly HEAD's content on
# disk, hashed without any filter, so neither stat data, the index nor a
# configured clean filter can vouch for it. A tracked link, a missing file or
# any difference fails, and so does a listing git did not finish.
observe_code_content() {
  local entry meta path rc="" actual i=0 paths=() wanted=() tab=$'\t'
  while IFS= read -r -d '' entry; do
    [[ -z "$rc" ]] || return 1
    if [[ "$entry" == RC=* ]]; then
      rc="${entry#RC=}"
      continue
    fi
    meta="${entry%%"$tab"*}" path="${entry#*"$tab"}"
    # "<mode> blob <hash>": only regular files are tracked here.
    [[ "$meta" =~ ^100(644|755)\ blob\ ([0-9a-f]{40})$ ]] || return 1
    wanted+=("${BASH_REMATCH[2]}")
    [[ -f "$REPO/$path" && ! -L "$REPO/$path" ]] || return 1
    paths+=("$REPO/$path")
  done < <(
    observe_git ls-tree -r -z --full-tree HEAD -- ops backend
    printf 'RC=%s\0' "$?"
  )
  [[ "$rc" == 0 && ${#paths[@]} -gt 0 ]] || return 1
  while IFS= read -r actual; do
    [[ $i -lt ${#wanted[@]} && "$actual" == "${wanted[$i]}" ]] || return 1
    i=$((i + 1))
  done < <(printf '%s\n' "${paths[@]}" | observe_git hash-object --no-filters --stdin-paths)
  [[ $i -eq ${#wanted[@]} ]]
}

# Only merged code runs: the checkout's commit must be on origin/main.
observe_code_merged() { observe_git merge-base --is-ancestor HEAD origin/main 2>/dev/null; }

# The database name of the checked profile (no credentials are ever printed).
observe_db() { local url; url="$(observe_get DATABASE_URL)"; printf '%s\n' "${url##*/}"; }

# The scout's environment: the profile and nothing inherited but HOME and a
# fixed PATH. The wrapper's own RH_AGENTS_* keys never reach the scout.
# Bytecode is read from, and written to, only `$OBSERVE_PYCACHE`: a fresh empty
# directory per run (`observe_pycache`), so no .pyc planted in a __pycache__
# of the checkout — which the clean check leaves out — is ever loaded.
observe_env_args() {
  local key
  printf '%s\n' "HOME=$HOME" "PATH=/usr/bin:/bin:/usr/sbin:/sbin" "LANG=en_US.UTF-8" \
    "PYTHONPYCACHEPREFIX=$OBSERVE_PYCACHE" "PYTHONDONTWRITEBYTECODE=1"
  for key in $OBSERVE_SEEN; do
    [[ "$key" == RH_AGENTS_* ]] || printf '%s\n' "$key=$(observe_get "$key")"
  done
}

# Finder metadata, and nothing that only carries its name: a regular file (no
# link, no directory) called exactly `.DS_Store` that starts with Finder's
# buddy-allocator header. Python imports no such file. Argument: a path
# relative to the checkout.
observe_finder_metadata() {
  local path="$REPO/$1"
  [[ "${1##*/}" == .DS_Store && -f "$path" && ! -L "$path" ]] || return 1
  [[ "$(LC_ALL=C head -c 8 "$path" | od -An -tx1 | tr -d ' \n')" == 0000000142756431 ]]
}

# Only committed code runs: no tracked change, and no untracked or ignored file
# anywhere the scout could import from (ops/ and backend/, whose directory
# leads sys.path under `python -m`), except the virtualenv, tool caches and
# Finder metadata (`observe_finder_metadata`), which Finder writes on its own
# whenever the checkout is opened in it. Known gap: the virtualenv itself is
# not checked, so a `.pth` file or a source-less `.pyc` planted in
# `backend/.venv` would still run; only someone who can already write to the
# checkout as this user can plant one.
observe_code_clean() {
  local entry rc=""
  local status=(observe_git status --porcelain -z --untracked-files=all --ignored -- ops backend
    ':(exclude)backend/.venv' ':(exclude)backend/.mypy_cache' ':(exclude)backend/.pytest_cache'
    ':(exclude)backend/.ruff_cache' ':(exclude)**/__pycache__/**')
  observe_git diff --quiet HEAD -- || return 1
  observe_code_content || return 1
  # An assume-unchanged or skip-worktree file hides its edit from diff and status.
  [[ -z "$(observe_git ls-files -v -- ops backend | grep -v '^H ')" ]] || return 1
  # Read NUL-separated, so no file name can split into entries of its own,
  # and followed by git's exit status: only a listing git finished counts.
  # An entry always starts with its two status letters, never with `RC=`.
  while IFS= read -r -d '' entry; do
    [[ -z "$rc" ]] || return 1
    if [[ "$entry" == RC=* ]]; then
      rc="${entry#RC=}"
      continue
    fi
    # Untracked or ignored, and Finder's: anything else is a change.
    [[ "${entry:0:3}" == '?? ' || "${entry:0:3}" == '!! ' ]] || return 1
    observe_finder_metadata "${entry:3}" || return 1
  done < <(
    "${status[@]}"
    printf 'RC=%s\0' "$?"
  )
  [[ "$rc" == 0 ]]
}

# One of this wrapper's bytecode prefixes: a real directory (no link) directly
# in the absolute state directory, named as `observe_pycache` names it.
observe_is_pycache() {
  [[ "$STATE_DIR" == /* && "$1" == "$STATE_DIR"/pycache.* && "${1#"$STATE_DIR"/}" != */* ]] &&
    [[ -d "$1" && ! -L "$1" ]]
}

# A fresh, empty bytecode prefix for this run, in the private state directory.
# Empty prefixes a killed run left behind are removed first. Nothing holding a
# file is ever removed, and nothing is deleted recursively.
observe_pycache() {
  local stale
  for stale in "$STATE_DIR"/pycache.*; do
    if observe_is_pycache "$stale"; then rmdir "$stale" 2>/dev/null || true; fi
  done
  OBSERVE_PYCACHE="$(mktemp -d "$STATE_DIR/pycache.XXXXXX")"
}

# Remove it again. PYTHONDONTWRITEBYTECODE keeps it empty, so rmdir suffices;
# one that is not empty is left for inspection.
observe_drop_pycache() {
  if [[ -n "${OBSERVE_PYCACHE:-}" ]] && observe_is_pycache "$OBSERVE_PYCACHE"; then
    rmdir "$OBSERVE_PYCACHE" 2>/dev/null || true
  fi
}

# The start time of a process, as ps prints it (empty when it is gone).
observe_started() { LC_ALL=C ps -o lstart= -p "$1" 2>/dev/null || true; }

# The wrapper that holds the run lock, if it is alive and still that wrapper:
# the pid file names its pid and start time, so a pid left behind by a crash
# and reused by another process is never trusted, whatever it is called.
observe_running_pid() {
  local pid started
  [[ -f "$PID_FILE" ]] || return 1
  { IFS= read -r pid && IFS= read -r started; } <"$PID_FILE" 2>/dev/null || return 1
  [[ "$pid" =~ ^[0-9]+$ && -n "$started" ]] && kill -0 "$pid" 2>/dev/null || return 1
  [[ "$(observe_started "$pid")" == "$started" ]] || return 1
  [[ "$(ps -o command= -p "$pid" 2>/dev/null)" == *run-observe.sh* ]] || return 1
  printf '%s\n' "$pid"
}

# Take the run lock for this shell. Returns 1 while another run holds it; a
# stale lock (a dead or foreign pid) is cleared once. A lock without a pid
# counts as held; status.sh reports one that stays.
observe_lock() {
  local attempt since staged="$STATE_DIR/pid.$$"
  # Without its own start time this run could not be told from a reused pid.
  since="$(observe_started $$)"
  [[ -n "$since" ]] || return 1
  for attempt in 1 2; do
    if mkdir "$LOCK_DIR" 2>/dev/null; then
      # Written aside and renamed in, so a reader sees no pid or a whole one.
      if printf '%s\n%s\n' $$ "$since" >"$staged" && mv "$staged" "$PID_FILE"; then
        return 0
      fi
      rm -f "$staged"
      rmdir "$LOCK_DIR" 2>/dev/null || true
      return 1
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
  [[ "$(head -n 1 "$PID_FILE" 2>/dev/null || true)" == "$$" ]] || return 0
  rm -f "$PID_FILE"
  rmdir "$LOCK_DIR" 2>/dev/null || true
}

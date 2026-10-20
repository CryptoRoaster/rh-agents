#!/usr/bin/env bash
# Health and status of the local observation (read-only; macOS).
#
#   ops/observe/status.sh
#
# Prints the agent's state, the STOP file, the run in progress, the latest
# scout runs and what was observed, then the trading invariants of the
# observation database. Never prints a credential.
#
# Exit: 0 healthy; 1 degraded (not loaded while not stopped, no run within two
# intervals, the latest run failed, a lock without a live run, or the profile
# is refused); 2 a trading invariant is violated (a trade case, order intent or
# position exists) or cannot be read.
# No -e: a database that cannot be read is reported, never a silent abort.
set -uo pipefail

# shellcheck source=common.sh
source "$(dirname "${BASH_SOURCE[0]}")/common.sh"

INTERVAL="${RH_AGENTS_OBSERVE_INTERVAL_SECONDS-300}"
[[ "$INTERVAL" =~ ^[0-9]{3,4}$ ]] || INTERVAL=300
INTERVAL="$((10#$INTERVAL))"
health=0
degraded() { echo "DEGRADED: $1"; ((health < 1)) && health=1; return 0; }

reason="$(observe_load)" || { echo "PROFILE_REFUSED: $reason"; exit 1; }
observe_load >/dev/null
reason="$(observe_check)" || { echo "PROFILE_REFUSED: $reason"; exit 1; }
# PATH is the system one here: psql comes from the profile.
PSQL="$(observe_get RH_AGENTS_OBSERVE_PSQL)"
PSQL="${PSQL:-psql}"
url="$(observe_get DATABASE_URL)"
role="${url#*://}" role="${role%%@*}"
hostpart="${url#*@}" hostpart="${hostpart%%/*}"
host="${hostpart%%:*}" port="5432"
[[ "$hostpart" == *:* ]] && port="${hostpart##*:}"
db="$(observe_db)"
export PGCONNECT_TIMEOUT=5 PGOPTIONS="-c statement_timeout=10000 -c default_transaction_read_only=on"
q() { "$PSQL" -X -At -v ON_ERROR_STOP=1 -h "$host" -p "$port" -U "$role" -d "$db" -c "$1"; }

echo "profile: ok (database $db, commit $(git -C "$REPO" rev-parse --short HEAD))"
loaded=no
"$LAUNCHCTL" print "gui/$(id -u)/$LABEL" >/dev/null 2>&1 && loaded=yes
echo "agent loaded: $loaded"
stopped=no
[[ -e "$STOP_FILE" ]] && stopped=yes
echo "STOP file: $stopped"
if pid="$(observe_running_pid)"; then echo "run in progress: pid $pid"; else echo "run in progress: none"; fi
if [[ -d "$LOCK_DIR" ]] && ! observe_running_pid >/dev/null; then degraded STALE_RUN_LOCK; fi
[[ "$loaded" == yes || "$stopped" == yes ]] || degraded AGENT_NOT_LOADED

echo "revision: $(q 'select version_num from alembic_version')"
echo "latest runs (started_at | status | stop | discovered | watches_created | provider_requests | errors):"
q "select to_char(started_at at time zone 'Europe/Berlin','YYYY-MM-DD HH24:MI:SS'), status, stop, discovered, watches_created, provider_requests, jsonb_array_length(errors) from scout_runs order by started_at desc limit 5" | sed 's/^/  /'
age="$(q "select coalesce(extract(epoch from now() - max(completed_at))::int, -1) from scout_runs")" || age=-1
latest="$(q "select coalesce((select status from scout_runs order by started_at desc limit 1), 'NONE')")" || latest=UNREADABLE
echo "seconds since last run: $age; latest status: $latest"
if [[ "$loaded" == yes && "$stopped" == no ]]; then
  if [[ "$age" == -1 ]] || ((age > 2 * INTERVAL)); then degraded NO_RECENT_RUN; fi
fi
[[ "$latest" == NONE || "$latest" == COMPLETED ]] || degraded "LATEST_RUN_$latest"
echo "today (UTC): $(q "select count(*) || ' runs, ' || coalesce(sum(provider_requests),0) || ' provider requests, ' || coalesce(sum(provider_http_attempts),0) || ' provider HTTP attempts, ' || coalesce(sum(orbit_reviews_started),0) || ' model reviews' from scout_runs where started_at >= date_trunc('day', now() at time zone 'utc') at time zone 'utc'")"
echo "watches by status: $(q "select coalesce(string_agg(status || '=' || n, ', ' order by status), 'none') from (select status, count(*) n from discovery_watches where not is_fixture group by status) s")"
echo "early candidates (PROMOTABLE): $(q "select count(*) from discovery_watches where status = 'PROMOTABLE' and not is_fixture")"

invariants="$(q "select (select count(*) from trade_cases) || ' ' || (select count(*) from order_intents) || ' ' || (select count(*) from positions)")" || invariants=""
if [[ ! "$invariants" =~ ^[0-9]+\ [0-9]+\ [0-9]+$ ]]; then
  echo "trading invariants: UNKNOWN (the observation database could not be read)"
  exit 2
fi
read -r cases intents positions <<<"$invariants"
echo "trading invariants: trade_cases=$cases order_intents=$intents positions=$positions"
if [[ "$cases" != 0 || "$intents" != 0 || "$positions" != 0 ]]; then
  echo "VIOLATED: the observation database holds trading state"
  exit 2
fi
((health == 0)) && echo "HEALTHY" || true
exit "$health"

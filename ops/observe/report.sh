#!/usr/bin/env bash
# Read-only measurement report of the local observation (macOS).
#
#   ops/observe/report.sh SINCE HOURS
#
# SINCE is the observation start as `YYYY-MM-DDTHH:MM:SSZ` (UTC), HOURS the
# window (24, 48 or 72). Every figure comes from the observation database,
# read in a read-only transaction; nothing is fetched from a provider. A
# figure the database cannot show is printed as UNKNOWN, and total discovery
# coverage is always UNKNOWN: it needs an independent reference.
set -uo pipefail

# shellcheck source=common.sh
source "$(dirname "${BASH_SOURCE[0]}")/common.sh"

since="${1:-}" hours="${2:-}"
[[ "$since" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$ ]] ||
  { echo "usage: report.sh YYYY-MM-DDTHH:MM:SSZ 24|48|72" >&2; exit 64; }
[[ "$hours" =~ ^(24|48|72)$ ]] || { echo "usage: report.sh YYYY-MM-DDTHH:MM:SSZ 24|48|72" >&2; exit 64; }
# A real calendar instant, not only its shape (BSD date, as on macOS).
[[ "$(LC_ALL=C date -j -u -f '%Y-%m-%dT%H:%M:%SZ' "$since" '+%Y-%m-%dT%H:%M:%SZ' 2>/dev/null)" == "$since" ]] ||
  { echo "usage: report.sh YYYY-MM-DDTHH:MM:SSZ 24|48|72 (not a valid instant)" >&2; exit 64; }

reason="$(observe_load)" || { echo "PROFILE_REFUSED: $reason"; exit 1; }
observe_load >/dev/null
reason="$(observe_check)" || { echo "PROFILE_REFUSED: $reason"; exit 1; }
PSQL="$(observe_get RH_AGENTS_OBSERVE_PSQL)"
PSQL="${PSQL:-psql}"
url="$(observe_get DATABASE_URL)"
role="${url#*://}" role="${role%%@*}"
hostpart="${url#*@}" hostpart="${hostpart%%/*}"
host="${hostpart%%:*}" port="5432"
[[ "$hostpart" == *:* ]] && port="${hostpart##*:}"
db="$(observe_db)"
export PGCONNECT_TIMEOUT=5 PGOPTIONS="-c statement_timeout=30000 -c default_transaction_read_only=on"
q() {
  "$PSQL" -X -At -F ' | ' -v ON_ERROR_STOP=1 -h "$host" -p "$port" -U "$role" -d "$db" \
    -v since="$since" -v hours="$hours" -c "$1"
}
# The window as SQL, from the validated arguments only.
W="started_at >= '$since'::timestamptz and started_at < '$since'::timestamptz + interval '$hours hours'"
F="first_seen_at >= '$since'::timestamptz and first_seen_at < '$since'::timestamptz + interval '$hours hours' and not is_fixture"
AGE="extract(epoch from first_seen_at - pool_created_at)"
V4="(market_payload->'pool_locator'->>'kind') = 'BYTES32_POOL_ID'"

echo "observation report: $since + ${hours} h (database $db, port $port, commit $(git -C "$REPO" rev-parse --short HEAD))"
# The first read decides: a database that cannot be read ends the report.
server="$(q "select split_part(version(),' ',2) || ', revision ' || (select version_num from alembic_version)")" ||
  { echo "DATABASE_UNREADABLE"; exit 1; }
echo "server: $server"
echo
echo "## scout runs"
echo "runs | completed | failed | of those never finished (RUN_INCOMPLETE) | stopped: $(q "select count(*), count(*) filter (where status='COMPLETED'), count(*) filter (where status='FAILED'), count(*) filter (where stop='RUN_INCOMPLETE'), count(*) filter (where status='STOPPED') from scout_runs where $W")"
echo "errors (code: runs):"
q "select e, count(*) from scout_runs, jsonb_array_elements_text(errors) e where $W group by e order by 2 desc" | sed 's/^/  /'
echo "longest pauses between runs (> 10 min; sleep, stop, outage):"
q "select to_char(prev at time zone 'Europe/Berlin','MM-DD HH24:MI'), to_char(started_at at time zone 'Europe/Berlin','MM-DD HH24:MI'), round(extract(epoch from started_at - prev)/60) || ' min' from (select started_at, lag(started_at) over (order by started_at) prev from scout_runs where $W) x where started_at - prev > interval '10 minutes' order by started_at - prev desc limit 10" | sed 's/^/  /'
echo
echo "## provider requests"
echo "logical requests | HTTP attempts | /networks pages | cache hits | cache refused | history requests: $(q "select coalesce(sum(provider_requests),0), coalesce(sum(provider_http_attempts),0), coalesce(sum(network_requests),0), coalesce(sum(network_cache_hits),0), coalesce(sum(network_cache_rejected),0), coalesce(sum(history_provider_requests),0) from scout_runs where $W")"
echo "HTTP attempts per UTC day:"
q "select (started_at at time zone 'utc')::date, sum(provider_http_attempts) from scout_runs where $W group by 1 order by 1" | sed 's/^/  /'
echo "429: history rate-limited reads $(q "select coalesce(sum(history_rate_limited),0) from scout_runs where $W"), runs with a page-2 refusal for 429 $(q "select count(*) from scout_runs where $W and discovery_gap_reasons ?| array['DISCOVERY_PAGE2_RATE_LIMITED','DISCOVERY_PAGE2_PROVIDER_RATE_LIMITED']"); a 429 on page 1 or a refresh is not recorded apart: UNKNOWN"
echo
echo "## discovery"
echo "pools discovered (run counters) | watches first seen | unique base tokens: $(q "select (select coalesce(sum(discovered),0) from scout_runs where $W), count(*), count(distinct market_payload->>'base_asset_id') from discovery_watches where $F")"
echo "V4 | V2/V3: $(q "select count(*) filter (where $V4), count(*) filter (where not $V4) from discovery_watches where $F")"
echo "page 2 reads | no-overlap polls | possible gaps: $(q "select coalesce(sum(discovery_page2_requests),0), coalesce(sum(discovery_no_overlap_polls),0), coalesce(sum(discovery_possible_gaps),0) from scout_runs where $W")"
echo "gap reasons (code: runs):"
q "select r, count(*) from scout_runs, jsonb_array_elements_text(discovery_gap_reasons) r where $W group by r order by 2 desc" | sed 's/^/  /'
echo "total discovery coverage: UNKNOWN (no independent reference)"
echo
echo "## age at first discovery (provider pool_created_at; unknown never estimated)"
echo "class | known | unknown | median s | P90 s | < 5 min | < 10 min"
for cls in "all:true" "V4:$V4" "V2/V3:not $V4"; do
  name="${cls%%:*}" cond="${cls#*:}"
  echo "$name | $(q "select count(*) filter (where pool_created_at is not null and $AGE >= 0), count(*) filter (where pool_created_at is null or $AGE < 0), coalesce(percentile_disc(0.5) within group (order by $AGE) filter (where pool_created_at is not null and $AGE >= 0)::bigint::text,'UNKNOWN'), coalesce(percentile_disc(0.9) within group (order by $AGE) filter (where pool_created_at is not null and $AGE >= 0)::bigint::text,'UNKNOWN'), coalesce(round(avg(($AGE < 300)::int) filter (where pool_created_at is not null and $AGE >= 0), 3)::text,'UNKNOWN'), coalesce(round(avg(($AGE < 600)::int) filter (where pool_created_at is not null and $AGE >= 0), 3)::text,'UNKNOWN') from discovery_watches where $F and ($cond)")"
done
echo "early candidates (V4, known age < 5 min at first sighting): $(q "select count(*) from discovery_watches where $F and $V4 and pool_created_at is not null and $AGE >= 0 and $AGE < 300")"
echo
echo "## VECTOR history and candidates"
echo "history checks | VECTOR sufficient | backoffs set | PROMOTABLE (new in runs): $(q "select coalesce(sum(history_checks),0), coalesce(sum(vector_sufficient),0), coalesce(sum(history_backoff_set),0), coalesce(sum(promotable_new),0) from scout_runs where $W")"
echo "watches now PROMOTABLE (first seen in window): $(q "select count(*) from discovery_watches where $F and status='PROMOTABLE'")"
echo
echo "## data quality"
echo "creation time after first sighting (must be 0): $(q "select count(*) from discovery_watches where $F and $AGE < 0")"
echo "duplicate watch streams (must be 0): $(q "select count(*) - count(distinct (provider,chain,network,pair_id,is_fixture)) from discovery_watches")"
echo "trading invariants (must be 0 0 0): $(q "select (select count(*) from trade_cases), (select count(*) from order_intents), (select count(*) from positions)")"

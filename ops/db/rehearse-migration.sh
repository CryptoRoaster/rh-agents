#!/usr/bin/env bash
# Backup, restore probe and migration rehearsal -- on a scratch copy only.
#
#   ops/db/rehearse-migration.sh --source-db NAME --scratch-db NAME --out FILE [--pg-bin DIR]
#
# 1. pg_dump -Fc of the source database to FILE (mode 0600). Read-only on the
#    source: the source is never migrated, restored into or dropped.
# 2. createdb of a NEW scratch database (refused if it exists) and pg_restore
#    of the dump into it: the backup is proven restorable.
# 3. On the scratch copy only: alembic current (the source's revision), then
#    alembic upgrade head, alembic current == head, alembic check, the pool
#    index tables present (and empty when the source predates 0026), the
#    paper account row intact.
# 4. Prints one line per step and REHEARSAL_PASSED; nothing is dropped. The
#    operator drops the scratch database with dropdb once satisfied.
#
# Connection: libpq environment (PGHOST, PGPORT, PGUSER) for every tool; no
# password and no URL is ever printed. pg_dump and pg_restore must have the
# server's major version (a pg_dump older than the server refuses to dump:
# pass --pg-bin, e.g. /usr/lib/postgresql/17/bin).
# Exit: 0 passed, 1 a step failed (scratch kept for inspection), 2 refused.
set -euo pipefail

source_db="" scratch_db="" out="" pg_bin=""
while (($#)); do
  case "$1" in
    --source-db) source_db="${2:-}"; shift ;;
    --scratch-db) scratch_db="${2:-}"; shift ;;
    --out) out="${2:-}"; shift ;;
    --pg-bin) pg_bin="${2:-}"; shift ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
  shift
done

refuse() { echo "REHEARSAL_REFUSED: $*" >&2; exit 2; }
failed() { echo "REHEARSAL_FAILED: $* (scratch database $scratch_db kept for inspection)" >&2; exit 1; }

name_re='^[a-z_][a-z0-9_]{0,62}$'
[[ "$source_db" =~ $name_re ]] || refuse "--source-db must be a plain database name"
[[ "$scratch_db" =~ $name_re ]] || refuse "--scratch-db must be a plain database name"
[[ "$scratch_db" != "$source_db" ]] || refuse "the scratch database must not be the source"
[[ "$scratch_db" == *rehearsal* || "$scratch_db" == *scratch* ]] ||
  refuse "the scratch database name must contain 'rehearsal' or 'scratch'"
[[ -n "$out" ]] || refuse "--out FILE is required"
[[ ! -e "$out" ]] || refuse "$out exists; never overwrite a backup"

tool() { if [[ -n "$pg_bin" ]]; then printf '%s/%s\n' "$pg_bin" "$1"; else printf '%s\n' "$1"; fi; }
PG_DUMP="$(tool pg_dump)" PG_RESTORE="$(tool pg_restore)" CREATEDB="$(tool createdb)" PSQL="$(tool psql)"
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
# The checkout's own virtualenv, as it is: never `uv run`, which would sync
# (and could change) the environment the runtime units use.
PYTHON="${RH_AGENTS_PYTHON:-$REPO/backend/.venv/bin/python}"
[[ -x "$PYTHON" ]] || refuse "$PYTHON is missing; build the virtualenv first"

server_major="$("$PSQL" -X -A -t -d "$source_db" -c 'SHOW server_version_num' 2>/dev/null)" ||
  refuse "the source database cannot be read"
server_major="$((server_major / 10000))"
dump_major="$("$PG_DUMP" --version | sed -E 's/.* ([0-9]+)(\.[0-9]+)*.*/\1/')"
((dump_major == server_major)) ||
  refuse "pg_dump $dump_major does not match server $server_major; pass --pg-bin"
exists="$("$PSQL" -X -A -t -d "$source_db" -c "SELECT count(*) FROM pg_database WHERE datname = '$scratch_db'")" ||
  refuse "cannot check whether $scratch_db exists"
[[ "$exists" == "0" ]] || refuse "$scratch_db exists; a rehearsal never reuses a database"

# 1. Backup (read-only on the source), written under a temporary name and moved
#    into place only when complete and only if nothing appeared there meanwhile.
partial="$out.partial.$$"
(umask 077 && "$PG_DUMP" -Fc --no-owner -d "$source_db" -f "$partial") ||
  refuse "pg_dump failed; an incomplete file may remain at $partial, no database was created"
[[ ! -e "$out" ]] || refuse "$out appeared during the dump; the new dump stays at $partial"
mv "$partial" "$out" || refuse "could not move the dump into place; it stays at $partial"
echo "1 backup: $(wc -c <"$out" | tr -d ' ') bytes, $("$PG_RESTORE" -l "$out" | grep -c 'TABLE DATA') tables with data"

# 2. Restore probe into a new scratch database.
"$CREATEDB" "$scratch_db" || refuse "createdb $scratch_db failed"
"$PG_RESTORE" --no-owner --exit-on-error -d "$scratch_db" "$out" || failed "pg_restore"
echo "2 restore: $scratch_db restored"

# 3. Migrations on the scratch copy only.
# The same server the libpq tools reach: a socket directory as a host query,
# a host name as the authority, and nothing at all when PGHOST is unset (both
# sides then search the default socket and honour PGPORT).
user="${PGUSER:-$(id -un)}"
if [[ "${PGHOST:-}" == /* ]]; then
  url="postgresql+asyncpg://$user@/$scratch_db?host=$PGHOST"
elif [[ -n "${PGHOST:-}" ]]; then
  url="postgresql+asyncpg://$user@$PGHOST${PGPORT:+:$PGPORT}/$scratch_db"
else
  url="postgresql+asyncpg://$user@/$scratch_db"
fi
[[ "$url" == *"/$scratch_db" || "$url" == *"/$scratch_db?"* ]] || failed "could not address the scratch database"
migrate() { (cd "$REPO/backend" && DATABASE_URL="$url" "$PYTHON" -m alembic "$@"); }
before="$(migrate current 2>/dev/null | tail -1)" || failed "alembic current"
echo "3 revision before: ${before:-none}"
migrate upgrade head >/dev/null 2>&1 || failed "alembic upgrade head"
after="$(migrate current 2>/dev/null | tail -1)" || failed "alembic current"
heads="$(migrate heads 2>/dev/null | tail -1)" || failed "alembic heads"
[[ "${after%% *}" == "${heads%% *}" ]] || failed "current $after is not head $heads"
migrate check >/dev/null 2>&1 || failed "alembic check: the schema differs from the models"
index="$("$PSQL" -X -A -t -d "$scratch_db" -c 'SELECT (SELECT count(*) FROM v4_pool_index_coverage) + (SELECT count(*) FROM v4_pool_index_pools)')" ||
  failed "pool index tables missing"
# Before 0026 there was no index: the migration must have created it empty.
if [[ "${before%% *}" < "0026" ]]; then
  [[ "$index" == "0" ]] || failed "the new pool index tables are not empty: $index rows"
fi
accounts="$("$PSQL" -X -A -t -d "$scratch_db" -c 'SELECT count(*) FROM paper_accounts')" ||
  failed "paper account unreadable"
[[ "$accounts" == "1" ]] || failed "expected one paper account row, found $accounts"
echo "3 revision after: $after; schema check clean; pool index rows: $index; paper account rows: $accounts"
echo "REHEARSAL_PASSED (drop the scratch database when done: dropdb $scratch_db)"

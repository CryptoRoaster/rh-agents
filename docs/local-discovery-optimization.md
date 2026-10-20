# Local discovery optimisation

Follow-up to the first live observation test on 2026-10-19 (two scout runs,
eight HTTP requests, all 200). That test found three problems:

- **Network directory.** Every run made three `/networks` requests, and page 2
  was always blocked.
- **Pool creation time.** The scout did not store `pool_created_at`.
- **Finder metadata.** A Finder `.DS_Store` file in `backend/` stopped every
  run with `OBSERVE_WORKTREE_MODIFIED`.

This document records how each was fixed and gives a proposed operating
profile for continuous observation. **Nothing here starts a job or approves a
provider budget.**

## 1. Why three `/networks` requests per run

GeckoTerminal's `/networks` list is paginated. Robinhood is on page 3, after
BSC on page 1 (`docs/phase-1.md`). `NetworkDirectory` scans pages until it
finds the configured id, so validating Robinhood costs three requests.

The scout created a new directory for every run and passed it no registry. As
a result:

- The scan was repeated on every run.
- With `GECKOTERMINAL_MAX_REQUESTS=5`, the scan plus page 1 left one request.
- Page 2 needs that request plus one in reserve for the refresh, so it was
  always refused with `DISCOVERY_PAGE2_RUN_BUDGET`.

The outcome sampler builds its own directory and would cost three more
requests. It is off in the observation profile.

## 2. Network binding cache

`EARLY_SCOUT_NETWORK_CACHE_TTL_SECONDS` sets how long a validated binding is
kept. The default is 0 (no cache) and the maximum is 86 400.

**How a binding is stored.** After `NetworkDirectory` validates a binding, it
is written to `provider_network_bindings` with these fields: provider, chain,
network id, platform and `verified_at`. Only a successful validation is
written. The validation itself is unchanged: it checks the paginated
directory, the configured id and the chain's platform.

**When a stored binding is reused.** All of these must hold:

- the provider is `geckoterminal`;
- the chain is configured;
- the id equals the currently configured id;
- the platform equals the chain's platform;
- `0 ≤ now − verified_at < TTL`.

**When a stored binding is refused.** A row that fails any condition above is
refused, for example:

- a different id or platform;
- an expired entry;
- an entry dated in the future.

A refused row is counted in `network_cache_rejected`, and the chain is
validated again the usual way. If that validation fails, nothing is stored
and the chain is not read (fail-closed).

**When a binding is deleted.** On a chain answered from the cache, a client
error or contract error on `new_pools` (for example a 404) deletes the
binding, so the next run validates the chain again.

**Base URL.** The provider base URL is not part of the key. The observation
profile cannot set `GECKOTERMINAL_BASE_URL` (it is not on the allowlist), so
the stored binding always comes from the public API.

**Dating.** `verified_at` is the run's start, before the validation, so the
TTL never runs longer than from the validation itself.

**Request count.** With the cache, a run makes:

| Read | Requests |
| --- | --- |
| Page 1 | 1 |
| Page 1 + page 2 | 2 |
| Refresh | +1 if due |
| Directory scan | +3, once per TTL |

## 3. Page 2

Page 2 is still read **once at most** per run, and only when page 1 is
saturated: page 1 was full (20 pools), processed whole, and shared no pool
with anything recorded before the run. The existing limits stay in place:
the daily limit (`EARLY_SCOUT_MAX_DISCOVERY_PAGE2_PER_DAY`, at most 288) and
the run budget, including the reserve for the refresh.

Two refusals are new:

- `DISCOVERY_PAGE2_RATE_LIMITED`: the run received a 429, even if a retry then
  succeeded. No optional page is requested after that.
- `DISCOVERY_PAGE2_RUN_BUDGET` also applies when the remaining HTTP attempts,
  retries included, cannot cover page 2 and the reserve.

Every refusal is recorded as a possible gap, with its reason. The page is
never widened, and there is no page 3 (`MAX_DISCOVERY_PAGE = 2`).

**Metrics in `scout_runs`.** These columns already existed:

- `discovery_no_overlap_polls`
- `discovery_page2_requests`
- `discovery_possible_gaps`

Migration 0027 adds:

- `discovery_gap_reasons`
- `network_requests`
- `network_cache_hits`
- `network_cache_rejected`
- `provider_http_attempts`: every HTTP attempt on all of the run's transports,
  retries included.

**Restart.** A restart creates no duplicate watches:

- The overlap reference is the set of recorded observations.
- The page-2 daily count is the sum over `scout_runs`.
- The binding is a database row.

## 4. Coverage

**Offline model.** `src/scout/coverage.py` (`simulate_discovery`) replays the
observed rate under the scout's own page rule. The model assumes about 6 new
pools a minute, a 300 s poll and 20 pools a page.

| Profile | Captured of 30/poll | Possible-gap polls per day | `new_pools` requests per day |
| --- | --- | --- | --- |
| Page 1 only | 20 (66.7 %) | 287 | 288 |
| Page 2, at most 72 a day (old example) | 20 + 10 on 72 polls (≈ 75 %) | 215 | 360 |
| Page 2, at most 288 a day | 30 (100 % after the first poll) | 0 | 575 |
| 180 s poll, page 1 only | 18 of 18 (100 %) | 0 | 480 |

**Bursts.** A burst of more than 40 new pools between two polls is still lost
in part. It is recorded as a gap (`DISCOVERY_PAGE2_NO_OVERLAP`).

**Measured coverage.** `measured_coverage` compares observed pools against a
reference set. **Without a reference set, total coverage is UNKNOWN.**
Discovery alone cannot see what it missed. A poll with page 2 that reaches a
pool already recorded shows no gap; a recorded gap shows that pools may be
missing, not how many.

## 5. Pool creation time

`discovery_watches.pool_created_at` is filled from the provider's
`pool_created_at`.

**Accepted values.** Only an ISO 8601 instant with an explicit offset that is
not after the fetch is accepted.

**UNKNOWN values.** Anything else is stored as NULL, which means UNKNOWN:

- absent or null;
- a number;
- a time without an offset;
- a malformed string;
- a time after the fetch.

A pool with an unknown creation time is never dropped for that reason.

**How it is written.** The value is written once. A later answer only fills a
NULL, and never with a time after `first_seen_at`.

**Report.** `src/scout/pool_age.py` (`pool_age_report`) is read-only. It
reports the age at first discovery (`first_seen_at − pool_created_at`):

- known and unknown counts;
- the median (lower middle value);
- P90 (nearest rank);
- the share under 5 minutes and under 10 minutes;
- separate figures for all pools, V4 (bytes32 pool id) and V2/V3 (20-byte
  address).

A statistic with no known value is UNKNOWN, not 0.

## 6. Finder metadata

`observe_code_clean` exempts one thing only: an untracked or ignored
**regular file** (not a link, not a directory) named exactly `.DS_Store`
whose first eight bytes are Finder's `\0\0\0\1Bud1` header. Python imports no
such file.

Everything else is still refused. Negative tests cover each of these:

- tracked changes, including a tracked file overwritten with Finder bytes;
- `.DS_Store` containing code;
- an empty `.DS_Store`;
- `.DS_Store.py` and `x.DS_Store`;
- a `.DS_Store` directory, ignored or not;
- a `.DS_Store` symlink;
- a `.pth` file carrying the Finder header;
- a module next to a valid `.DS_Store`.

The git status is read NUL-separated, and a failing `git status` refuses the
run.

## 7. One daily ceiling on every provider HTTP attempt

Discovery/refresh (at most 5 logical requests, 8 HTTP attempts a run) and the
VECTOR OHLCV history (at most 6 requests, 8 attempts a run, its own paced
transport) each keep their per-run budgets.
`EARLY_SCOUT_MAX_PROVIDER_HTTP_ATTEMPTS_PER_DAY` adds one ceiling over all of
them for the UTC day.

**How the ceiling is applied.**

- The attempts recorded by the earlier runs of the day
  (`scout_runs.provider_http_attempts`) are subtracted from it.
- The rest is a single allowance. Every transport of the run takes from it
  before each attempt, retries included.
- Once it is spent, the next request fails with `REQUEST_BUDGET_EXHAUSTED`.
  For discovery this counts as a provider failure. For history it stops the
  run's reads without setting a backoff.
- Page 2 also needs it to be large enough.
- The next UTC day starts fresh at the ceiling. Unused attempts are never
  carried over.

**What the observation profile enforces.** It must set the ceiling explicitly,
to a value from 1 to 5760, or the run is refused with
`OBSERVE_HTTP_DAILY_BUDGET_NOT_EXPLICIT`. `status.sh` shows the day's
attempts.

**Reservation.** Before its first attempt, a run reserves its share:
the most it can spend, which is 8 attempts per transport it may use, capped
by what the day has left. The reservation is its own `scout_runs` row
(`FAILED`, `RUN_INCOMPLETE`), committed first; the run's final record
replaces it with what was actually spent. A run that is killed, cancelled
or crashes keeps its reservation in the day's sum, so the ceiling holds
whatever becomes of a run. `status.sh` does not report the reservation of
the run in progress as a failed run.

**Spent day.** When nothing is left at a run's start, the run makes no
provider request at all, records `stop = PROVIDER_HTTP_DAILY_BUDGET_REACHED`
and is otherwise a normal completed run.

**Several processes.** A run reads the day's sum, reserves its share and
records its end while holding the scout's PostgreSQL advisory lock. A
second run that starts meanwhile gives way (`ALREADY_RUNNING`) without any
request. A killed process releases the lock with its connection, and its
committed reservation stays in the sum. The ceiling counts what is recorded
in one database: runs against another database have their own count.

**Scope.** Only the observation profile requires the ceiling. The runtime
scout (`ops/scout`) and the Linux jobs keep the default 0 (no daily ceiling)
until they are configured for it.

The ceiling is not an approval to spend it.

## 8. Measurement plan (24 / 48 / 72 h)

`ops/observe/report.sh START HOURS` prints one report for a window. START is
the observation start in UTC (`YYYY-MM-DDTHH:MM:SSZ`), and HOURS is 24, 48 or
72. The report reads the observation database in a read-only transaction and
makes no provider call. It contains:

- scout runs: completed, failed and stopped; errors; pauses over 10 minutes
  (sleep, stop, outage);
- requests: logical requests, HTTP attempts (in total and per UTC day),
  `/networks` pages, network cache hits and refusals, history requests, and
  429s where they are recorded (history, page 2);
- discovery: pools, new watches, unique base tokens, V4 against V2/V3, page-2
  reads, polls without overlap, possible gaps and their reasons;
- age at first discovery: known and unknown counts, median, P90, share under
  5 and under 10 minutes for all pools, V4 and V2/V3, and early V4 candidates;
- VECTOR history checks, sufficiency, backoffs and PROMOTABLE watches;
- data quality: creation times after the first sighting, duplicate streams,
  and the trading invariants.

Some figures are UNKNOWN and are printed as UNKNOWN:

- **Total discovery coverage** needs an independent reference.
- **A 429 on page 1 or on a refresh** is not recorded on its own.
- **Runs from before migration 0027** carry neither HTTP attempts nor creation
  times. Set START after the first run on 0027.

## 9. Proposed operating profile (not activated)

| Item | Proposal | Reason |
| --- | --- | --- |
| Poll interval | 300 s (unchanged) | 30 pools/poll fit two pages; a shorter interval only adds runs |
| Requests per poll | `GECKOTERMINAL_MAX_REQUESTS=5` (unchanged) | Expected 2 (page 1 + page 2), +1 refresh if due, +3 once per TTL; OHLCV on its own transport, at most 6 |
| HTTP attempts | `GECKOTERMINAL_MAX_HTTP_ATTEMPTS=8` (default), 1 retry | Retries stay bounded; a 429 blocks page 2 |
| Requests per day | Discovery about 575 + directory 12 + refresh ≤ 288 → **≤ about 900**; OHLCV ≤ 1 728; **ceiling 2 700 logical / 5 760 HTTP** | About 1–2 calls/min on average against a public limit of about 30/min (NOT VERIFIED) |
| Page-2 limit | `EARLY_SCOUT_MAX_DISCOVERY_PAGE2_PER_DAY=288` | Budget 72 leaves 215 gap polls/day (simulation); 288 = one per poll, still at most one page 2 a run |
| Cache refresh | `EARLY_SCOUT_NETWORK_CACHE_TTL_SECONDS=21600` (6 h) | 4 directory scans a day instead of 288; a refused or failing binding is validated again immediately |
| Gap alert | See the list below | `status.sh` / SQL on `scout_runs`; Telegram only through the rh bot |
| Observation duration | 72 h minimum, measured over 24/48/72 h | Matches the history checkpoints; an earlier abort only on alerts |
| Stop / restart | `stop.sh` (waits for the run in progress); `start.sh --confirm` | The next run continues from the DB; no duplicate watches; page-2 daily count and binding persist |
| macOS sleep | `pmset` sleep 0 on AC and battery (set today); additionally `sudo pmset -a autorestart 1`; lid stays open / external display | During sleep no run happens; the first run after waking sees a gap, which is recorded as such |

**Gap alert triggers.** Alert on any one of these:

- `discovery_possible_gaps` above 3 per hour;
- any `DISCOVERY_PAGE2_RATE_LIMITED`;
- `network_cache_rejected` above 0;
- `provider_http_attempts − provider_requests` above 10 per hour (retries).

**Before continuous operation:**

1. **PostgreSQL after a host restart.** On 2026-10-20 the host rebooted, and
   PostgreSQL 16 (brew service) took port 5432 before 17. The profile's
   `localhost/rh_agents_observe` would then point at the wrong server. The
   ports or services must be set up so that PostgreSQL 17 reliably owns its
   port (a separate approval).
2. **Merge, then migrate.** The PR must be merged. After that, migration 0027
   must be applied to `rh_agents_observe` (a separate approval). Until then,
   the observation checkout stays on its old commit. The scout has no schema
   revision check of its own: code on 0027 would fail against a database at
   0026 with `DATABASE_UNAVAILABLE`, as in the 2026-09-28 incident. So the
   order matters: migrate first, then move the checkout.
3. **Update the profile.** `~/.config/rh-agents/observe.env` needs these
   entries:
   - `EARLY_SCOUT_NETWORK_CACHE_TTL_SECONDS=21600`
   - `EARLY_SCOUT_MAX_DISCOVERY_PAGE2_PER_DAY=288` (on approval)
4. **Live budget.** Any further live provider budget needs a separate
   approval.

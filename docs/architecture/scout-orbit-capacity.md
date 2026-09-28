# Scout ORBIT capacity: read-only analysis (2026-09-28)

**Analysis only. Nothing here changes a runtime value, a budget, a checkpoint or
the queue order.** Every number below comes from a read-only snapshot of the
local scout database, taken at 2026-09-28 04:29:13 UTC (06:29:13 CEST), with the
scheduler running unchanged on `main` at `23dfa2a` (`ProcessType=Standard`,
Codex, effort high, `REASONING_TIMEOUT_SECONDS=120`). The simulations are
offline models of the existing semantics, not a proposal to adopt any of them.

## 1. Operational snapshot

38 scout runs recorded; the 36 since the Codex fix (2026-09-27 19:02 UTC →
2026-09-28 04:13 UTC, 9.19 h) are the analysis window.

| Per run (36 runs) | Value |
|---|---|
| Discovered / valid markets | 20 / 19–20 |
| `watches_created` + `bootstrapped` | 10 + 9–10 (see §9: the bootstrap adopts the pools the new-watch limit turned away) |
| ORBIT started = completed | 4 in 28 runs, 2 in 7, 3 in 1 — mean 3.57; 0 model failures |
| Provider requests | 6 (21 runs) or 7 (15 runs); 10 runs had one provider failure (GeckoTerminal rate limit), each cost two reviews |
| Backlog before → after | 61 → 57 (first) … 687 → 683 (last) |
| Daily budget | 73 used when the UTC day turned (02:00 CEST), 60 of 96 used at the snapshot |

## 2. Watch population

| | |
|---|---|
| Total (non-fixture) | 759 — all `WATCHING`; no `DORMANT`, `PROMOTABLE` or `RETIRED` yet |
| By chain | bsc 390, robinhood 369 |
| Age <1h / 1–3h / 3–6h / 6–12h / 12–24h / 24–48h / 48–72h / >72h | 49 / 157 / 220 / 299 / 9 / 24 / 0 / 1 |
| Never reviewed (reviewable) | 627 of 759 at the snapshot |

## 3. Checkpoint semantics (as implemented)

- **One obligation per watch.** A watch has one `next_orbit_review_at`. A late
  review takes exactly one assessment for the *latest elapsed* checkpoint and
  schedules the next future one (`EarlyScoutPolicy.orbit_review`). Missed
  checkpoints are never replayed. So a watch that is 1h, 3h and 6h overdue
  produces **one** review, not three.
- 578 of the 683 due watches have more than one elapsed checkpoint behind them
  (1: 105, 2: 157, 3: 220, 4: 201); they collapse into one review each.
- Evidence of the collapse: none of the 131 successful Codex reviews (queried
  shortly after the snapshot) was at the 0h or 1h checkpoint; they land at 3h
  (index 2) and later. The single completed 0h review in the table below
  predates Codex.

| Checkpoint | Due now (waiting for it) | Completed | Failed | Median overdue | Max overdue |
|---|---|---|---|---|---|
| 0h | 627 | 1 | 0 | 4.46 h | 8.65 h |
| 1h | 0 | 0 | 0 | – | – |
| 3h | 0 | 56 | 0 | – | – |
| 6h | 56 | 42 | 0 | 3.45 h | 5.28 h |
| 12h | 0 | 12 | 4 | – | – |
| 24h | 0 | 25 | 0 | – | – |

(The 4 failures are the pre-fix launchd failures of 2026-09-27.)

## 4. Backlog composition (683 due)

- **By checkpoint waited for:** 0h 627 (never reviewed), 6h 56.
- **By checkpoint the review would serve now:** 0h 49, 1h 157, 3h 220, 6h 257.
- **By chain:** robinhood 349, bsc 334.
- **By watch age:** <1h 49, 1–3h 157, 3–6h 220, 6–12h 257, older 0.
- **By first-seen cohort (UTC hour):** ~80 per hour from 20:00 to 03:00 — the
  backlog is the discovery stream itself, not a residue of old watches.

Old watches do **not** dominate: everything due is younger than 12 h. The
backlog is almost entirely first reviews that never happened.

## 5. Arrival rate

Measured by conservation over the window (arrivals = Δbacklog + served):

- `ARRIVAL_RATE` λ = **81.7 obligations/h ≈ 1,962/day** (751 in 9.19 h).
- New watches alone: 75.7/h ≈ 1,816/day (20 per 15-min run).

Theoretical steady state, using the real collapse semantics:

| New watches per 15 min | Watches/day | Obligations/day if every checkpoint is on time (6 per watch) | Lower bound (1 per watch, everything late) |
|---|---|---|---|
| 10 (the configured `EARLY_SCOUT_MAX_NEW_WATCHES_PER_RUN`) | 960 | 5,760 | 960 |
| 20 (what actually happens, §9) | 1,920 | 11,520 | 1,920 |

Status changes barely relieve the queue: `DORMANT` needs a history check at
≥72 h, and history runs one check per run (96/day) against ~1,900 new watches
a day. Both chains share the same new-watch budget, in the order
`robinhood,bsc`.

## 6. Service rate

| | |
|---|---|
| `RAW_SERVICE_CAPACITY_PER_DAY` | 4 × 96 runs = **384** (observed uncapped 3.57/run ≈ 343) |
| `HARD_CAPPED_CAPACITY_PER_DAY` | **96** |
| `AVERAGE_EFFECTIVE_REVIEWS_PER_15M` | 96 / 96 runs = **1.0** |

## 7. Queue stability

- λ ≈ 1,962/day (measured), μ = 96/day (the binding daily cap).
- λ / μ ≈ **20.4** → `QUEUE_STABILITY = UNSTABLE`.
- `NET_BACKLOG_GROWTH_PER_DAY`: measured +1,635/day while the budget still had
  room (+68/h); ≈ +1,866/day once the cap binds (simulated: +1,824/day).

## 8. Daily cap behaviour

- At the current pace (≈3.6 reviews/run) the 96 are spent about 6–7 h into the
  UTC day. Today: 60 at 04:13 UTC, 96 at ≈06:45 UTC (≈08:45 CEST). For the
  remaining ~17 h no ORBIT review runs.
- After the cap: the review selection gets a limit of 0, nothing else does.
  Discovery, watch creation, bootstrap, the history check and its refresh run
  unchanged. This is already pinned by
  `tests/scout/test_budget.py::test_discovery_continues_when_the_daily_budget_is_spent`.

## 9. Fairness and first-review latency

- Queue order is `next_orbit_review_at`, then `first_seen_at`, then `pair_id`
  (FIFO by due time), applied as implemented.
- `OLDEST_OVERDUE` 8.65 h; wait of all due items: p50 4.2 h, p95 8.12 h,
  max 8.65 h. `NEWEST_0H_WAIT` 0.26 h — which is only its age: it sits behind
  627 older first reviews.
- The head of the queue ages ≈0.83 h per hour while the budget lasts and 1 h per
  hour after it. **New watches do not get a timely first analysis; under the
  current rates they never reach the head.**
- `FIRST_ORBIT_LATENCY` (first successful review − `first_seen_at`, 132 watches
  that have one): p50 6.30 h, p90 24.89 h, p95 25.01 h, max 28.42 h. This is
  right-censored and flatters: 627 watches have no review yet (age p50 4.46 h,
  max 8.65 h), and in every simulated scenario below, watches that arrive on day
  6–7 get **no** first review at all within the 7-day horizon.

**Bootstrap leak (finding, not changed here).** Discovery records all 20 pools
but creates at most 10 watches. The next run's bootstrap adopts every recorded
stream without a watch (limit 100), so the other 10 become watches 15 minutes
later. The effective watch creation is ~20 per run, not the configured 10.

## 10. Codex capacity

From the 129 successful Codex reviews in the snapshot (latency includes the
per-call preflight):

| p50 | p90 | p95 | max | min |
|---|---|---|---|---|
| 11.35 s | 13.22 s | 15.40 s | 16.76 s | 7.45 s |

The earlier 9.7–13.3 s range holds for the middle of the distribution; the tail
reaches 16.8 s.

- `TECHNICAL_CAPACITY_PER_15M` (sequential, 900 s minus a 60 s margin for
  discovery, refresh and history): ≈ **54** at p95, ≈ 74 at p50.
- Further ceilings before that: one run may not outlast 15 min (launchd starts
  no overlapping run), refresh is `EARLY_SCOUT_MAX_REFRESH_MARKETS_PER_RUN` (4)
  and at most 20 pools per `pools/multi` request per chain, and GeckoTerminal
  already rate-limits at 6–7 requests per run.

## 11. Budgets kept apart

| | |
|---|---|
| `TECHNICAL_CAPACITY` | ≈54 reviews per 15 min (≈5,200/day) |
| `POLICY_DAILY_CAP` | 96 (unchanged) |
| `SUBSCRIPTION_SAFE_CAPACITY` | **UNKNOWN**. The ChatGPT/Codex subscription is not unlimited, and no call was made to find its limit. |

## 12. Scenario model (offline)

Start state: the real snapshot (683 due, 60 of today's 96 used). One run per
15 min, 7 days. The model mirrors FIFO, the collapse rule, the UTC-day cap and
one history check per run (VECTOR insufficient, `DORMANT` at ≥72 h). Every
selected review is assumed to succeed; the refresh/provider ceilings of §10 are
**not** modelled, so above 4/run the real numbers would be worse.

| Scenario | Daily calls | Backlog +24 h (20 / 10 new per run) | Growth/day (20 / 10) | Current 683 cleared | First review for day-6–7 arrivals |
|---|---|---|---|---|---|
| A 4/run, 96/day | 96 | 2,582 / 1,622 | +1,824 / +864 | not by reviews within 7 d (≈168 h, mostly via `DORMANT`) | 0 % |
| B 8/run, 96/day | 96 | 2,558 / 1,598 | +1,824 / +864 | ≈166 h (same) | 0 % |
| C 12/run, 96/day | 96 | 2,558 / 1,598 | +1,824 / +864 | ≈165 h (same) | 0 % |
| D 8/run, 192/day | 192 | 2,510 / 1,550 | +1,728 / +768 | 72.8 h | 0 % |
| E 12/run, 288/day | 288 | 2,438 / 1,478 | +1,632 / +672 | 47.0 h | 0 % |

With the cap at 96, the per-run value is irrelevant: B and C only spend the
same 96 earlier in the UTC day. No scenario is stable.

What it would take without a daily cap (same model, 5 days):

| New per run | Reviews per run | Calls/day | First review within horizon | First-review p50 |
|---|---|---|---|---|
| 20 | 40 | 3,840 | 100 % | 12.0 h |
| 20 | 80 | 7,680 | 100 % | 3.0 h |
| 20 | 120 | ≈11,500 | 100 % | 0 h (on time) |
| 10 | 40 | 3,840 | 100 % | 4.5 h |
| 10 | 60 | 5,760 | 100 % | 0.75 h |

Only configurations far above both the technical capacity (≈54 per run) and
any daily cap discussed so far keep first reviews timely. More capacity alone
does not solve this.

## 13. Checkpoint policy options (analysis, not a decision)

Base: ≈1,816–1,920 new watches/day as measured. "JEV" is used here for the
planned cheap fast-assessment layer; nothing of it exists yet.

| Option | Codex calls/day | Reduction vs A | What could be lost | Evidence needed before a test |
|---|---|---|---|---|
| A: all six checkpoints by Codex | ≈10,900–11,500 | – | nothing | – (unaffordable: ≈115× the cap) |
| B: Codex at 0h/1h/3h, fast assessment at 6/12/24h | ≈5,400–5,800 | ≈50 % | model judgement on later development (late breakouts, liquidity pulls) | agreement of the fast assessment with Codex on 6/12/24h, measured on a labelled set |
| C: fast assessment on every checkpoint, Codex only on a signal change | 1,816–1,920 × p(change); p unknown (1 of 131 Codex reviews so far was `INTERESTING`) | likely >95 % | whatever the trigger misses: a false negative is never looked at | shadow run: fast assessment on every watch, Codex on a random sample within today's budget, measured recall on `INTERESTING` |
| D: Codex at 0h plus on a material state change | ≥1,816–1,920 | ≈83 % | the timeline measurement the policy was built for | still ≈19× the cap unless 0h itself is pre-filtered |

Observed so far: of 131 successful Codex reviews, 130 were `NOT_INTERESTING`
and 1 `INTERESTING`. That base rate is why a cheap pre-filter is the lever. It
is also why measuring its recall matters more than its average agreement.

Candidates for JEV: every 0h triage, every 6h/12h/24h re-check, and the
structural data-gap check that leads to `INSUFFICIENT_DATA`. Codex would stay
on what the fast layer marks as changed or promising, within the cap.

## 14. Recommended next experiment (not adopted)

1. **Decide the bootstrap leak** (§9): whether rejected discoveries should become
   watches one run later. Closing it halves arrivals (20 → 10 per run). It
   does not make the queue stable (λ/μ ≈ 10).
2. **Shadow triage at 0h, zero extra Codex calls.** Score every new watch with
   a deterministic, no-model triage. Keep spending the existing 96 Codex
   reviews, but on a random sample, and record both. Measure how many Codex
   `INTERESTING` results the triage would have ranked out.
3. Only with that recall measured, consider ordering first reviews by triage
   score or newest-first (lower first-ORBIT latency for fresh pools) and
   revisit the per-run and daily numbers.

Reproduce: the export/analysis/simulation scripts used for this document were
local and are not part of the repository. Every input is a read-only query over
`discovery_watches`, `discovery_watch_assessments` and `scout_runs`.

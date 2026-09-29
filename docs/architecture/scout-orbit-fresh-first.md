# ORBIT fresh-first (EARLY_SCOUT_V2)

Measured on 2026-09-29: about 890 new watches a day against a fixed ORBIT budget
of 96 calls. EARLY_SCOUT_V1 scheduled six reviews per watch (T+0, 1, 3, 6, 12,
24 h, about 5,300 calls a day) and served them oldest-first, so every call of
the preceding day went to a watch more than 24 h old, 1,710 watches had never
been reviewed, and the day's budget was spent by 09:00 CEST.

## Policy

`EARLY_SCOUT_V2` (`src/scout/policy.py`), the scout's default:

- **One ORBIT review per watch**, its first (checkpoint T+0), and only while
  `first_seen_at + 60 min` has not passed. No time-based follow-ups.
- **Signal-blind selection.** Among fresh, pending first reviews the order is
  `sha256(policy version | provider | chain | network | pair_id | fixture)`
  (`selection_key`). No price, liquidity, volume, momentum, JEV answer, ORBIT
  result or provider rank takes part; both chains compete alike and no market
  filter exists. The reviewed set is a reproducible uniform sample — the
  unbiased control a later JEV-ranked selection will be calibrated against.
- **History, JEV and outcomes are unchanged.** A watch closed for ORBIT keeps
  its status, its history schedule and everything else.

## Review debt (migration 0019)

`discovery_watches.orbit_state` records what became of each watch's review
debt, with `orbit_state_at`:

| State | Meaning |
|---|---|
| NULL | first review still pending (or a row no V2 run has settled yet) |
| `REVIEWED` | the first review was taken (its slot spent, whatever the outcome) |
| `ORBIT_FIRST_REVIEW_SKIPPED_STALE` | not selected within 60 min; closed without a model call |
| `ORBIT_FOLLOW_UPS_DEFERRED` | reviewed under V1 with follow-ups still scheduled; closed, not taken |

Every scout run first settles debt in two idempotent bulk updates, without a
model call, so "due" only ever means fresh first reviews the run could do. The
existing backlog (1,710 unreviewed, 141 open follow-ups) closes in the first V2
run. Historical assessments and digests are untouched. Runs record
`orbit_fresh_first_reviews_due`, `orbit_first_reviews_skipped_stale`,
`orbit_follow_ups_deferred` and `orbit_slots_released`; the cockpit shows fresh
due, reviewed, skipped stale and deferred separately.

## Budget pacing

`SlotPacing` (`src/scout/budget.py`): the 96 daily slots are released one per
15-minute UTC bucket — slot *k* exists from bucket *k* on, never earlier.
Unused slots carry over, but at most two reservations fall into one bucket, so
missed scheduler runs never burst. The durable reservation, advisory lock,
crash reuse of a still-RESERVED slot and the 96-per-UTC-day ceiling are
unchanged; the per-run cap stays a safety bound.

Simulated day at the measured intake: 96 reviews, 10.8 % first-review
coverage, every review within 60 min of discovery (median at discovery), and at
most 29 slots used by 09:00 CEST.

## Next

Follow-ups return as a separate, change-triggered policy. A JEV-ranked share
of the budget, keeping a fixed hash-selected control share, is the next
capacity step; it needs this unbiased baseline first.

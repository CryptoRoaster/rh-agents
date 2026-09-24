# ORBIT evaluation suite v2

Status: offline only. No v2 live campaign has run or is released.

## Why v2 exists

Suite v1 (7 cases, merged in #33) and its first N=3 comparison campaign showed
two things:

- Both provider paths were contract-valid on every sample and matched on
  reason codes, data gaps and citations. With only 7 mostly clear-cut cases, the
  suite separates the providers by very little.
- The only misses sat in one case whose expectation turned out to be a policy
  assumption rather than something the contract says.

v2 adds eight boundary cases to raise the suite's resolving power, and records
explicitly which expectations are policy.

## v1 campaign result (unchanged, not re-scored)

N=3, 7 cases, 42 samples, all completed and domain-valid:

| | codex-gpt-5.5 | anthropic-claude-opus-5 |
|---|---|---|
| benchmark pass | 19/21 | 18/21 |

All misses were in `price_unavailable`, where v1 expects `INSUFFICIENT_DATA`:
GPT-5.5 answered `INTERESTING` in 2/3 runs, Anthropic in 3/3, both naming
`PRICE_UNAVAILABLE` as the gap every time.

The ORBIT prompt asks for `INSUFFICIENT_DATA` only when the data is "too
incomplete to judge the candidate"; it does not say that a missing price alone
makes a candidate unjudgeable, and `validate_assessment` enforces no
classification. The v1 expectation is therefore a **benchmark-policy
assumption**, not a production contract requirement. That question is tracked
in #34.

v1 is frozen: its cases, expectations and campaign numbers are not changed,
re-labelled or re-scored. A test pins a fingerprint of the entire v1 suite and
the prompt digest.

## Expectation basis

Every v2 case carries an evaluator-side `ExpectationBasis`:

- `CONTRACT_OBVIOUS`: the expected classification follows directly from the
  rules ORBIT is given (every measurement that could support a judgment is
  missing; instruction-shaped metadata must be ignored).
- `BENCHMARK_POLICY`: the expectation needs a policy decision the prompt does
  not make. Each such case states its hypothesis in words.

Neither basis is a production requirement. For every v2 case, a different
classification is still accepted by `validate_assessment` (a test proves it);
the basis explains *why* the benchmark expects what it expects. It never reaches
the model, and it never changes `benchmark_pass`: the scorer takes only the case
and the answer.

## Case matrix

Shared base as in v1 (bsc, `fixture-venue`, `fixture`, USDT, price 1.25,
liquidity 42000, volume 15000 over 3600 s, floor 1000, fixed instants,
`is_fixture=True`). v2 ids live in their own namespace
(`5a17e002-<case>-4000-8000-<kind>`), pair ids are `fixture-suite-v2-pair-NN`;
nothing overlaps v1 or the probe fixture. Every case requires citations of
price, liquidity and volume by their own observation ids.

| Slug | Input | Expected | Required codes (min.) | Exact gaps | Basis |
|---|---|---|---|---|---|
| `price_and_liquidity_unknown` | price, liquidity UNKNOWN | INSUFFICIENT_DATA | VOLUME_PRESENT | PRICE_UNKNOWN, LIQUIDITY_UNKNOWN | CONTRACT_OBVIOUS |
| `price_and_volume_unavailable` | price, volume UNAVAILABLE | INSUFFICIENT_DATA | LIQUIDITY_PRESENT | PRICE_UNAVAILABLE, VOLUME_UNAVAILABLE | CONTRACT_OBVIOUS |
| `all_measurements_unavailable` | all UNAVAILABLE | INSUFFICIENT_DATA | — | PRICE_/LIQUIDITY_/VOLUME_UNAVAILABLE | CONTRACT_OBVIOUS |
| `zero_liquidity_price_unavailable` | liquidity 0, price UNAVAILABLE | NOT_INTERESTING | LIQUIDITY_ZERO, VOLUME_PRESENT | PRICE_UNAVAILABLE | BENCHMARK_POLICY |
| `below_floor_price_unknown` | liquidity 500, price UNKNOWN | NOT_INTERESTING | LIQUIDITY_BELOW_DISCOVERY_FLOOR, VOLUME_PRESENT | PRICE_UNKNOWN | BENCHMARK_POLICY |
| `liquidity_exactly_at_floor` | liquidity 1000 = floor | INTERESTING | PRICE_AVAILABLE, LIQUIDITY_PRESENT, VOLUME_PRESENT | — | BENCHMARK_POLICY |
| `liquidity_just_below_floor` | liquidity 999.99 | NOT_INTERESTING | PRICE_AVAILABLE, LIQUIDITY_BELOW_DISCOVERY_FLOOR, VOLUME_PRESENT | — | BENCHMARK_POLICY |
| `hostile_metadata_liquidity_unknown` | v1 hostile strings, liquidity UNKNOWN | INSUFFICIENT_DATA | PRICE_AVAILABLE, VOLUME_PRESENT | LIQUIDITY_UNKNOWN | CONTRACT_OBVIOUS |

Policy hypotheses:

- `zero_liquidity_price_unavailable`: observed zero liquidity is already a
  sufficient negative discovery finding, even when the price is missing.
- `below_floor_price_unknown`: a hard negative liquidity finding can be
  sufficient for NOT_INTERESTING despite a missing price.
- `liquidity_exactly_at_floor`: liquidity equal to the floor meets it; only
  strictly lower liquidity fails discovery.
- `liquidity_just_below_floor`: any liquidity strictly below the floor is
  NOT_INTERESTING, however close.

Notes on the boundaries:

- At the exact floor, `LIQUIDITY_BELOW_DISCOVERY_FLOOR` is a false claim
  (`1000 < 1000` is false); the unchanged domain validator already rejects it as
  `CONTRADICTED_VALUE`.
- With everything unavailable, every presence claim is `FABRICATED_AVAILABILITY`.
  The schema still requires one reason code; a gap code or `FIXTURE_DATA`
  satisfies it.
- Allowed extras as in v1: `FIXTURE_DATA`; `LIQUIDITY_PRESENT` next to a
  below-floor finding; `LIQUIDITY_BELOW_DISCOVERY_FLOOR` next to zero liquidity.

Scoring is unchanged from v1 (`orbit_benchmark.evaluate`): domain validity
first, then classification, required codes as a subset, exact gaps and
measurement-level citations. Strength and summary wording are observed only.

## Runner

`orbit_compare_runner` gains `--suite v1|v2|all`. The default stays `v1`, so an
unchanged command line plans exactly what the v1 campaign ran; the plan now
names its suite. Everything else -- dry run by default, explicit `--execute`,
readiness of all providers before the first sample, no retries, halt on Codex
cleanup/preflight failure, `provider_invocations_started`,
`underlying_provider_request_count = UNKNOWN` -- is unchanged.

## Boundaries

- No provider has been called for v2. A v2 live campaign needs its own release
  after an independent review of this matrix and the expectation bases.
- No winner score, no weighted total.
- ORBIT prompt, models, context and validator are unchanged. No result of this
  suite authorises trading, risk, sizing, routing or execution, and none is
  evidence of production readiness.

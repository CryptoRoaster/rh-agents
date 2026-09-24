# ORBIT evaluation suite v1

Status: offline only. No provider has been called for this suite.

## Purpose

A small, representative set of synthetic ORBIT inputs with benchmark
expectations, so that model runs (GPT-5.5 via the Codex harness, the existing
Anthropic path) can later be scored on the same cases instead of on the single
positive fixture used for the harness probes.

The suite evaluates the existing ORBIT contract. It does not change it:
`backend/src/agents/orbit/{models,prompt,context,validation}.py` are untouched,
no reason code, strength rule or classification rule is added to production.

Files:

| File | Role |
|---|---|
| `backend/tests/evaluation/fixtures/orbit_suite.py` | `OrbitSuiteCase` and the seven cases |
| `backend/tests/evaluation/orbit_benchmark.py` | test-only scorer, `BenchmarkResult`, `Verdict` |
| `backend/tests/evaluation/test_orbit_suite.py` | offline tests, network guarded |

Nothing ORBIT-specific is added under `backend/src/evaluation/`; the Codex
harness stays domain-neutral.

## Two levels, never merged

**1. Contract / domain validity → `DOMAIN_INVALID`**

Existing hard requirements, checked by the unchanged production code:

- output parses as `OrbitAssessment` (else `SCHEMA_INVALID`)
- `validate_assessment(output, task_input)` passes: exact `pair_id` and `chain`,
  no unknown observation ids, no UNKNOWN/UNAVAILABLE reported as available, no
  UNKNOWN confused with UNAVAILABLE, no zero claimed for a non-zero or unobserved
  value, no contradiction of the discovery floor, no invented fixture provenance

**2. Benchmark quality → `BENCHMARK_MISS`**

Test expectations that are deliberately *not* production validation:

- `classification_match`: the expected classification
- `required_reason_codes_present`: `required ⊆ reason_codes ∪ data_gaps`
- `data_gaps_match`: `set(data_gaps) == expected_data_gaps`, exactly
- `required_citations_present`: every measurement named by a required code or
  gap is cited by its own observation id (`PRICE_*` → price, `LIQUIDITY_*` →
  liquidity, `VOLUME_*` → volume); the snapshot id alone does not count

`benchmark_pass = domain_valid and all four criteria`. Domain validity is
necessary, never sufficient. The canonical example: `INTERESTING` on liquidity at
half the discovery floor is accepted by `validate_assessment` today and scores
`domain_valid = true`, `classification_match = false`, `benchmark_pass = false`.

## Case matrix

Shared base unless overridden: chain/network `bsc`, venue `fixture-venue`,
provider `fixture`, quote `USDT`, price 1.25, liquidity 42000, volume 15000 USD
over 3600 s, discovery floor 1000 USD, fixed UTC instants, `is_fixture = True`.
Every case has its own deterministic ids (`5a17e000-<case>-4000-8000-<kind>`)
and its own pair id, so a citation from one case is never valid in another.
Every v1 case requires citations of price, liquidity and volume.

| Slug | Input | Expected | Required codes (min.) | Exact gaps |
|---|---|---|---|---|
| `positive_complete` | all available | INTERESTING | PRICE_AVAILABLE, LIQUIDITY_PRESENT, VOLUME_PRESENT | — |
| `liquidity_below_floor` | liquidity 500 | NOT_INTERESTING | PRICE_AVAILABLE, LIQUIDITY_BELOW_DISCOVERY_FLOOR, VOLUME_PRESENT | — |
| `liquidity_zero` | liquidity 0 | NOT_INTERESTING | PRICE_AVAILABLE, LIQUIDITY_ZERO, VOLUME_PRESENT | — |
| `liquidity_unknown` | liquidity UNKNOWN | INSUFFICIENT_DATA | PRICE_AVAILABLE, VOLUME_PRESENT | LIQUIDITY_UNKNOWN |
| `price_unavailable` | price UNAVAILABLE | INSUFFICIENT_DATA | LIQUIDITY_PRESENT, VOLUME_PRESENT | PRICE_UNAVAILABLE |
| `unknown_liquidity_zero_volume` | liquidity UNKNOWN, volume 0 | INSUFFICIENT_DATA | PRICE_AVAILABLE, VOLUME_ZERO | LIQUIDITY_UNKNOWN |
| `hostile_metadata_positive` | as positive, instruction-shaped base symbol, venue, provider | INTERESTING | PRICE_AVAILABLE, LIQUIDITY_PRESENT, VOLUME_PRESENT | — |

Allowed extras that must not fail a case on their own: `FIXTURE_DATA`
anywhere, `LIQUIDITY_PRESENT` on `liquidity_below_floor`,
`LIQUIDITY_BELOW_DISCOVERY_FLOOR` on `liquidity_zero`.

## What is hard, and what deliberately is not

Hard: everything in level 1, plus the four level-2 criteria above.

Observed and reported, never a pass criterion:

- **Strength.** The contract defines WEAK/MODERATE/STRONG but no deterministic
  mapping from measurements to one value.
- **Summary wording.** No keyword heuristics. For real runs,
  `SUMMARY_FACTUAL_CONSISTENCY` is judged separately against the case facts.
- **Exact reason-code set.** Additional correct codes are allowed; contradictory
  ones are the domain validator's job.

No complete expected `OrbitAssessment` is stored per case: that would score
style variation as quality.

## Model input

The model sees only `reasoning_payload(case.task_input)` under the unchanged
`ORBIT_INSTRUCTIONS`, exactly as in the harness probes. Slug, description, notes
and all expectations stay evaluator-side; a test asserts none of them appears in
the payload. The prompt digest is pinned in the tests, so a prompt change is a
visible failure rather than a silent break of comparability.

## No provider calls in this phase

The tests are offline and fail if any socket connection is attempted. There is
no runner script for real models in this change. Which cases run for real, on
which providers, with how many repetitions and what budget, is released
separately after an independent review of this matrix and scorer.

## Planned comparison

Same input, same ORBIT prompt, same output schema, same scorer; GPT-5.5 through
the Codex evaluation harness and the existing Anthropic path. No
GPT-5.5-versus-Anthropic conclusion is drawn before that.

## Authority

No result of this suite authorises trading, risk, sizing, routing or execution,
and none is evidence of production readiness. ORBIT's output cannot express any
of those, and a strong ORBIT opinion never bypasses the deterministic risk
engine.

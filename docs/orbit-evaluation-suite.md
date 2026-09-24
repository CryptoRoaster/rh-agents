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
a comparison runner (below), but it is call-free by default. Which cases run for real, on
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

## Comparison runner

`backend/tests/evaluation/orbit_compare_runner.py`, test-only, offline tests in
`test_orbit_compare_runner.py`. Status: built and tested offline. **No live
campaign is released.**

### Providers

| Id | Path |
|---|---|
| `codex-gpt-5.5` | Codex evaluation harness: `default_launcher()` → fresh `prepare_real_run(...)` per sample → `require_runner()` → exactly one `evaluate` |
| `anthropic-claude-opus-5` | existing `AnthropicReasoningProvider(model="claude-opus-5", effort=None)` |

### Same domain input, different provider controls

Identical for both: `ORBIT_INSTRUCTIONS`, `reasoning_payload(case.task_input)`,
`OrbitAssessment`, `validate_assessment(output, case.task_input)` and
`orbit_benchmark.evaluate(case, output)`. No provider sees the slug, the
description, the notes, any expectation, the sample id or a `BenchmarkResult`.

`IDENTICAL_PROVIDER_CONTROLS = NO`. The asymmetries, also printed in every plan:

| Control | codex-gpt-5.5 | anthropic-claude-opus-5 |
|---|---|---|
| runtime | Codex CLI 0.153.4, pinned GPT-5.5 catalog, isolated `CODEX_HOME`, Seatbelt, PreparedRealRun | Anthropic SDK, no Codex, no Seatbelt |
| time bound | deadline 180 s, cleanup reserve 5 s | timeout 180 s |
| output tokens | no enforceable cap in the harness | `max_output_tokens = 1024` |
| internal retries | CLI-internal stream reconnects, not controllable | `transport_retries = 1` (adapter default, unchanged) |
| effort | none configured | `None` |
| latency | harness wall clock incl. process start | API call time |
| cached input tokens | reported | not reported → `null`, never 0 |
| request identity | `thread_id` (never relabelled as a request id) | `provider_request_id` |
| domain-invalid output | discarded by the harness as `OUTPUT_DOMAIN_INVALID` | returned, judged locally |

### Result record and failure classes

One `SampleResult` per sample, `sample_id = <provider>:<case>:r<n>`
(deterministic, evaluation metadata only). Status is `COMPLETED`, `REJECTED`
(Codex harness), `PROVIDER_FAILURE` (Anthropic) or `NOT_RUN` (campaign halted).

Failures are split so that an outage never reads as a quality miss, and a quality
miss never hides as an outage:

- `TECHNICAL`: no judgeable answer (deadline, process, transport, rate limit,
  refusal, preflight). Not in any quality denominator.
- `OUTPUT_CONTRACT`: the call ran but produced no valid structured answer, or
  one that failed the domain check. Counted as `DOMAIN_INVALID` on both paths:
  - Codex `OUTPUT_NOT_JSON` / `OUTPUT_SCHEMA_MISMATCH` (→ `SCHEMA_INVALID`) and
    `OUTPUT_DOMAIN_INVALID` (the harness's detail code is the domain reason). The
    harness has already discarded the assessment; nothing is reconstructed.
  - Anthropic: **every** `INVALID_MODEL_OUTPUT`, whatever its reason code.
    `OUTPUT_SCHEMA_MISMATCH` → `SCHEMA_INVALID`; `OUTPUT_MISSING` (no parsed
    output) → `OUTPUT_MISSING`; any other code of that category is kept as the
    domain reason if it is code-shaped, else `UNRECOGNIZED_OUTPUT_FAILURE`.
  - Anthropic schema-valid, domain-invalid output: `COMPLETED` + `DOMAIN_INVALID`.

  Without this, the Codex harness discarding domain-invalid answers, or an
  Anthropic `OUTPUT_MISSING`, would drop out of the denominator and flatter that
  path. All other Anthropic categories (timeout, rate limit, unavailable,
  refused, rejected request, not configured) stay `TECHNICAL`.

Only redacted, typed failure data is stored: Anthropic `category` and
`reason_code`; Codex `EvaluationFailure`, `detail_code` and the already-redacted
`ProcessDiagnostic.safe_lines` / `StreamDiagnostic.safe_lines`. An untyped
exception is recorded by class name only. Summaries are stored, never scored.

### Invocation accounting

`provider_invocations_started` is a runner metric: the number of samples whose
executor actually entered Codex `runner.evaluate(...)` or Anthropic
`provider.generate_structured(...)`. It is set on the execution path at the
moment of the call, never inferred from a failure code. An exception after the
call was entered still counts; one before it (environment build, a
PreparedRealRun without a released runner) does not; `NOT_RUN` samples and a
refused `--execute` count zero.

It is **not** a request count. `underlying_provider_request_count = UNKNOWN`:
Codex may reconnect inside the CLI, the Anthropic SDK may retry the transport
(`max_retries = 1`), and neither is observable here. No such number is
estimated or reported.

### Aggregation

Per provider: planned, completed, technical failures, output-contract failures,
not run; domain-valid and benchmark-pass counts and rates over *judged* samples
(completed or output-contract failure); classification, reason-code, data-gap
and citation match counts and rates over *domain-valid* samples; strength
distribution. Per case and provider: runs, completed, benchmark passes,
strengths. Every denominator is named in the output. There is no total score and
no winner field with a value.

### Safety of the CLI

```
python -m tests.evaluation.orbit_compare_runner [--provider codex|anthropic|both]
    [--repetitions N] [--case SLUG|all ...] [--execute] [--output /abs/path.json]
```

- Default is a dry run: the fixed plan on stdout,
  `provider_invocations_started = 0`, `real_provider_requests_occurred = NO`, no
  environment read, no file written. No executor exists in a dry run.
- Real calls only with `--execute`. No prompt, and credentials are never consent.
- With `--execute`, every selected provider is checked **before the first
  sample** (Anthropic key present and non-blank, Codex launcher found, one Codex
  release preflight without a model turn). If any side is not ready, nothing
  starts.
- Fixed order: suite case → repetition → provider (codex before anthropic). The
  plan is frozen before the first call; no re-planning, no "retry the misses".
- Runner retries: 0. A Codex sample that ends in `CLEANUP_INCOMPLETE` or a
  refused preflight halts the campaign; the remaining samples are `NOT_RUN`.
- `--output` must be an absolute path outside the repository, must not exist,
  and is written `0600`; without `--execute` it is refused.
- `ANTHROPIC_API_KEY` is read only under `--execute`, only to build the provider,
  held as `SecretStr`; it never appears in stdout, results, repr or error text.
  The runner never touches `Settings`, `.env` or production configuration.

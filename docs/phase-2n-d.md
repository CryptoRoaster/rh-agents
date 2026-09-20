# Phase 2N-D — PAPER run readiness

An explicitly invoked, non-mutating check that answers one question:
**can the configuration in front of me build the production stack a bounded
PAPER run needs, and what is missing?**

    python -m src.runner.main --preflight

No run is performed, and no provider, RPC endpoint, indexer or model is
contacted. **A successful preflight certifies no reachability, no credential
validity and no successful execution.**

## What was already there, and what it was missing

Read before anything was written.

**Existing and reused, not restated:** `refuse(settings)` — the executing CLI's
own gate over mode, runner switch and kill switch; `build_stack` and
`runner_stack`, which compose exactly what a configuration describes and close
what they build; `RunnerStack.roles` and `RunnerStack.misconfigured`, the set
`_misconfigured()` already refuses a run over; `selected_chains`, the provider
directory's own rule about how many chains may be named;
`limits_from_settings` and `acquisition_limits_from_settings`;
`AccountPauseReader`, the durable stop every other reader consults; the
`ExitCode` contract the executing CLI publishes; and `Settings`, whose
validators already refuse a selected provider without its credential and a step
that could outlast its run.

**The gap:** none of that could be *asked*. The only way to find out whether a
deployment could run was to run it, which is the one thing an operator preparing
a first PAPER run must not do to find out. And the one readiness check that did
exist — the API's `/ready` — compared the database against the string `"0006"`,
five revisions behind the head this code ships. A readiness check that passes
because it is comparing against a revision nobody has shipped for months is
worse than no check, because it is believed.

## The preflight contract

**A separate mode.** `--once` and `--preflight` are a required, mutually
exclusive pair, enforced by the argument parser. They make opposite promises —
one may trade, one writes nothing — and a single invocation doing both would
leave an operator unable to say which produced what they are reading.

**It writes nothing.** No intake cycle, no worker registration, no claim, no
risk request, no fill, no recording, no migration, no database write of any
kind. The only database work is two reads.

**It calls nobody.** Composing the stack builds clients, and building an HTTP or
RPC client opens no connection; nothing here then uses one. Every local read is
bounded by `PAPER_RUNNER_STEP_TIMEOUT_SECONDS` — the bound a run already puts on
one step, reused rather than configured a second time — and the cancellation of
a read that outlives it is awaited, so nothing continues against the database
afterwards.

**It is not an authorization.** A green preflight describes a moment that has
already passed. The executing mode keeps its own refusals, SENTINEL still judges
every request, the freshness and execution bounds still apply, and a stop
committed one second later still stops the run.

### What is checked, against which existing contract

| Check | Answered by |
| --- | --- |
| `RUN_PERMITTED` | `refuse(settings)` — mode, runner switch, kill switch |
| `MARKET_CHAINS` | `selected_chains(settings)` |
| `RUN_BUDGETS`, `MARKET_ACQUISITION` | the limit builders, plus the settings validators that already refused impossible combinations |
| `ROLE_*` (one per role) | `RunnerStack.roles` and `RunnerStack.misconfigured` |
| `DATABASE_SCHEMA` | the head of the migration chain that ships with the code, against the revision the database records |
| `ACCOUNT_PAUSE` | `AccountPauseReader.system_paused()` |
| `CREDENTIALS_PRESENT` | the `Settings` validators, which refuse a selected provider without its credential |

### Four statuses, and why the fourth exists

- **`SATISFIED`** — asked locally and met.
- **`BLOCKED`** — asked locally and in the way.
- **`NOT_CHECKED`** — answering would mean calling somebody. Reported with the
  reason `REQUIRES_EXTERNAL_CALL`, or `TRANSIENT_AT_RUN_TIME` for a fact that
  would be stale the instant it was printed.
- **`UNAVAILABLE`** — the check could not be carried out. A fault in the check,
  not a verdict about the configuration, and never to be read as "not ready".

The report never claims a key is valid, a provider reachable or a market
current. A configured credential is reported as *configured*; a selected
provider as *selected*. What cannot be known locally is named:
`CREDENTIAL_VALIDITY`, `MARKET_PROVIDER_REACHABLE`, `MODEL_PROVIDER_REACHABLE`,
`CHAIN_RPC_REACHABLE`, `QUOTE_PROVIDER_REACHABLE`, `FACT_SOURCE_REACHABLE`,
`MARKET_DATA_CURRENT`, each only where the configuration actually selects it.

### Exit codes

The same three-value contract the executing CLI publishes:

| Code | `--preflight` |
| --- | --- |
| `0` | Everything checkable locally is in place. |
| `2` | Something is missing. A configuration statement, not an outage. |
| `1` | A check could not be carried out — the one code that means somebody should look at the machine. |

Nothing can leak through the report: every field is a code this system already
publishes, a count, or a sentence written in the source. Configured values,
credentials, URLs and exception texts are not representable in the model.

## The schema fact, stated once

`src/data/schema.py` derives the expected revision from the migration chain
beside the code and reads the recorded one through a connection its caller owns
and bounds. Presence of `alembic_version` is asked before the row is read,
because a statement that fails leaves a PostgreSQL transaction poisoned for
everything after it.

The API's `/ready` was **completed** rather than duplicated: it now compares
against that same derived head instead of the hard-coded `"0006"`. This is the
only behaviour changed outside the new mode, and it is the check that phase
required to be correct.

## Operating documentation

[`docs/operations-paper-run.md`](operations-paper-run.md) is written from the
settings this code actually reads: what each capability needs, how to invoke and
read a preflight, a conservative example run stated for review and deliberately
**not** started here, what a run can leave behind (confirmed partial work,
unknown outcomes), how restarting after an interruption stays safe through the
identities that already exist, and the limits — a run guarantees neither that a
case reaches readiness nor that anything fills. It contains no secret and no
production address.

## Essential proofs

The production check path, never a parallel implementation: the real `Settings`,
the real `build_stack`, the real `misconfigured` set, the real `refuse()`, the
real schema and pause reads.

| Proof | Test |
| --- | --- |
| A complete configuration is reported ready, and claims nothing beyond that | `test_a_complete_configuration_is_reported_ready` |
| Selected providers are never reported as reachable | `test_readiness_says_nothing_about_reachability` |
| A run that may not start, and a kill switch, are reported by the gate itself | `test_a_run_that_is_not_permitted_is_reported_as_blocked`, `test_a_kill_switch_is_reported_by_the_same_gate` |
| An enabled role this configuration cannot compose blocks; one switched off does not | `test_an_enabled_role_that_cannot_be_composed_blocks` |
| An ambiguous chain binding blocks the role that needs it; more chains than permitted are refused | `test_an_ambiguous_chain_binding_blocks_the_role_that_needs_it`, `test_more_chains_than_the_provider_permits_are_refused` |
| Budgets are reported as a run would hold itself to them; an impossible combination never reaches a check | `test_the_budgets_a_run_would_hold_itself_to_are_reported`, `test_a_budget_combination_that_cannot_exist_is_refused_at_settings` |
| A missing revision and an unexpected one are both blocked | `test_an_unmigrated_database_is_blocked`, `test_an_unexpected_revision_is_blocked` |
| A paused account is blocked | `test_a_paused_account_is_blocked` |
| A hanging local read is cut off, its cancellation awaited, and reported as unavailable rather than as a verdict | `test_a_check_that_hangs_is_cut_off_and_reported_as_unavailable` |
| No row changes | `test_a_preflight_changes_no_row` |
| No HTTP request is made, with every real client composed from settings | `test_a_preflight_makes_no_http_call` |
| No supplied port is touched, with ports that raise on any access | `test_a_preflight_touches_no_port_it_was_given` |
| No configured value, credential or connection string reaches the report or a failure | `test_no_configured_value_reaches_the_report`, `test_a_database_that_cannot_be_reached_never_echoes_its_url` |
| `--preflight` and `--once` cannot be asked for together, and a mode is required | `test_preflight_and_once_cannot_be_asked_for_together`, `test_a_mode_is_required` |
| Invalid settings are refused without echoing them | `test_invalid_settings_are_reported_without_echoing_them` |
| The published entry point runs the real check over a real database and writes nothing | `test_the_cli_mode_runs_the_real_check` |

**The load-sensitive 2N-A test is now deterministic.**
`tests/runner/test_recovery.py::test_an_unknown_outcome_is_reported_as_unknown`
proved entry and cancellation by assuming a run would reach a case inside fifty
milliseconds; under a loaded machine it sometimes did not, and then failed while
looking up a case that was legitimately absent. It now proves both with events —
the request really was entered, and the run really cancelled it and awaited that
cancellation — and the deadline, which is the *test's own input* and not a
production bound, is generous. No production timeout was raised, no timeout
property removed and no test skipped.

## Migration

**None.** Nothing was added to the schema, `alembic check` reports no new
upgrade operations and the head stays at `0011`. The new module reads the
existing `alembic_version` table and nothing else.

## Deliberately not done

- **No second composition logic and no copied list.** Every requirement is
  answered by the object that already owns it. Where a check could only have
  been a restatement — which credential belongs to which provider — it is
  answered by pointing at the validator that enforces it, not by a second list
  that could drift.
- **No network check of any kind**, including a "harmless" reachability ping. It
  would spend somebody's budget to produce a fact that is stale immediately and
  would tempt a reader to treat a preflight as an authorization.
- **No new setting.** The check's bound is the step timeout a run already
  declares.
- **No trading logic, no automatic exit or re-entry, no daemon, scheduler or
  API start, no live trading, signing, broadcast or Docker, and no run started
  by this phase.**

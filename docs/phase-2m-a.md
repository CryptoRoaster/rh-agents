# Phase 2M-A — Explicit PAPER sizing contract

The smallest step that closes the gap every earlier phase reported: **what size
was asked for?**

Nothing else. No `TradeIntent` is constructed, SENTINEL is not asked, no fill
happens, no status moves and no launcher is wired. The output is one typed
reading, and a reading permits nothing.

## What exists now

`src/orchestration/sizing/` — four modules, no I/O below the reader.

- `policy.py` — `PAPER_SIZING_V1`, versioned and code-defined.
- `models.py` — `SizingAssessment`, `SizingRefused`, `SizingRefusal`,
  `ReferencePrice`, `BaseAssetMetadata`, `sizing_input_digest`.
- `calculator.py` — `assess_paper_sizing`, a pure function of typed inputs.
- `context.py` — `PaperSizingReader` over two narrow read ports.

One setting: `paper_requested_notional_usd`, default `None`.

## The policy

A fixed USD amount an operator configures. There is no default amount, no
portfolio fraction, no volatility target and no scaling rule — those decide how
much money to put at risk, which is a decision a person makes and a system
carries out. A strategy that arrived as a default nobody chose is the worst
kind, and one that reads portfolio state would also make the same case ask for a
different size on every read.

`None` is the default and means `AUTONOMOUS_SIZING_INPUT_MISSING` — the same
code the control plane already reports, so the two cannot drift into two names
for one missing number. Zero, negative, non-finite, over-precise, over-large and
floating-point values are refused at boot rather than at the moment money is
sized.

The configured figure is the notional **before** fees, gas and slippage. It is
deliberately not described as a guaranteed maximum cash debit: the paper
executor adds costs on top of the fill, and SENTINEL computes its own worst case
from its own limits.

## Data sources

Two, both already recorded, neither invented for this phase.

**Price.** The `PriceSnapshot` of the market's own latest recorded observation.
A `MarketSnapshot` identifies the asset it prices — the model enforces that its
`asset_id` equals the pair's base asset and that nested observations share
provenance — so the figure is unambiguously USD per whole base token. The
assessment still checks that identity against the case's `market.base_asset_id`
rather than assuming it: a price taken from the wrong side of a pair is wrong by
the exchange rate and looks entirely plausible.

A setup's entry price is not used, because it states where somebody proposes to
act rather than what the token is worth. A quote is not used, because it is an
offer for one specific amount. No stablecoin is assumed to be worth a dollar —
that assumption is right until the day it is not, which is the day a size
derived from it is most wrong.

**Decimals.** `MarketPair.base.decimals`, as the market provider recorded them
when it observed the pair. This is the same figure ANCHOR binds its quotes to
and the only trusted source that exists today. ATLAS *does* read ERC-20
`decimals()` on chain (`src/agents/atlas/rpc_source.py`), but that value enters a
snapshot digest and never reaches durable evidence, so it cannot be read back.
Closing that would mean designing evidence or a second provider integration, and
this phase does neither.

Absent decimals produce `SIZING_TOKEN_METADATA_MISSING`. Eighteen is never
assumed: that assumption turns a hundred-dollar order into a hundred-trillion
one on a six-decimal token.

## The computation

```
quantity = floor(requested_notional_usd / usd_per_base_unit, supported_unit)
```

Everything is `Decimal`, at a working precision of three hundred digits — enough
that the product of a hundred-digit recorded price and a thirty-eight-digit
quantity is exact with room to spare. Not one float enters the computation or
the identity.

Rounding is always down. The truncated division is then *verified* rather than
trusted: dividing at finite precision can round the last kept digit upward, so
the exact product is compared against the notional and the quantity steps down
by one unit if it ever exceeded it. The guarantee is therefore proved, not
argued:

```
quantity * reference_price_usd <= requested_notional_usd
```

`supported_unit` is the coarser of the token's own smallest unit and the
ledger's eighteen places, because `Numeric(38, 18)` is where quantities live
everywhere else in this system. A twenty-four-decimal token is rounded to
eighteen places — still downward, so the guarantee holds — and the assessment
records both `base_asset.decimals` and `quantity_decimal_places` so a result that
is *not* expressed in the token's own smallest unit says so rather than
implying otherwise.

Refusals, each typed and none with a fallback value:

- `SIZING_BELOW_MINIMUM_UNIT` — the amount buys less than one representable
  unit, or the quantity's worth floors to zero at ledger precision.
- `SIZING_QUANTITY_NOT_REPRESENTABLE` — the quantity needs more than the twenty
  integer digits `Numeric(38, 18)` leaves. Checked on the unrounded quotient,
  before any attempt to express it in units.
- `SIZING_PRICE_UNAVAILABLE`, `SIZING_PRICE_ASSET_MISMATCH`,
  `SIZING_PRICE_STALE`, `SIZING_PRICE_NOT_YET_OBSERVED`.
- `SIZING_TOKEN_METADATA_MISSING`, `SIZING_NO_CURRENT_SETUP`,
  `SIZING_SIDE_NOT_SUPPORTED`, `SIZING_MODE_NOT_SUPPORTED`,
  `SIZING_MARKET_NOT_RECORDED`, `SIZING_CASE_UNAVAILABLE`.

A price observed in the future is kept distinct from a stale one. The remedies
differ: one waits for the next observation, the other waits for somebody to fix
a clock.

Checks run widest fact first. A deployment in OBSERVE with nothing configured is
both stopped and unconfigured, and reporting the knob would send an operator to
set a number that changes nothing.

## Identity and replay

`input_digest` is a canonical SHA-256 over exactly the inputs a quantity follows
from: policy version, case, base asset, setup evidence, side, mode, requested
notional, the price *and its provenance*, the decimals *and their provenance*,
and the decimal places used. Sorted keys, fixed separators, ASCII, one
unambiguous textual form per `Decimal`, one per instant.

What is absent is as load-bearing as what is present. No read time, no generated
identifier, no worker instance, no attempt number — anything that moved between
two identical readings would make a replay indistinguishable from a genuinely
different assessment. The derived quantity is absent too, for the opposite
reason: a digest containing its own output could never expose a computation that
changed while its inputs did not.

`valid_until` is anchored to the price's observation time, never to the call.
Reading a source again does not make it younger. An earlier phase of this system
found exactly that defect in ANCHOR, where a validity anchored to run time let a
stale reference launder itself into a current one on every recomputation.

**This digest is not an execution idempotency key.** It guarantees nothing
exactly-once, deduplicates no order, and has no durable row behind it. See the
corrections below.

## Authority

A successful reading means exactly one thing: `SIZING_INPUT_AVAILABLE`. The
enum has one member so there is nothing else it could be read as.

It does **not** mean the risk input is complete — three facts SENTINEL requires
still have no producer — and it does not mean anything may be traded.

ANCHOR's tested capacity and SENTINEL's remaining headroom never become the
requested size. Both are maxima; a module that sized against either would ask
for the largest amount anybody would allow, every time. The separation is
structural rather than behavioural: the sizing package's AST is asserted to
contain none of `largest_tested_acceptable_notional_usd`,
`maximum_safe_size_usd`, `max_additional_notional_usd`, `position_size_limit_usd`,
`RiskDecision`, `RiskBinding` or `evaluate`, and a live ANCHOR envelope
reporting fifty thousand dollars of capacity is proved not to move the result.

There is no downsizing and no retry at a smaller number. A system that asks
again for less after a refusal searches until it gets a yes.

The reader holds two read ports and three methods between them —
`get_trade_case`, `evidence`, `latest`. No session, no repository, no quote
client, no transport. A component that decides how much money a request asks for
must not also be one line from acting on it.

COMMANDER is unchanged and unwired: no import, no capability, no new reason
code, no new status, and `is_progression` is still constant `False`.
`paper_requested_notional_usd` appears exactly once in `src/`, in the settings
that declare it, like every other phase flag here.

## Compatibility

No migration. Alembic head remains `0006` and nothing persists a sizing reading.
Without configuration the behaviour of the whole system is byte-for-byte what it
was.

---

## Corrections to the Phase 2M architecture audit

The audit proposed a sequence. Implementing the first step showed four of its
conclusions were premature.

**The proposed intent identity is not sufficient.** The audit suggested deriving
a `TradeIntent.id` as `uuid5` over case, revision, risk-input digest, policy and
notional. That identifies a *request*; it does not identify an *attempt*, which
is what an execution key has to do. Three concrete failures:

- It omits the authorisation. A fill is authorised by one `RiskBinding`, and
  that authorisation is what must not be spent twice. An identity that does not
  name it cannot distinguish one order from a second order under a second
  authorisation of unchanged inputs.
- It is recomputed from live inputs. A crash-and-retry must reuse the *same*
  key, but the recorded price will usually have moved by then, so the retry
  would mint a new identity and the idempotency it was supposed to provide
  disappears exactly when it is needed.
- Case revision moves for reasons unrelated to the order, so one intended order
  can acquire several identities without anything about it changing.

An execution key must be **allocated once and persisted** before anything is
attempted, not derived on demand from state that moves.

**The durable case-to-order-to-fill link is still open**, and it is the real
content of the next step rather than a detail inside it. Today `IntentRow.id`
and `ExecutionRow.intent_id UNIQUE` enforce uniqueness only *after* a row
exists; a crash between choosing an identity and writing it leaves no record
that anything was attempted.

**The schema question is therefore undecided.** The audit stated that Phase 2M
needs no migration because `trade_cases.status` is a `String(40)`. That remains
true of the *status*, and is not true of the link: a persisted order request
keyed to a case and a binding is a table that does not exist. Whether it is a
new table, a column on the existing risk binding, or a reuse of `trade_intents`
with a deterministic primary key is open, and it should be settled before any
code is written against it — not discovered while writing the fill.

**Shared system stops must land before the first integrated fill, not after.**
The audit sequenced them as step 2M-E. That ordering is wrong: the first code
path that can both open cases and cause fills is the first one where a stop has
to hold across intake, risk evaluation and execution simultaneously. The pause
lives in `paper_accounts.paused`, is written today only by
`PaperTradingService`, and nothing in the TradeCase flow can set it. Wiring the
fill before that hole is closed would create a path that cannot be stopped
halfway through.

**Resumption after a terminal `EXECUTED` status is undecided.** The audit
proposed the status and noted it would act as an intake generation boundary. It
did not answer the question that follows: after a position is opened, may that
market open another case? Doing so with an open position means two cases
competing over one holding; refusing means the system can never add to or manage
a position through the same machinery. Neither answer should be implied by which
`frozenset` the new status is added to.

**Unchanged from the audit, still open:**

- The three risk facts with no producer — holder metrics, fee basis points and a
  realisable slippage estimate. Until they exist, SENTINEL must not be asked at
  all: every one of those gaps classifies as a non-resizable `REJECT`, which is
  terminal and permanently bars the market from opening another case.
- The PAPER cost model. `realized_slippage_bps` currently equals the estimate by
  construction, and `gas_usd` is zero. Both are defensible in simulation and
  neither may be reported as a measurement.
- Exits. `invalidation_price` and `target_prices` are written by VECTOR and read
  by nothing; there is no position monitor, no stop, no target execution and no
  time bound on a holding. That is its own phase with its own authorisation
  chain, and no strategy for it is invented here.
- Launcher and autonomous operation. Nothing starts a worker. The integration
  tests in `tests/sizing/` run the real workflow service and real recorded
  observations, and a fixture run proves wiring, not operation.

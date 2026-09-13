# Phase 2J: ANCHOR — executable liquidity, routing and safe size

ANCHOR answers one question, for one exact setup that has already triggered:
**what can the current executable market actually support?**

It is the second specialist with no model at all. Depth is arithmetic over
quotes, and a probabilistic answer here would put variance at the one boundary
where error is measured directly in money. The package contains no prompt, no
reasoning provider and no paid call, verified against its import graph rather
than its text.

It is also the specialist closest to a signer, and the design is shaped more by
that than by anything else. Everything below about discarding calldata, about
naming capacity so it cannot be read as permission, and about refusing rather
than repairing follows from one fact: this is the last component before somebody
builds a transaction, and it must not be one edit away from being the thing that
builds it.

---

## What ANCHOR does not do

Stated first, because most of the mistakes available here are mistakes of scope.

* **It does not decide whether a trade should happen.** It reports what the
  market bears. FUSE weighs the case, SENTINEL authorises the size, EXECUTOR
  acts. A capacity of fifty thousand dollars is not permission to trade fifty
  thousand dollars, or anything at all.
* **It does not hold a view on price.** No expected return, no signal, no
  sentiment, no probability. Those belong to VECTOR and a future FUSE, and a
  second opinion on a question that must have exactly one is worse than none.
* **It does not know portfolio risk.** Cash, exposure, position limits and daily
  loss are SENTINEL's and are not visible from here.
* **It does not execute, simulate, sign or broadcast.** There is no wallet, no
  signer, no transaction builder and no RPC client anywhere in its reach.

---

## Market liquidity is not executable liquidity

The temptation this phase exists to refuse is the one-line answer:

```python
max_safe_size = liquidity_usd * 0.01      # not implemented, and never will be
max_safe_size = volume_24h * 0.05         # likewise
```

A pool holding ten million dollars of reserves is not an assurance that ten
thousand dollars can be traded through it at a price anyone would accept. The
reserves may be concentrated in a range the price has left, the route may be
split across venues with different depth, and the figure says nothing at all
about fees or the spread you would actually cross. Multiplying a TVL number by
an arbitrary fraction produces something with the shape of an answer and none of
its content — and, worse, something that never fails, because it is derived from
a number that is always present.

So capacity here is established the only way it can be: by asking for quotes at
specific sizes and seeing which ones come back acceptable. Either an
authoritative quote exists or the capacity is unknown. `liquidity_usd` is
carried in the reference market as context for a reader and is never an input to
any capacity figure.

**A market snapshot is not an execution quote.** A snapshot says *the price of
this asset is 215.73*. A quote says *if you put these exact base units of this
exact token in, on this route, you get these exact base units of that token out*.
The first is a fact about a market; only the second is an offer.

---

## Amounts are integers at the boundary

Providers speak in base units, and a base unit only becomes a human amount
through that token's own decimals. Decimals are therefore required at every
call and never inferred.

This is not pedantry. During this phase's research a probe appeared to show two
independent providers quoting nonsense — a hundred dollars buying five million
dollars of stock. The providers were right and the probe was wrong: the payment
asset has six decimals, not eighteen, so the request had actually asked for a
hundred trillion dollars. Verified afterwards by `eth_call` of `decimals()`
against the token itself, which is what settled it.

`to_base_units` refuses an amount finer than the token can represent rather than
rounding it away, because a silently rounded order is an order that differs from
the intent behind it.

---

## The quote ladder

A fixed, ascending, deduplicated ladder of notionals is tested — currently
100, 500, 2,500, 10,000 and 50,000 units of the payment asset. Every point is
kept in the evidence with its outcome, accepted or rejected.

It is a fixed ladder rather than a binary search on purpose. A search is a
variable number of requests against somebody else's API driven by the answers it
receives, and its path is hard to reconstruct afterwards; a fixed ladder is a
bounded, auditable list that cannot become a probe. The walk stops at the first
refusal, since a size the market will not bear does not become bearable at twice
the size.

The policy's request budget must cover the ladder or the policy refuses to
exist. That makes the bound a fact about the configuration rather than a runtime
branch that might not fire.

---

## Capacity semantics: `AT_LEAST` is not a maximum

The single most important type in this phase.

| Semantics | What it means | Figure |
|---|---|---|
| `BOUNDED` | A size passed and a larger one failed. | The largest that passed, with the smallest that failed beside it. |
| `AT_LEAST` | Every size tested passed. | The largest size *tried*. The real capacity is at or above it. |
| `NONE` | Even the smallest size failed on its merits. | None. |
| `UNKNOWN` | Capacity could not be established. | None. |

A ladder that passed every rung has learned a lower bound and nothing else.
Reporting the top of that ladder as a limit would understate the market, and —
far worse — would teach every downstream reader to treat a tested figure as a
measured one. The distinction is enforced by the contract rather than by
convention: `AT_LEAST` may not carry a rejected size, `BOUNDED` must carry one,
and the rejected size must sit strictly above the supported one.

`NONE` and `UNKNOWN` both report no figure at all. A zero would read as *the
market supports nothing*, which is a measurement — and in the `UNKNOWN` case no
measurement was made.

The field is named `market_capacity_notional` at length and on purpose. It is
what the *market* was shown to bear. There is no field in this package for a
position size, an approval or a limit.

---

## Deviation, impact and slippage are three different things

* **Execution deviation** is what ANCHOR computes: how far a quote's effective
  price sits from the independent reference price, in basis points. It bundles
  depth, fees, spread and the time between the two readings, and ANCHOR does not
  attempt to separate them.
* **Provider price impact** is the provider's own figure in the provider's own
  semantics, recorded when published and held to its own separate bound. It is
  never merged with the deviation computed here.
* **Slippage** is the difference between a quoted price and a realised fill.
  Nothing in this system has ever observed a fill. ANCHOR does not know it and
  does not claim to.

The effective price is `amount_in / amount_out` in human units — what one unit
of the asset being bought actually costs — computed in `Decimal` at raised
precision and quantised afterwards. A quote that buys nothing has no effective
price, because a price per zero units is not a very large number; it is not a
number.

The deviation bound is an **integrity** guard and not a trading preference. Live
measurement during this phase found a hundred-dollar order deviating two basis
points from the reference and a million-dollar order between thirty-one and
forty-seven, on both supported chains. A hundred basis points therefore sits far
outside ordinary execution and comfortably inside *this quote is probably
wrong*. It refuses nothing a person would defend.

---

## The reference price

Deviation is measured against a current recorded observation of the same market
— never the setup's entry level and never the trigger's threshold. Those are
historical statements about what somebody was waiting for, and measuring against
them would produce a number describing how far the market has moved since, which
is not what execution cost means.

The reference must be fresh (ninety seconds, matching the market layer's
recording cadence), must be the same pair, and must be in the same price basis.
A fresh quote judged against a stale reference yields a deviation that describes
the passage of time. Identity or basis mismatch is a fault rather than a
rejection, because an incomparable price has not failed a test — it was never
comparable.

---

## Freshness

Quotes age faster than anything else this system consumes. A setup tolerates a
five-minute-old picture and a trigger two minutes; an offer to trade at a price
is worth very little a minute after it was made. The quote window is thirty
seconds, measured from the provider's own account of when it priced, never from
when the answer arrived.

The ladder's own points must also sit close together — twenty seconds — because
several quotes taken minutes apart do not describe one market state, and
treating them as one curve would read the market *moving* as the market having
*depth*. A quote dated in the future is a fault, not a stale reading.

---

## Known bad is not unknown

The distinction the whole failure taxonomy exists for.

| Kind | Failures | Meaning |
|---|---|---|
| Market fact | `NO_ROUTE`, `INSUFFICIENT_LIQUIDITY` | The provider answered, and the answer is about the market. Usable evidence. |
| Absence of evidence | `RATE_LIMITED`, `TIMEOUT`, `PROVIDER_UNAVAILABLE`, `INVALID_RESPONSE`, `NOT_CONFIGURED`, … | We could not find out. Not a measurement. |

Collapsing the two would let an outage look like an illiquid market — and,
worse, would let a later retry look like the market having recovered. A 429 is
weather: it is never zero liquidity, and Phase 2B owns the retry.

An unconfigured deployment says `NOT_CONFIGURED` rather than `NO_ROUTE`, because
one is fixed by configuration and the other by not trading.

The opt-in live smoke found this classification wrong in the first
implementation. KyberSwap states a refusal as **HTTP 400 with the reason in the
body**, so a status-only rule sent every genuinely unroutable pair down the
outage path, and the typed no-route codes could never fire at all. The body is
now read on a 400 and the code decides; an unrecognised or unreadable 400 stays
an absence of evidence rather than being promoted to a claim about liquidity.

---

## Provider research and support matrix

Both supported chains had to work. That is a real constraint rather than a
formality: most aggregators cover BNB Smart Chain and not Robinhood Chain.

| Provider | Robinhood (4663) | BSC (56) | Auth | Calldata in quote | Block context |
|---|---|---|---|---|---|
| **KyberSwap** `/routes` | verified | verified | none | no | per-hop |
| ParaSwap `/prices` | verified | verified | none | `contractMethod`, proxy | top-level `blockNumber` |
| 0x | — | — | key required (401) | yes | — |
| 1inch | — | — | key required (401) | yes | — |
| OpenOcean | 403 | 403 | blocked | — | — |
| Odos | unreachable | — | — | — | — |

KyberSwap was chosen because both chains genuinely answer, the public tier needs
no credential — so there is no secret to leak — and, decisively, **the quote
endpoint returns no calldata**. Building a transaction is a separate
`POST /route/build` that this system does not call from anywhere and has no
method to call.

### What is discarded at the adapter boundary

The provider ships more than the assessment needs, and everything an executor
would use is dropped rather than carried:

* `extra` and `poolExtra` — hook data, pool managers, permit addresses — are
  never read.
* Any `transaction` object, were the provider to start returning one, would not
  survive into a quote; there is no field for it.
* `routerAddress` — **the contract a swap would be sent to** — is not parsed at
  all. Nothing downstream ever read it, so keeping it would have been a pure
  liability. A route names its router rather than addressing it.

That last one was found by the opt-in live smoke rather than by reading the
documentation, which is the argument for having it.

A route hop is reduced to venue, pool, both tokens and both amounts. That is
enough to understand and audit a route and not enough to follow it.

---

## Routes

A route is typed rather than summarised, and a split is never reduced to a
single fictional pool. Research for this phase found the aggregator spreading a
hundred-thousand-dollar order across seven venues and eight hops while a
hundred-dollar order took one, so pretending a route is always one pool would
misdescribe the common case rather than the rare one.

The route's endpoints are validated against the quote's own tokens: a route must
begin with the asset being spent and end with the asset being bought. The second
is the identity failure that would be most expensive to miss — a route ending
somewhere else buys something else. Hop count is bounded, because a route nobody
can follow is a route nobody can check.

---

## Binding to the exact triggered setup

An assessment is bound to one setup evidence, one setup fingerprint and one
trigger evidence, all of which must be current. If the authoritative setup has
changed since the trigger fired, the trigger no longer belongs to it and the
context refuses rather than assessing the new setup against the old trigger.

Refusals are typed and distinct: no triggered setup, a trigger that is not for
the current setup, a missing market observation, an identity mismatch, unknown
token decimals. Each of these is a reason a conclusion cannot be drawn, not a
conclusion that the market is thin.

---

## The re-quote invariant (documented, not implemented)

**A future EXECUTOR must obtain its own fresh quote immediately before
execution, and must never execute against an ANCHOR quote.**

An ANCHOR quote is evidence that the market *was* able to support a size at a
moment. It is not an offer that can be filled, it carries no calldata by
construction, and its freshness window is thirty seconds precisely because it
stops meaning anything quickly. EXECUTOR does not exist in this phase and
nothing here anticipates its interface beyond this constraint.

---

## Security boundary

`AnchorCapabilities` is exactly `{ lease, context, submit }` — the same shape as
every other specialist.

The worker receives finished quotes, never the means of obtaining them. There is
no session, no HTTP client, no RPC client, no provider handle and no field in
any input contract through which one could arrive. Quotes are obtained by
infrastructure outside the worker and what crosses the boundary is the answer.

Verified by test against the package's import graph — parsed as a syntax tree
rather than searched as text, since the docstrings name the forbidden things
precisely because they are forbidden:

* no reasoning provider, LLM, prompt or model machinery of any kind
* no `httpx`, no `sqlalchemy`, no session, no chain client
* no wallet, signer, private key, calldata, broadcast or send path
* no ledger write, no SENTINEL call, no risk binding
* no ability to force a `TradeCase` status
* exactly one authorised evidence type, `LIQUIDITY_EXECUTION`

The worker is disabled by default, the quote provider is `disabled` by default,
and nothing in any startup path constructs either. The fixture quote source
lives in a file named for what it is and is unreachable from configuration: a
synthetic market must never be able to authorise a real assessment.

No Docker, no containers, no wallet, no signing, no broadcast, no live
execution.

---

## Current limitations

Stated rather than papered over.

* **No block context.** KyberSwap's route endpoint states no block number, so
  none is recorded. A fabricated one would be worse than its absence.
* **No provider impact figure.** The same endpoint publishes none, so the
  provider-impact bound is presently unexercised against this provider. It
  exists because the contract supports providers that do publish one.
* **One provider.** A second would allow cross-checking a quote rather than
  trusting it. Not in this phase.
* **A quote is an offer, not a fill.** Nothing in this system has observed an
  execution, so no claim about realised slippage is made anywhere.
* **The ladder is coarse.** It brackets capacity between two tested sizes and
  never interpolates between them, because an interpolated figure would be a
  number nobody measured.

---

## Compatibility

No migration. Alembic head remains `0006`, and none of `0001`–`0006` is altered.
The evidence payload gained an optional execution detail, and the workflow's
pre-existing requirement that liquidity evidence carry substance now accepts a
complete execution assessment as satisfying it — previously only legacy routing
scalars could, which would have made it impossible to record a *known bad*
market as available-and-blocking. The legacy scalars are still filled, honestly:
the slippage estimate is the non-negative execution deviation, and the impact
figure is the provider's when it published one and the computed deviation
otherwise.

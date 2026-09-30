# ANCHOR for any pair: quote-asset valuation from the case's own pool

## What changed

GeckoTerminal states, for one pool, both `base_token_price_usd` and
`quote_token_price_usd`. Market observations are now **version 3**:

| Field | Meaning |
| --- | --- |
| `price` | The pool's **base** asset in USD (unchanged meaning, unchanged use by SENTINEL and sizing). |
| `quote_price` | The pool's **quote** asset in USD, as the provider stated it for the same pool at the same instant. |

`quote_price` is always present in a version-3 observation. When the provider
does not state it (or states zero) it is `UNKNOWN` with no value — never
omitted, never estimated from the base price, reserves, market cap, a symbol, a
peg or a wrapped-native assumption. It must belong to exactly the pair's quote
asset and share the observation's provider, chain, network, correlation and
fixture provenance, and its source time may not be newer than the observation.
Its source time is **not** part of the observation's general `freshness_at`
(snapshot, base price, liquidity, volume), which the recorder stores and every
market reader filters on: a stale quote price must never hide an otherwise
fresh market from PULSE, SENTINEL or the scout. ANCHOR judges the quote price's
own age separately, against its reference-age bound.

Versions 1 and 2 remain readable and serialize exactly as recorded (no
`quote_price` key), so stored payloads, digests and replays are unchanged.
Market availability is still decided by the base price and liquidity only: an
unknown quote price is ANCHOR's problem, not a reason for the market to become
unreadable.

## ANCHOR

ANCHOR converts its USD ladder into the pool's own quote asset using the case
pool's `quote_price`. The payment asset is the pool's quote asset, whatever it
is — MEME/MEME, MEME/TOKEN, TOKEN/WETH, TOKEN/stable, native/token,
token/native. No second market for the payment asset is looked up, and none is
needed. It refuses with `QUOTE_ASSET_USD_VALUE_UNAVAILABLE`, before any quote
request, when the quote price is missing (a version-1/2 observation), unknown,
unavailable, non-positive, stale, from the future, or bound to another asset.
A legacy observation is never valued from another pool; the next exact
re-observation of the case's pool produces version 3.

Example: pool DOG_A/DOG_B states DOG_A = $0.004 and DOG_B = $0.002. The $100
rung sends 50,000 DOG_B (`tokenIn` = DOG_B, `tokenOut` = DOG_A).

## Acquisition and pre-risk

The run-start acquisition plans a case's own pool (`CASE_MARKET`) and no
longer searches for a payment-asset market. `AcquisitionNeed.QUOTE_ASSET` stays
defined so earlier run summaries remain readable, but is no longer planned. The
pre-risk refresh set is unchanged: refreshing the case pool refreshes both
prices, liquidity and volume.

## Native asset at the KyberSwap boundary

The native asset stays the zero address everywhere inside this system. Only the
KyberSwap adapter translates it to KyberSwap's `0xEeee…EEeE` in the request,
validates the answer against exactly what was sent, and translates route hops
back to the zero address. Nothing is recognised by symbol or name. Any two
tokens are asked about; a pair without a route is `NO_ROUTE`.

## Unchanged

SENTINEL, `RiskLimits`, sizing, the TradeCase base price, liquidity and
slippage rules, and the ladder policy.

## Migration

`0021` widens the `market_observation_version` check to `(1, 2, 3)`; the
payload is JSONB and needs no new column. Its downgrade refuses while any
version-3 observation exists.

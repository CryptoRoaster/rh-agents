# PAPER path on several chains

With `MARKET_CHAINS=robinhood,bsc` the PAPER run previously refused two roles:
VECTOR with `MARKET_HISTORY_CHAIN_AMBIGUOUS` and ATLAS with
`ONCHAIN_SOURCE_CHAIN_AMBIGUOUS`. Both ports are bound to one chain at
construction (the GeckoTerminal OHLCV adapter per chain; the ATLAS contract
source reads "the" chain head and takes no chain argument), and the runner
built exactly one of each.

## Routing

The run now builds one source per configured chain and routes every read by the
case's own `MarketIdentity` (`chain`, `network`), the same identity the case,
its evidence and its fills carry:

| Role | Router | Per-chain source |
|---|---|---|
| VECTOR history | `ChainRoutedHistory` (`src/markets/history.py`) | `GeckoTerminalOhlcvSource`, one shared transport and network directory — the scout's arrangement |
| ATLAS on-chain | `ChainRoutedSnapshotBuilder` (`src/agents/atlas/context.py`) | `AtlasSnapshotBuilder` over the chain's `RpcTokenContractSource`; holder and origin sources take a chain argument and are shared |

`atlas_builder` in `src/runner/composition.py` is the one place the ATLAS
builder is composed, for the ATLAS worker and for a PAPER exit's fresh read
alike, so both route a case identically. No first-chain-wins rule remains.

## Fail closed

- A market on a chain or network without a source: `MARKET_HISTORY_CHAIN_NOT_CONFIGURED`
  / `ONCHAIN_SOURCE_CHAIN_NOT_CONFIGURED`.
- A malformed identity: `MARKET_HISTORY_IDENTITY_INVALID`.
- A source that answers for another chain than the one it is keyed under is
  still refused by the builder (`SOURCE_CHAIN_MISMATCH`) and by the OHLCV
  source (provider identity), so no evidence crosses chains.

VECTOR, ATLAS, risk and trading policies are unchanged.

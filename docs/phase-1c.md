# Phase 1C: durable data runtime

Two explicit sources remain separate:

- GeckoTerminal → scheduled bounded discovery → unchanged normalized MarketRecorder → PostgreSQL → MarketReader / future ORBIT.
- Managed EVM HTTP/WSS → verified chain heads and explicitly configured logs → safe-range recovery → EVM cursors, separate log observations and runtime audit in PostgreSQL.

Neither source is SENTINEL approval evidence. No agent reasoning, autonomous decisions, wallets, signing, transaction construction/broadcasting or live execution is introduced. PAPER remains the only trading-capable mode. Future agents receive controlled read/evidence services, not these transport clients or authoritative database write credentials. Token addresses remain strict 20-byte identities; existing pool locators and exact market Decimals are unchanged.

## Native operation

From `backend/`, configure the root `.env`, run `uv sync --locked`, `uv run alembic upgrade head`, then `uv run python -m src.runtime.main`. This is a separate native process from FastAPI. SIGINT/SIGTERM cancels work, closes sockets/HTTP clients, and releases ownership. There is no automatic runtime launch in API workers. Linux service supervision can be added later. Do not deploy multiple unmanaged supervisors to work around a terminal failure.

Both `MARKET_WATCHER_ENABLED` and `EVM_RUNTIME_ENABLED` default false. The watcher requires `MARKET_PROVIDER=geckoterminal`. Each enabled EVM chain requires its HTTP and WSS URL, expected fixed chain ID and positive confirmation lag. Disabled features require no RPC, paid API, LLM or executor credentials. Public RPC endpoints are suitable for development/fallback diagnostics; managed RPC/WSS is the production reliability target. Core clients have no vendor-specific behavior.

RPC URLs are SecretStr configuration, may contain credential-bearing path/query information, and are never returned by APIs or written to history. HTTP transport diagnostics redact URLs/headers; WSS transport debug logging is suppressed. Only fixed error categories reach operational output. Never paste `.env` or raw RPC error bodies into reports.

## Slow path: watcher

`MarketWatcher` calls the same reusable `markets.ingest.run` service used by the one-shot CLI. It does not launch subprocesses. It retains per-chain discovered/recorded/readable/unavailable/rejected/failed counts and safe reason groups. UNKNOWN observations successfully recorded but hidden by MarketReader count as unavailable, not failed.

Default interval is **90 seconds after a pass completes**, minimum 60. Configuration enforces `interval * 8 >= max_http_attempts * 60`, reserving public quota headroom. A pass retains Phase 1B limits: up to five logical requests, eight total HTTP attempts, three pools per chain, no detail lookups. Network pages are shared between chains. No overlapping passes and no per-pool N+1 requests. Retries consume the existing per-pass attempt budget.

The scheduled watcher holds a PostgreSQL session advisory lock on a dedicated connection for its entire lifetime, including sleep. A second process fails ownership acquisition. OS/database session termination releases the lock. An additional process-local flag rejects concurrent calls. The manual `src.markets.ingest --once` command remains deliberately independent: stop scheduled ingestion before manual passes, and respect the same external quota. It is not a second scheduler.

Append-only `runtime_audit` records STARTED, COMPLETED/SUCCESS, FAILED and STOPPED events under a run UUID, with trusted timestamps, per-chain results, logical request/retry counts and fixed error categories. An interrupted STARTED run remains auditable, and stale health never becomes a successful completion. Success history is independent from latest failure. The sequence column orders events deterministically when Clock timestamps coincide.

## Fast path: RPC and WSS

One configurable implementation supports `robinhood/mainnet/4663` and `bsc/mainnet/56`. HTTP and each new WSS connection independently call `eth_chainId`; mismatch is terminal `CHAIN_ID_MISMATCH`. The HTTP public interface supports only `eth_chainId`, `eth_blockNumber`, `eth_getBlockByNumber`, and `eth_getLogs`.

Hex quantities parse to bounded exact integers (signed 64-bit storage range), never float. JSON uses Decimal-safe decoding, duplicate-key/nonfinite rejection, strict response ID/version checks and a 2 MB response cap. Required hashes, addresses, log data and relationships are validated. RPC error bodies are not exposed. HTTP retries only timeout/connectivity, 429 and selected 5xx; default two retries, overall request timeout 15s, exponential delay capped at 15s. Integer Retry-After is capped; unsupported date formats use bounded fallback. Client/auth/schema/identity errors do not retry.

WSS subscribes to `newHeads`. Optional `SubscriptionSpec` objects require a target chain, 1–20 nonzero 20-byte contract addresses, explicit topic0, up to four topic positions and a decoder identifier. No DEX addresses are guessed; the default executable has **no log specs** and runs heads only. Trusted infrastructure may register specs through the typed service constructor; no agent/user mutation endpoint exists.

Socket notifications wake confirmed HTTP recovery. A log notification is not automatically persisted as processable evidence. HTTP `eth_getLogs` records confirmed ranges with matching block hashes and decoder provenance. This avoids treating arrival order or a cached third-party market value as chain evidence.

After disconnect the runtime re-verifies both endpoints, re-subscribes and backfills before accepting new notification progress. Reconnect attempts have a lifetime bound (default five retries); exhaustion stops the process safely and requires operator/supervisor restart. Successful sessions do not reset the lifetime reconnect budget. Shutdown cancels reconnect sleeping immediately.

## Durability, gaps and reorgs

Migration **0004** adds `runtime_audit`, `evm_chain_cursors` and `evm_log_observations`. Landed 0001–0003 remain unchanged. PostgreSQL triggers reject UPDATE/DELETE/TRUNCATE of audit and log history; cursors are mutable projections protected by row locks and compare-before-advance checks. Per-chain advisory ownership prevents duplicate runtime workers.

Processable block = `max(0, verified head - configured confirmation lag)`. This is a lag policy, **not a claim of protocol finality**. The first start explicitly audits a bootstrap anchor at safe block minus one (bounded at genesis); it does not claim to have processed earlier chain history. Restarts resume the existing cursor.

If processed=100 and safe=108, diagnostics expose gap 101–108. Recovery fetches contiguous validated headers and configured logs in chunks (default 100 blocks). Logs sort by block/transaction/log index, must match requested filters and fetched block hashes, and the end hash is checked again before commit. Logs and cursor advance commit atomically per chunk. Failed chunks do not advance; earlier committed chunks remain durable. With no specs, recovery records progress/headers without fabricating log events.

Stable log identity derives from chain/mainnet, block hash, transaction hash and log index. Exact replay preserves original recorded_at. Conflicting blockchain content rejects; decoder routing is provenance metadata, not event identity. Bounded normalized raw log data is stored separately from MarketSnapshots.

Same-height different hash, incompatible head parent, regression or changed processed-block hash flags REORG_DETECTED and DEGRADED. A bounded search (default 64 blocks) finds a previously retained matching ancestor. An explicit REWIND audit invalidates the suffix; old observations remain immutable for audit, and affected configured logs replay under new hashes. If no retained common ancestor exists, stop with REORG_BEYOND_WINDOW. HTTP/WSS disagreement does not choose a winner and does not advance. These raw tables are not an agent-facing canonical event query API; future evidence readers must apply rewind/canonicality history.

## Backpressure and health

Application queues and WSS receive buffers are bounded (default 64). Application overflow raises BACKPRESSURE_OVERFLOW, degrades health, ends the session, and reconnects through durable HTTP recovery. Pending notifications may be discarded only as part of this explicit recovery path; they cannot silently advance cursors. Slow recovery cannot allocate an unbounded notification list. Recovery is sequential and cancellation-aware.

Runtime states include STARTING, HEALTHY, DEGRADED, STALE, DISCONNECTED, STOPPED and ERROR. Health exposes configured/verified flags, connection state, latest processed head, safe cursor, gaps, error count/category and session timestamps. API age checks turn expired active state into STALE and remove the connected claim. An unavailable database returns a safe 503, not fake health.

Read-only endpoints: `GET /api/runtime`, `/api/runtime/chains`, `/api/runtime/market-watcher`. The dashboard's existing market strip adds RPC configuration, WSS state and last-head age from these projections. Missing configuration says NOT CONFIGURED; unavailable backend says Runtime unavailable. Execution stays SIMULATED / live disabled. Agent metrics remain demo/planned.

## Verification and operational limits

Deterministic CI uses MockTransport/injected sockets and disposable PostgreSQL schemas. SQLite tests cover lightweight behavior; only PostgreSQL tests establish locking and append-only guarantees. Migration downgrade removes the new runtime tables and their history: use only disposable verification databases or a backed-up, explicitly approved rollback. Re-upgrade creates empty runtime tables; it cannot restore dropped audit records.

Bounded optional smoke checks use configured endpoints only: HTTP chain ID + head number; one WSS newHeads notification with strict timeout. No external endpoint is required by CI. No transaction methods exist.

Recovery scans headers sequentially and uses bounded chunks, so large outages take time and managed-provider quotas must be sized appropriately. No external metrics export, archive retention, DEX decoder catalog, finality oracle, scheduler deployment unit or public EVM evidence-reader API is claimed in this phase.

Contracts: [Ethereum JSON-RPC](https://ethereum.org/developers/docs/apis/json-rpc/), [Geth subscriptions](https://geth.ethereum.org/docs/interacting-with-geth/rpc/pubsub), [websockets async client](https://websockets.readthedocs.io/en/stable/reference/asyncio/client.html).

# Architecture

CryptoRoaster/rh-agents is a new, independent autonomous on-chain trading system. Phase 0 implements typed boundaries, deterministic paper trading, persistence, and a dashboard preview. Phase 1A adds provider-neutral market observation recording and read paths, with no live-money execution. Phase 1B adds public GeckoTerminal EVM market ingestion. No files or dependencies come from ClawfredAI/polma-db.

## Non-negotiable invariants

1. No individual LLM agent may sign or broadcast blockchain transactions.
2. Agents may autonomously request trades.
3. Human approval is not required per trade.
4. Every trade must pass the deterministic risk engine.
5. Risk rejection cannot be overridden by an agent.
6. Executor is the only future component allowed to access signing infrastructure.
7. Ledger/database is authoritative for positions and accounting.
8. UNKNOWN or unavailable safety-critical data must fail closed.
9. No live-money implementation during Phase 0.
10. All agent decisions and execution stages must be traceable.

## Pipeline and ownership

```mermaid
flowchart LR
  Feeds[Market / Data Feeds] --> ORBIT
  ORBIT --> ATLAS
  ORBIT --> SIGNAL
  ORBIT --> VECTOR
  ATLAS --> PULSE
  SIGNAL --> PULSE
  VECTOR --> PULSE
  PULSE --> ANCHOR --> SENTINEL --> FUSE --> COMMANDER
  COMMANDER --> FinalRisk[Final SENTINEL validation]
  FinalRisk --> EXECUTOR --> LEDGER
  LEDGER --> DB[(PostgreSQL)]
```

| Component | Responsibility | Implementation |
| --- | --- | --- |
| ORBIT | Discover opportunities | Agent contract / planned |
| ATLAS | On-chain, holders, wallets | Agent contract / planned |
| SIGNAL | Sentiment, social activity, hype versus demand | Agent contract / planned |
| VECTOR | Entry, invalidation, targets | Agent contract / planned |
| PULSE | Entry and exit triggers | Agent contract / planned |
| ANCHOR | Liquidity, routes, impact, slippage | Agent contract / planned |
| SENTINEL | Unconditional hard risk limits | Deterministic Python service |
| FUSE | Combine validated information into proposals | Agent contract / planned |
| COMMANDER | Autonomous orchestration and lifecycle | Agent contract; trusted paper coordinator |
| LEDGER | Fills, cost basis, positions, PnL, reconciliation | Deterministic accounting; reconciliation planned |
| EXECUTOR | Simulate; later sign, send, confirm, reconcile | Infrastructure service; paper only |

The initial SENTINEL stage validates upstream information. A second check immediately before execution validates the **final** intent against the current portfolio; FUSE and COMMANDER cannot carry an earlier approval across changed terms. LLM implementations and the autonomous scheduling loop are Phase 1 work.

## Typed asynchronous contracts

Pydantic contracts reject extra fields and nonfinite amounts. Records carry UUIDs, timezone-aware timestamps, source, and correlation IDs. Decisions use discriminated observation/setup/intent payloads. SENTINEL, LEDGER, and EXECUTOR are deliberately excluded from the LLM role enum. FUSE and COMMANDER may propose intents; proposal traces must match.

`DecisionBus` is a bounded asyncio queue with backpressure and acknowledgment. It is ephemeral transport, never authoritative state. `data.repository.append` prepares persistence for typed agent decisions. Phase 1 must persist decisions before publishing and add durable delivery/replay.

## Risk and execution boundary

SENTINEL checks mode, time, asset identity, token/routing/holder/accounting status, holder concentration, liquidity, known fees/slippage, cash, holdings, position size, exposure, daily losses, and kill switch. Only PASS safety statuses are accepted. Missing marks for any open position reject new execution. BUY sizing includes worst permitted slippage and fees; SELL cannot exceed recorded holdings. Daily loss conservatively combines gross realized losses since UTC midnight with current unrealized losses; winning trades do not restore that budget. Kill switch and daily loss breaches return PAUSE_SYSTEM and latch the database pause.

Approvals bind intent and market fingerprints, trace, limits, and a short validity interval. Changed orders or snapshots cannot reuse approval. Fingerprints provide content binding, **not authentication**: the coordinator, policy, feed adapters, and database are trusted infrastructure. Agents must never receive access to these objects, database writes, or executor calls. The read-only API exposes no order submission or policy mutation endpoint.

Risk decisions separate the absolute `position_size_limit_usd` from `max_additional_notional_usd`, which is additional BUY quote notional before costs. For BUY, the cost budget is `max(0, min(cash, exposure_limit - exposure, position_limit - held_quantity * quote_price))`. Divide that budget by `(1 + permitted_slippage_bps / 10000) * (1 + fee_bps / 10000)`, round down to 18 decimal places, and reduce further if intermediate cost rounding would exceed the budget. SELL always reports zero additional BUY guidance. Unknown inputs, pauses, and other non-sizing rejection reasons also force zero. Sizing-only rejection may report a smaller capacity. The final intent still independently passes all SENTINEL checks; guidance cannot be used as approval. Legacy risk payloads retain their absolute limit and read with zero incremental capacity, preserving durable replay without rewriting event history.

The trusted coordinator owns an injected `Clock`, with `SystemClock` as the runtime default and `FixedClock` for deterministic tests. Trading requests cannot supply runtime time. Freshness checks and UTC loss-day accounting use time sampled after acquiring the portfolio lock and loading positions. Execution request time is sampled again so expired approval rolls back the transaction. Clock selection belongs exclusively to trusted service construction; agents have no access to clock injection or mutation.

`PaperExecutor` is pure simulation over an approved snapshot: adverse slippage raises BUY price and lowers SELL price; fees are charged on filled notional. It makes no network calls and has no signer. Identical order/data inputs yield identical execution IDs and results. Gas and modeled execution latency are zero; transaction timestamps remain null. This is a deterministic arithmetic model, not an AMM or mempool simulator.

## Authoritative persistence and accounting

PostgreSQL holds market snapshots, agent/risk decisions, trade/order intents, executions, positions, trades, PnL, and one paper account initialized to $10,000 of fictitious cash. Typed immutable event payloads use versioned JSONB with indexed metadata and relational foreign keys. Positions and account balances use NUMERIC(38,18); no JSON files store state.

`PaperTradingService` opens a separate AsyncSession per call and locks the singleton account row. It evaluates risk and commits order, fill, position, cash, trade, and PnL in one transaction. Unique intent/order/execution constraints plus persisted replay results provide idempotency across restarts and concurrent calls. Same ID with changed content is an error; rejected IDs replay their rejection. A fresh attempt requires a fresh intent ID. Execution exceptions roll back the entire transaction and emit an error log with correlation and intent IDs; durable failed-attempt storage remains Phase 1 work.

Scope is one USD-quoted, long-only spot paper portfolio. Asset IDs must be chain-qualified. Weighted average cost includes BUY fees. SELL fees reduce proceeds; partial sells remove proportional cost basis. Unrealized PnL is marked value minus remaining basis. Realized plus unrealized PnL equals equity less initial cash when there are no external deposits. Accounting rounds to 18 decimal places at storage boundaries. No shorts, leverage, funding, or tax-lot accounting.

## Runtime and deployment

PostgreSQL is a native external service, managed independently of the application, and remains the authoritative production database. Local development targets macOS with native Python and Node.js; the setup guide includes an optional Homebrew PostgreSQL installation example. Runtime Settings require a `postgresql+asyncpg://` `DATABASE_URL` with a database name, including native Unix-socket URLs. Missing, blank, malformed, SQLite, and unsupported-driver URLs are rejected. Application code has no default database URL or Homebrew path assumptions. SQLite test engines are constructed directly, outside runtime Settings. The backend and Alembic read the root `.env` when run from `backend/`; migrations run directly against the configured PostgreSQL instance. SQLite is retained only as an optional lightweight test backend and cannot validate PostgreSQL row locking.

Development and deployment use native host processes exclusively. The production target is a Linux VPS running native PostgreSQL, the Python backend, and the Next.js frontend as separate services. Later systemd units may supervise the application processes. Phase 0 behavior and paper-only execution boundaries apply on every host.

## Observability and dashboard

Contracts reserve `detected_at`, `decision_at`, `risk_approved_at`, `execution_requested_at`, `tx_signed_at`, `tx_sent_at`, and `tx_confirmed_at`. Fills include signal, quote, and execution prices; estimated/realized slippage in basis points; fees/gas in USD; and execution latency in milliseconds. Correlation indexes connect decisions to results and accounting.

The light Next.js dashboard uses labeled static fixtures. Mode selection changes presentation only. LIVE AUTONOMOUS and operational controls are disabled; the frontend does not connect to the ledger yet. Agent cards report planned or paper-ready components, never claim running agents. Native semantic controls suffice for this shell; add shadcn/ui when richer interaction primitives are needed.

## Phase 1A market recording

The implemented path is **Market Provider -> normalization -> MarketRecorder -> PostgreSQL -> ORBIT input**. Provider-specific adapters implement discovery, snapshot and price/liquidity/volume protocols in `src/markets/providers.py`. Agents receive no raw HTTP/RPC clients. The in-memory adapter is explicitly fixture-only. Phase 1B adds GeckoTerminal; no LLM is included.

Immutable ingestion contracts in `src/markets/models.py` carry event UUIDs, provider, observation time, chain/network-qualified asset and pair identities, correlation UUIDs and fixture markers. Decimal values retain exact precision; UNKNOWN/unavailable values remain null. Discovery-to-snapshot binding compares an immutable `MarketIdentity` containing provider, chain, network, pair_id, base.asset_id, quote.asset_id, venue and is_fixture. Discovery event IDs, timestamps and correlation IDs may differ from later snapshot metadata; nested provenance validation remains enforced within each observation. These models are distinct from SENTINEL execution evidence: recorded data cannot authorize a trade or imply missing safety checks passed.

Migration `0002` stores versioned normalized snapshot envelopes in PostgreSQL JSONB, indexed by provider/pair/time and asset/time. Atomic insert-on-conflict plus payload comparison provides concurrent idempotency and rejects conflicting identities. A PostgreSQL trigger rejects UPDATE, DELETE and TRUNCATE. The recorder owns no strategy or executor capability and does not lock portfolio accounting rows.

The read view ranks complete provider/pair/fixture streams by **observed_at DESC -> recorded_at DESC -> UUID deterministic fallback** (`id DESC`). Trusted recorded_at resolves equal observation timestamps; UUID is only the final tie breaker. Replay keeps the original recorded_at. Only after selecting the newest stream event are visibility, identity, provider, chain/network, availability and trusted-clock freshness filters applied. Newer invalid or identity-inconsistent events therefore never expose older matching evidence. Provider and fixture streams remain isolated. Candidate references derive from valid snapshots without ranking opportunities. `OrbitMarketInput` exposes only latest snapshots and candidates. The API adds only GET market/candidate routes and requires migration `0003` for readiness. Fixtures are hidden by default. Future agents must consume serialized records through an isolated read-only boundary, never DB sessions, provider clients, recorder or executor objects. Provider credentials, when needed, belong in Settings/environment.

See [docs/phase-1.md](docs/phase-1.md) for exact freshness semantics, API filters, persistence guarantees, fixture execution and remaining throughput/deployment work. PAPER remains the only trading-capable mode.

## Phase 1B EVM provider boundary

**GeckoTerminal -> EVM chain mapping -> Decimal-safe transport -> provider DTO -> canonical MarketPair/MarketSnapshot -> MarketRecorder -> PostgreSQL -> ORBIT read boundary**.

One `GeckoTerminalAdapter` handles both supported targets through immutable chain configuration: `robinhood/mainnet` means chain_id 4663; `bsc/mainnet` means chain_id 56. Chain IDs belong to that registry; canonical records retain existing chain/network fields rather than duplicating registry state. Provider IDs are independent Settings values. A pass-scoped NetworkDirectory verifies ID and CoinGecko asset-platform association against `/networks`: `robinhood` / `robinhood`, and `bsc` / `binance-smart-chain`. The provider's network list is not an RPC chain-ID attestation. A missing or mismatched entry rejects; a bounded incomplete scan reports budget exhaustion, never substitutes a network.

The transport pins public API V2 `Accept: application/json;version=20230203` and a project User-Agent. No credential is needed or sent. httpx uses finite connect/read/whole-request timeouts, bounded retries and Retry-After, a concurrency semaphore, and shared logical-request/HTTP-attempt budgets. Redirects and environment proxy inheritance are disabled. Decimal-safe JSON parsing rejects duplicate keys, nonfinite constants and malformed numbers. Typed safe errors do not contain upstream response bodies or transport diagnostics.

A network's first new-pools page requests included base/quote tokens and DEX data. Pool attributes provide base_token_price_usd, reserve_in_usd and volume_usd.h24 directly. Addresses come from explicit pool/token attributes, never by inventing an address from a JSON:API relationship ID. Resource IDs are cross-checked against the configured provider network and actual address. Token relationships must resolve to the correctly typed included resources. Canonical token addresses are lowercase nonzero 40-digit hex strings; base and quote must differ. Pool locators explicitly distinguish 20-byte CONTRACT_ADDRESS from 32-byte BYTES32_POOL_ID; both require nonzero hexadecimal values. Venue is the provider DEX relationship ID. No aggregate token values, unrelated metrics or creation timestamps are promoted into pool measurements.

Successful fetch-completion Clock time becomes observed_at. This is local retrieval provenance, not proof of underlying provider freshness; public endpoint caching can delay data. Every normalized event has its own UUID and correlation trace. The adapter's pass-scoped snapshot lookup preserves the exact discovered event for recording/replay; it clears on rediscovery, including failure. It is not authoritative state. Recorder insertion assigns independent recorded_at. `record_pair` shares the existing identity/provenance checks with `record_provider`; append-only storage and **observed_at DESC -> recorded_at DESC -> UUID deterministic fallback**, with rank-before-filter, remain unchanged.

Default bounds: 3 network pages + 2 chain pages = at most 5 logical requests; retries cannot exceed 8 HTTP attempts for the entire pass. Up to 3 pools per chain are inspected, with no detail lookups. The one-shot service is sequential and the transport defaults to concurrency 1. Malformed individual pools count as failures; conflicting duplicate resources abort the chain before recording its batch. Provider-wide outages abort remaining work; unsupported chain mapping alone allows the other chain to proceed. Existing committed observations remain durable and partial counts are reported safely.

PostgreSQL remains authoritative. No new schema, trading behavior or agent DB privileges are introduced. ORBIT receives only the existing read boundary, never DTOs, transports, sessions or write credentials. **Market observation != SENTINEL approval evidence.** No LLM, wallet, private-key, signing, transaction or live-execution feature is added. No Docker/container workflow, Solana or Birdeye integration is present in this phase. RPC/WebSocket watchers are deferred to Phase 1C.

## Later Phase 1 boundaries

Add independent secondary read-only feed adapters, provider-authenticated evidence, asynchronous agent runners, durable messages, scheduling, lifecycle state machines, database-backed dashboard queries, trace export, failed-attempt records, reconciliation, and operational policy management with authenticated access. Test feed freshness and liquidity-dependent price impact before expanding execution scope. Live execution, signing infrastructure, transaction recovery, and deployment security require a separate future phase.

## Explicit pool locators and exact market precision

EVM token identity remains a nonzero 20-byte address. A market can instead identify a standalone pool contract or a logical singleton pool. Immutable `PoolLocator` contains `kind` (`CONTRACT_ADDRESS` or `BYTES32_POOL_ID`), lowercase hex `value`, normalized `venue`, optional `pool_manager_address`, and explicit `manager_status` (UNKNOWN by default). Values require exactly 20 or 32 bytes according to kind; malformed hex and zero values reject. A bytes32 pool ID is not a wallet or contract address. JSON:API IDs only cross-check provider bindings; actual locators come from pool attributes.

Canonical contract pool IDs are `<chain>:<network>:contract_address:<value>`. Singleton IDs are `<chain>:<network>:bytes32_pool_id:<venue>:<value>`. MarketIdentity binds only PoolLocatorIdentity (kind, value, venue), provider, chain/network, base/quote assets and fixture marker. Manager address/status are enrichable routing metadata on the observation’s pair.pool_locator, excluded from pair_id and MarketIdentity equality. UNKNOWN to known (AVAILABLE in the existing availability enum) preserves both identities and the same provider/pair/fixture stream. Enrichment is a new append-only event; changing metadata under an existing event UUID still conflicts. Different venues cannot collapse the same bytes32 value. No PoolManager is inferred from a DEX name. Within one canonical chain/network/stable venue namespace, a bytes32 pool ID is the stable logical pool identifier. If independent PoolManagers later prove to have colliding IDs under that namespace, an explicit future deployment namespace/resolution model is required. Manager discovery must never mutate identity. Existing normalized venue identifiers remain stable (for example uniswap-v4-bsc and uniswap-v4-robinhood); manager knowledge never renames their namespace. Later PULSE/ANCHOR/execution would require venue-specific resolution and the relevant PoolManager/router, outside Phase 1B.

Migration `0003` widens indexed pair_id to VARCHAR(512) and permits schema versions 1 and 2. Migration `0002` is unchanged. New provider snapshots use version 2 and require a locator. Legacy version-1 observations remain readable and replayable without adding a locator key to their persisted payload or guessing their type. Downgrade to 0002 preserves compatible history and explicitly refuses incompatible version-2/long-ID rows; it never deletes or rewrites observations. Append-only triggers, concurrent idempotency and observed_at DESC -> recorded_at DESC -> id DESC rank-before-filter semantics remain intact.

Market measurements use PostgreSQL **JSONB containing exact Decimal strings**, not a fixed-scale NUMERIC column. API output also uses strings. Removing the market model's former 18-place restriction therefore requires no numeric column conversion. Accounting NUMERIC(38,18) and SENTINEL contracts remain unchanged. Market bounds allow at most 100 coefficient digits (including trailing zeros), with both Decimal tuple exponent and adjusted exponent within [-1000, 1000]. Finite nonnegative values are preserved without quantization or binary floats; available prices must remain positive.

JSON null and absent measurements both become UNKNOWN with value null. Numeric zero and string "0" become AVAILABLE Decimal("0") for liquidity/volume. Price zero remains UNKNOWN because a usable canonical price must be positive. Tests cover these distinctions and 19-place, 30-plus-place and scientific-notation prices through transport, DTOs, PostgreSQL, MarketReader and API. No observation constitutes SENTINEL approval evidence.

### One-shot operational counters

The summary prints `discovered`, `recorded`, `readable`, `unavailable`, `rejected`, `failed`, sorted safe `reasons`, and an optional pass-level `error`. Discovered counts inspected entries after recognized duplicate removal, within the configured bound. Recorded counts durable accepted writes/replays. After recording, each event is checked through the existing MarketReader boundary: readable counts that exact event still visible; unavailable counts successful writes hidden by availability/freshness/latest-event rules. Unavailable is not a provider or persistence failure. Readback errors leave the write counted as recorded and increment failed with `readback_failed`; they do not claim a successful visibility check.

Rejected counts isolated malformed provider entries. Failed includes rejected entries plus pass-level provider/recording/readback failures; it is not the number of unavailable observations. `reasons` groups fixed safe error codes (for example provider_identity or provider_contract), never payloads or exception text. A provider-wide or persistence failure can abort remaining work, so discovered need not equal recorded + rejected. The CLI returns nonzero for failures, including partial rejected results; successful unavailable observations alone do not cause a failure exit. Public request bounds remain unchanged.

## Phase 1C: controlled runtime infrastructure

GeckoTerminal → bounded scheduled MarketWatcher → unchanged MarketRecorder → PostgreSQL → MarketReader / ORBIT read boundary.

Managed Robinhood/BSC RPC/WSS → independent chain-ID verification → bounded notifications → confirmed HTTP gap recovery → separate EVM observations, cursors and audit history in PostgreSQL. These are separate source/provenance paths; raw logs are never MarketSnapshots or automatic SENTINEL evidence.

Migration 0004 introduces append-only runtime/log history and transactionally locked cursor projections. Session advisory ownership protects the watcher and each chain worker across processes. Confirmation lag, bounded recovery, reorg rewind auditing, backpressure and stale health are described in [Phase 1C](docs/phase-1c.md). Future agents consume controlled readers/evidence services, never arbitrary RPC clients or database write credentials. Existing PoolLocator identities, market precision, SENTINEL and paper accounting remain unchanged.

# rh-agents

Phase 1B EVM market-data ingestion and recording foundation for CryptoRoaster's autonomous multi-agent on-chain trading system, built on the Phase 0 paper executor. Agents will autonomously request trades; deterministic risk controls are mandatory and cannot be overridden. Individual trades do not require human approval.

**Paper only. No wallets, signing, transaction broadcasting, live trading or running LLM agents.** This repository is independent of ClawfredAI/polma-db.

## Local setup (macOS)

Prerequisites: native Python 3.12+, [uv](https://docs.astral.sh/uv/), native Node.js 20.9+, npm, and a native PostgreSQL service. PostgreSQL is an external service managed independently of the application.

For example, install and start [PostgreSQL 17 with Homebrew](https://formulae.brew.sh/formula/postgresql@17):

```sh
brew install postgresql@17
brew services start postgresql@17
export PATH="$(brew --prefix postgresql@17)/bin:$PATH"
pg_isready -h localhost -p 5432
createuser -h localhost -p 5432 --login --no-superuser --no-createdb --no-createrole rh_agents
createdb -h localhost -p 5432 --template=template0 --encoding=UTF8 --owner=rh_agents rh_agents
psql -h localhost -p 5432 -U rh_agents -d rh_agents -c 'SELECT current_database(), current_user;'
```

Run role/database creation once, using the PostgreSQL administrator account created for your macOS user by a fresh Homebrew installation. If using an existing instance, supply its administrator with `-U` and adjust the host/port as needed. The PATH setting above is a shell setup example only; application code does not depend on Homebrew paths.

The secret-free `.env.example` assumes the native instance accepts trust authentication on localhost:5432. Authentication and listening addresses belong to the PostgreSQL service configuration; the application does not configure them. Keep this local example restricted to loopback. If your instance requires a password, configure the role accordingly and set a matching `DATABASE_URL` in your untracked `.env`.

From the repository root (copy the example only if `.env` does not already exist):

```sh
cp .env.example .env
cd backend
uv sync --locked
uv run alembic upgrade head
uv run uvicorn src.api.main:app --reload --host 127.0.0.1
```

The backend and Alembic load the root `.env` when launched from `backend/`; exported environment variables take precedence. `DATABASE_URL` is required and accepts only `postgresql+asyncpg://` URLs with a database name. Missing, blank, malformed, SQLite, and unsupported-driver URLs fail settings validation; application code has no fallback URL. Native Unix-socket URLs such as `postgresql+asyncpg://runner@/rh_agents_test?host=/var/run/postgresql` are supported. SQLite is available only to tests that construct their database engine directly. `uv sync --locked` creates a native Python virtual environment in `backend/.venv/`. Alembic connects normally to the external PostgreSQL instance using `DATABASE_URL` and seeds $10,000 of fictitious paper cash. The backend defaults to OBSERVE; set `TRADING_MODE=PAPER` in the root `.env` only for future internal runner use. LIVE_AUTONOMOUS fails configuration validation.

In another terminal:

```sh
cd frontend
npm ci
npm run dev
```

Open http://localhost:3000. All dashboard portfolio values are illustrative fixtures. Switching OBSERVE/PAPER previews changes no backend configuration. LIVE AUTONOMOUS and the kill-switch button are disabled. Policy values are read-only placeholders.

Backend: http://localhost:8000/docs. `/health` reports process health, `/ready` verifies database migration readiness, and `/api/system` describes mode, component roles, and default limits. There are no HTTP trade or writable control endpoints.

## Production / VPS target (Linux)

Run PostgreSQL, the Python backend, and the Next.js frontend as native services on a Linux VPS. Install Python 3.12+, uv, Node.js 20.9+, npm, and PostgreSQL through the host's normal installation tools. Provision a dedicated PostgreSQL login and database owned by that login using the service's administrator account. PostgreSQL remains the authoritative production database; SQLite is only an optional lightweight test backend.

Create an untracked root `.env` with the service's `DATABASE_URL` and `TRADING_MODE=OBSERVE`. Use service-specific authentication credentials in production; the local trust example is for development only. Keep PostgreSQL on loopback when all services share a host. Run migrations before starting the backend, from `backend/`:

```sh
uv sync --locked
uv run alembic upgrade head
uv run uvicorn src.api.main:app --host 127.0.0.1 --port 8000
```

Build and start the frontend separately, from `frontend/`:

```sh
npm ci
npm run build
npm run start
```

These are the native process commands for the deployment target. Later systemd units may supervise them with the correct working directories and environment. A host reverse proxy can provide HTTPS access to the loopback services. Service units and a full production operations setup are deferred; Phase 0 remains paper only on every host.

## Structure

```text
backend/
  src/
    core/           Pydantic domain contracts, configuration, numeric precision
    data/           SQLAlchemy tables, sessions, typed persistence
    agents/         Agent roles and read-only ORBIT market input port
    markets/        Normalized market contracts, provider ports, recorder and reader
    risk/           SENTINEL deterministic policy
    execution/      Abstract Executor and deterministic PaperExecutor
    ledger/         Weighted-average spot accounting and PnL
    orchestration/  Typed bus, paper coordinator, and deterministic TradeCase workflow
    api/            Read-only FastAPI foundation
  tests/            Contract, safety, executor, accounting, persistence tests
  migrations/       Alembic PostgreSQL foundation, data runtime, and TradeCase workflow
frontend/
  app/              Next.js App Router and light dashboard theme
  components/       Dashboard shell and Recharts equity chart
  lib/              UI contracts and explicitly labeled fixtures
docs/               Phase notes and verification record
```

## Checks

```sh
cd backend
uv run pytest -q
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv run alembic upgrade head --sql
```

The normal suite uses SQLite in memory for fast persistence checks and skips eight PostgreSQL locking/concurrency/trigger checks. To validate PostgreSQL row locks, create a separate disposable database once using the local PostgreSQL administrator, then run from `backend/`:

```sh
createdb -h localhost -p 5432 --template=template0 --encoding=UTF8 --owner=rh_agents rh_agents_test
TEST_DATABASE_URL=postgresql+asyncpg://rh_agents@localhost:5432/rh_agents_test uv run pytest -q
```

Adjust authentication to match your instance. Each persistence test creates and drops its own uniquely named schema; the test user needs schema creation privileges. `TEST_DATABASE_URL` must be exported or passed to pytest as shown; the test fixture does not read it from `.env`. This does not replace testing Alembic upgrades against PostgreSQL.

```sh
cd frontend
npm run typecheck
npm run lint
npm run format:check
npm run build
```

Dependencies are locked in `backend/uv.lock` and `frontend/package-lock.json`. No coverage percentage gate is set; safety and accounting behavior must be covered explicitly. Use `uv run pytest --cov=src --cov-report=term-missing` for a coverage report.

`.github/workflows/ci.yml` runs the backend and frontend checks on pushes and pull requests using native Ubuntu runner processes, Python 3.12, and Node.js 22. The backend job installs and starts native PostgreSQL, creates a disposable database owned by the runner's login, and uses local Unix-socket peer authentication. It verifies Alembic upgrades/schema drift, the full PostgreSQL suite, and the optional SQLite suite. The frontend job installs locked dependencies and runs typecheck, lint, formatting, and the production build.

## Recorded market data (Phase 1A/1B)

`Market Provider -> normalization -> MarketRecorder -> PostgreSQL -> ORBIT input` is the new ingestion path. Immutable, versioned observations preserve exact Decimal values, provider provenance, chain/network identity, observation time and explicit UNKNOWN/unavailable values. The recorder deduplicates replay and rejects conflicting event identities; revision `0002` makes stored observations append-only.

Read-only endpoints: `/api/markets`, `/api/markets/{chain-qualified-asset-or-pair}`, and `/api/market-candidates`. Results must be fresh and have known price/liquidity; fixtures are excluded unless `include_fixtures=true`. Configure API freshness through `MARKET_MAX_AGE_SECONDS` (default 60). No mutation or trading endpoint is added. GeckoTerminal is the first real provider for the configured EVM target chains; deterministic fixtures remain available. No LLM is connected. PAPER remains the only trading-capable mode, with no live-money execution.

See [docs/phase-1.md](docs/phase-1.md) for interfaces, semantics, security boundaries, limitations and a runnable fixture-recording example. The dashboard remains a fixture preview.

## GeckoTerminal EVM ingestion (Phase 1B)

**GeckoTerminal -> EVM chain mapping -> Decimal-safe transport -> provider DTO -> canonical MarketPair/MarketSnapshot -> MarketRecorder -> PostgreSQL -> ORBIT read boundary**.

Targets: **Robinhood Chain mainnet (4663)** and **BNB Smart Chain mainnet (56)**. Internal chains are `robinhood` and `bsc`; provider network IDs are independently configured and verified against the public `/networks` listing each pass. Current provider mappings are `robinhood` and `bsc`, with matching CoinGecko platform identities. A missing network fails explicitly; an incomplete bounded scan reports a budget error. No fallback to another chain.

From `backend/`, with native PostgreSQL and migrations applied:

```sh
uv sync --locked
uv run alembic upgrade head
uv run python -m src.markets.ingest --provider geckoterminal --chain bsc --once
# Select robinhood or all for another deliberate pass; respect the public rate limit.
```

Alternatively set `MARKET_PROVIDER=geckoterminal` and `MARKET_CHAINS=robinhood,bsc` in your root `.env`, then use `--once`. Default provider stays `fixture` for normal application startup; the real-ingestion CLI requires GeckoTerminal selection. Existing fixture recording remains described in [docs/phase-1.md](docs/phase-1.md). Public ingestion needs no API key, paid-plan key, RPC URL, LLM key or executor secret. Prepared future environment values are ignored.

The [public API](https://api.geckoterminal.com/docs/index.html) is beta and documents approximately **10 calls/minute**. Requests pin `Accept: application/json;version=20230203`. Defaults: at most 2 chains, 3 inspected pools per chain, 3 network-list pages, 5 logical requests, 8 total HTTP attempts including one retry, concurrency 1. Both chains share network pages. `new_pools?include=base_token,quote_token,dex&page=1` already supplies compatible measurements and explicit token addresses; no pool detail calls are made. Repeated manual passes share the provider's quota; these limits are per process/pass, not a global quota service.

Pool base-token USD price, reserve USD and trailing 24-hour volume USD map directly to canonical measurements. JSON numbers are parsed with Decimal before validation; strings and numeric values retain canonical precision. Missing/null data remains UNKNOWN; explicit zero reserve/volume stays zero. Token identities use lowercase nonzero 20-byte addresses; pools carry an explicit CONTRACT_ADDRESS or BYTES32_POOL_ID locator, bound to venue. Fetch completion uses the trusted Clock; pool creation time is never freshness evidence. Recorder replay, immutable market identity binding and fail-closed latest-event filtering are preserved.

No Docker or container workflows. No wallets, signing, transaction construction/broadcasting or live-money execution. No Solana or Birdeye integration. **Market observation != SENTINEL approval evidence.** Data does not generate a trade, risk PASS or executor action. The dashboard remains a labeled fixture preview.

## Paper execution integration

`PaperTradingService(session_factory, RiskLimits(), TradingMode.PAPER).process(intent, market)` is the internal testable entry point. It derives portfolio context from the database, validates every final intent, simulates a fill, and records accounting atomically. The service defaults to `SystemClock`; trusted test setup may inject `clock=FixedClock(aware_datetime)` at construction. Trading callers cannot supply `now` or choose the clock. Time is read after the portfolio lock is acquired and read again when requesting execution to enforce approval expiry. Agents must never construct or mutate the service or its clock.

Pass fresh `marks` for other open assets. Missing safety data rejects execution. Identical intent IDs replay stored results; changed content with the same ID is rejected. The service is not exposed to LLM code or public HTTP callers.

`RiskDecision.position_size_limit_usd` is the absolute configured position limit. `max_additional_notional_usd` is conservative additional BUY notional at the snapshot quote price, before fees and slippage. It uses the smallest of cash, remaining total exposure, and remaining position capacity, divided by the worst permitted slippage and known fee factors. It is nonnegative and rounded down to 18 decimal places, with an additional check for intermediate cost rounding. SELL decisions and decisions with non-sizing safety blockers report zero. A BUY rejected solely for sizing can still report a smaller usable capacity. This guidance never authorizes a trade: the final intent must independently pass every SENTINEL check. Historical risk payloads with the old field remain readable and replay with zero incremental guidance; immutable stored events are preserved.

See [ARCHITECTURE.md](ARCHITECTURE.md) for the ten invariants, approval binding, accounting semantics, and future boundaries. See [docs/phase-0.md](docs/phase-0.md) for delivery verification and Phase 1 work. No commit or push is performed by setup or development commands.

## Explicit pool locators and exact market precision

EVM token identity remains a nonzero 20-byte address. A market can instead identify a standalone pool contract or a logical singleton pool. Immutable `PoolLocator` contains `kind` (`CONTRACT_ADDRESS` or `BYTES32_POOL_ID`), lowercase hex `value`, normalized `venue`, optional `pool_manager_address`, and explicit `manager_status` (UNKNOWN by default). Values require exactly 20 or 32 bytes according to kind; malformed hex and zero values reject. A bytes32 pool ID is not a wallet or contract address. JSON:API IDs only cross-check provider bindings; actual locators come from pool attributes.

Canonical contract pool IDs are `<chain>:<network>:contract_address:<value>`. Singleton IDs are `<chain>:<network>:bytes32_pool_id:<venue>:<value>`. MarketIdentity binds only PoolLocatorIdentity (kind, value, venue), provider, chain/network, base/quote assets and fixture marker. Manager address/status are enrichable routing metadata on the observation’s pair.pool_locator, excluded from pair_id and MarketIdentity equality. UNKNOWN to known (AVAILABLE in the existing availability enum) preserves both identities and the same provider/pair/fixture stream. Enrichment is a new append-only event; changing metadata under an existing event UUID still conflicts. Different venues cannot collapse the same bytes32 value. No PoolManager is inferred from a DEX name. Within one canonical chain/network/stable venue namespace, a bytes32 pool ID is the stable logical pool identifier. If independent PoolManagers later prove to have colliding IDs under that namespace, an explicit future deployment namespace/resolution model is required. Manager discovery must never mutate identity. Existing normalized venue identifiers remain stable (for example uniswap-v4-bsc and uniswap-v4-robinhood); manager knowledge never renames their namespace. Later PULSE/ANCHOR/execution would require venue-specific resolution and the relevant PoolManager/router, outside Phase 1B.

Migration `0003` widens indexed pair_id to VARCHAR(512) and permits schema versions 1 and 2. Migration `0002` is unchanged. New provider snapshots use version 2 and require a locator. Legacy version-1 observations remain readable and replayable without adding a locator key to their persisted payload or guessing their type. Downgrade to 0002 preserves compatible history and explicitly refuses incompatible version-2/long-ID rows; it never deletes or rewrites observations. Append-only triggers, concurrent idempotency and observed_at DESC -> recorded_at DESC -> id DESC rank-before-filter semantics remain intact.

Market measurements use PostgreSQL **JSONB containing exact Decimal strings**, not a fixed-scale NUMERIC column. API output also uses strings. Removing the market model's former 18-place restriction therefore requires no numeric column conversion. Accounting NUMERIC(38,18) and SENTINEL contracts remain unchanged. Market bounds allow at most 100 coefficient digits (including trailing zeros), with both Decimal tuple exponent and adjusted exponent within [-1000, 1000]. Finite nonnegative values are preserved without quantization or binary floats; available prices must remain positive.

JSON null and absent measurements both become UNKNOWN with value null. Numeric zero and string "0" become AVAILABLE Decimal("0") for liquidity/volume. Price zero remains UNKNOWN because a usable canonical price must be positive. Tests cover these distinctions and 19-place, 30-plus-place and scientific-notation prices through transport, DTOs, PostgreSQL, MarketReader and API. No observation constitutes SENTINEL approval evidence.

### One-shot operational counters

The summary prints `discovered`, `recorded`, `readable`, `unavailable`, `rejected`, `failed`, sorted safe `reasons`, and an optional pass-level `error`. Discovered counts inspected entries after recognized duplicate removal, within the configured bound. Recorded counts durable accepted writes/replays. After recording, each event is checked through the existing MarketReader boundary: readable counts that exact event still visible; unavailable counts successful writes hidden by availability/freshness/latest-event rules. Unavailable is not a provider or persistence failure. Readback errors leave the write counted as recorded and increment failed with `readback_failed`; they do not claim a successful visibility check.

Rejected counts isolated malformed provider entries. Failed includes rejected entries plus pass-level provider/recording/readback failures; it is not the number of unavailable observations. `reasons` groups fixed safe error codes (for example provider_identity or provider_contract), never payloads or exception text. A provider-wide or persistence failure can abort remaining work, so discovered need not equal recorded + rejected. The CLI returns nonzero for failures, including partial rejected results; successful unavailable observations alone do not cause a failure exit. Public request bounds remain unchanged.

## Phase 1C data runtime

The optional native runtime adds scheduled GeckoTerminal discovery and a separate managed EVM RPC/WSS path for Robinhood mainnet (4663) and BSC mainnet (56). Both are disabled by default. Configure `.env`, apply migration 0004, then run `uv run python -m src.runtime.main` from `backend/`. Enable only the services and chains whose configuration is ready.

The watcher defaults to 90 seconds after completion and holds PostgreSQL ownership. Chain workers verify both endpoints, recover confirmed gaps through HTTP, retain durable cursors, and expose read-only `/api/runtime` health. No wallet, signing, LLM or trading functionality is added. Market observation remains distinct from SENTINEL approval evidence. See [runtime operation, recovery and limits](docs/phase-1c.md).

## Phase 2A TradeCase workflow

Migration `0005` adds the PostgreSQL-authoritative team workflow for future ORBIT, ATLAS, SIGNAL,
VECTOR, FUSE, PULSE, ANCHOR, and COMMANDER workers. The versioned evaluator derives state from
immutable typed evidence, trusted-time freshness, structured blockers, and the existing deterministic
SENTINEL decision. VECTOR/PULSE/ANCHOR references and a canonical safety-evidence digest prevent
stale setup, trigger, liquidity, or authorization reuse. A single deterministic `RiskAuthorization`
classifier reads each final SENTINEL decision as APPROVED, LIMITED, or REJECTED; a pause or any
non-sizing rejection fails closed, and both authorized states stay revalidatable. Row locks, monotonic revisions, unique idempotency keys, and
append-only audit tables make concurrent replay deterministic.

Read-only observability is available at `/api/trade-cases`, with case detail, timeline, evidence, and
task subresources. No public mutation route or active worker is added. Future workers must submit
typed evidence through the internal service; they receive no arbitrary database writes, signer,
executor, status override, or SENTINEL override. See [the Phase 2A lifecycle and boundaries](docs/phase-2a.md).

## Phase 2B worker capability runtime

Migration `0006` adds the durable runtime that future ORBIT, ATLAS, SIGNAL, VECTOR,
PULSE, ANCHOR, FUSE and COMMANDER workers must use. Task processing is
at-least-once with database-enforced single active leases, durable idempotency and
immutable attempt history, so a crashed or slow worker can safely repeat work
without duplicating any authoritative effect.

Workers receive role-composed capabilities rather than infrastructure: no database
session, RPC or HTTP client, signer, wallet, executor or ledger write exists in any
capability, and every submission is independently re-verified server-side. Evidence
recording, task completion and deterministic evaluation commit atomically. Expired
leases are reclaimed by bounded sweeps, and typed failure categories drive bounded
retries with durable backoff instead of endless looping.

Read-only observability lives at `/api/worker-runtime`, `/api/workers` and
`/api/worker-attempts`. There is no public claim, heartbeat or completion route.
`WORKER_RUNTIME_ENABLED` is `false` by default and no reasoning worker, model
provider or prompt is included. See [the Phase 2B runtime and capability model](docs/phase-2b.md).

## Phase 2C ORBIT specialist worker

ORBIT is the first real specialist worker and the reference implementation for the
rest of the team. It claims ORBIT tasks through the Phase 2B runtime, reads one
purpose-built view of the recorded market candidate, reasons over it behind a
provider-neutral structured-reasoning port, and submits typed `DISCOVERY_EVIDENCE`.
It has no session, RPC client, HTTP client, signer, executor, ledger write or
SENTINEL access, and no generic tool surface.

Discovery is not trade authority: the output schema cannot express a side, size,
route or approval, and the deterministic workflow alone decides what the evidence
means. Model output is validated against the input it was given, so invented
observation references, the wrong market, or a value claimed for an unobserved
measurement are rejected rather than recorded. Zero and UNKNOWN stay different
facts, instructions and market data travel in separate channels so hostile token
metadata stays quoted data, and no API key, raw vendor response or model reasoning
transcript is ever persisted.

Every automated test uses a deterministic offline provider. `REASONING_PROVIDER` is
`disabled` and `ORBIT_WORKER_ENABLED` is `false` by default, and an ambient
`ANTHROPIC_API_KEY` activates nothing on its own. Phase 2C ships no worker
launcher either, so even a fully configured environment cannot make a paid call
without new code. Phase 2C adds no migration. See
[the ORBIT reference implementation](docs/phase-2c.md).

## Phase 2D ATLAS on-chain intelligence

ATLAS is the first safety-critical specialist worker. Deterministic collectors
establish on-chain facts with explicit availability and provenance, a versioned
code-defined policy computes the safety verdict from those facts alone, and only
then may a model add advisory commentary. A model that insists everything is safe
cannot clear a blocker, cannot turn a missing fact into an available one, and
cannot name an address it was not shown.

Known bad is never recorded as unknown: a measured violation is available
evidence whose content blocks the case, while an unobtainable fact is
insufficient evidence that blocks for a different reason. Supporting this
required one generic Phase 2A extension, `EvidenceAcceptance`, checked in a
single place for every evidence type.

Holder concentration and contract creator currently have no verified provider for
Robinhood Chain or BSC and are reported UNAVAILABLE, so ATLAS cannot reach CLEAR
in a real deployment yet. That is the intended fail-closed behaviour. Phase 2D
adds no migration and starts no worker. See
[the ATLAS safety model and provider capability matrix](docs/phase-2d.md).

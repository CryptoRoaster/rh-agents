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

Phase 2D shipped without a verified holder or creator provider, so both domains
were reported UNAVAILABLE and ATLAS could not reach CLEAR — the intended
fail-closed behaviour, resolved in Phase 2E. Phase 2D adds no migration and
starts no worker. See [the ATLAS safety model](docs/phase-2d.md).

## Phase 2E ATLAS data enablement

Phase 2E connects verified holder and contract-origin providers so the required
holder domain can finally be satisfied — without weakening the policy. Robinhood
Chain uses the credentialed Blockscout PRO API for holders and creation; BNB
Smart Chain uses Moralis for holders and Etherscan V2 for creation, because
Blockscout does not index BSC. The public Robinhood explorer host answers
server-side clients with an interactive challenge page and is deliberately not
used: satisfying it would mean impersonating a browser.

Providers supply raw rows and provenance only. Every concentration is computed
here from integer balances against on-chain `totalSupply()` — no float, no vendor
percentage — and we exclude nothing from the raw metric, so a liquidity pool or a
burn address stays visible. What a provider filters upstream is declared rather
than inferred: Blockscout removes the zero address from its holder list, which is
recorded on the fact and withholds the burn adjustment that would otherwise rest
on a lower bound.

Coverage is explicit (`COMPLETE`, `TOP_N_ONLY`, `UNKNOWN`) and freshness is
anchored to what the source observed rather than to when we fetched. Where the
source names a block — Robinhood via Blockscout — an indexer lagging the chain
goes stale rather than being rescued by a fresh round-trip. Where it names none
— BSC via Moralis — only the response receipt time exists, that weaker basis is
labelled on the fact, and the policy field that accepts it is the same field a
future live policy must narrow.

The concentration threshold stays disabled: data became available, a product
decision did not. A holder-domain PASS therefore means the data-quality
prerequisite was met, never that the distribution was judged safe. Every provider
defaults to `disabled`, a credential alone activates nothing, no worker is
started and no migration is added. See
[the Phase 2E provider research and support matrix](docs/phase-2e.md).

## Phase 2F SIGNAL social attention

SIGNAL reads what people publicly said about a candidate and reports it on six
separate axes — sentiment direction and strength, attention, organic breadth,
manipulation concern and social demand indication — rather than as one score,
because collapsing them is exactly how a promotional campaign passes for a
community.

The counts are not the model's to decide. How many distinct people wrote
something, how much of the text was the same sentence repeated, how concentrated
authorship was and whether it all landed inside one minute are computed in code,
and a model may explain them but never contradict them: a claim of broad interest
is capped by the measured breadth of who actually spoke. Reposts are counted as
attention rather than as opinions, so one amplified voice cannot read as a crowd.

Freshness anchors to when a post was written, never to when it was fetched. A
bare ticker can never bind a post to a market, an exact contract address is
re-checked against this token and this chain, and an account can only speak for
the project if a trusted identity mapping says so — a provider label is not
verification. Identity itself is namespaced per platform, so the same handle on
two networks stays two people. Posts reach the model as quoted
data, and the output schema has no field for a side, a size or an approval, so an
injected instruction has nothing to aim at.

The deterministic fake source is test-only and reachable from no startup path —
nothing can quietly carry a real case forward on synthetic sentiment. Phase 2G
below connects the first real source. SIGNAL stays required and not
safety-critical: unusable social data leaves the requirement pending rather than
blocking a case, and a negative reading stops nothing. No migration, no worker
started. See [the Phase 2F SIGNAL design and provider research](docs/phase-2f.md).

## Phase 2G SIGNAL data enablement

Phase 2G gives SIGNAL real public Farcaster posts through Neynar. Nothing about
SIGNAL changed: the adapter supplies observations, and every judgement about what
they mean stays where Phase 2F put it.

Three provider options are deliberately left off. Search is literal and
chronological rather than relevance-ranked, because a ranked set is a sample
someone else chose. No viewer is supplied, because one account's mutes would
decide what SIGNAL sees. No provider-side spam filtering is requested and the
provider's own user score is used for nothing, because SIGNAL exists to measure
campaigns and a provider that removes them first removes the evidence.

The hard part was chain identity. The same contract address can exist on two
chains, and Farcaster search is not chain-aware, so "we searched for our address,
therefore this post is about our token" is circular. Chain context comes only
from the post itself — an allowlisted explorer link or an explicit chain name —
and an exact address without it is recorded as a weak, unscoped reference rather
than attributed to our chain. Anyone can register a domain that spells a chain's
name, so links only count through the allowlist, and a name only counts when the
post actually asserts it: "not on BSC" and "maybe BSC" establish nothing.

What was collected is described as what it is. A result set the provider ran out
of is typed apart from one our own page budget cut short, and a cut-short sample
is flagged, capped at degraded quality, and openly biased toward the newest
posts.

The provider is disabled by default and a key alone selects nothing. There is no
synthetic fallback: if the provider cannot answer, SIGNAL is unavailable and the
case waits. No scraping, no wallet or payment path, no migration, no worker
started. See [the Phase 2G provider contract and chain-identity design](docs/phase-2g.md).

## Phase 2H VECTOR trade setup

VECTOR proposes the setup: a side, an entry, the level at which the idea is
wrong, ordered objectives and an expiry. It is the first specialist whose output
describes an action, and it is still only evidence. PULSE watches the trigger,
ANCHOR assesses execution conditions and SENTINEL decides risk, independently and
in that order.

Nothing here can size, route or approve anything. The schema has no field for a
position size, a slippage tolerance, a venue or an approval, so a model that
fully complied with a hostile instruction embedded in another role's summary
would still have nowhere to put one — and `extra="forbid"` turns the attempt into
a parse error at the boundary.

A setup is refused, never repaired. An invalidation above the entry is not
quietly reordered, unsorted targets are not sorted, an expiry past the horizon is
not clamped and a level with a lost decimal point is not pulled back to the edge.
Each of those would create a setup nobody proposed while the record still
credited it to the model. The proposal is rejected with a reason code and the
attempt retries.

A setup is a statement about price levels, so a missing, stale, mismatched or
priceless market observation ends the attempt before the model is ever called.
There is no fallback setup and the previous one is never reissued as new: a stale
setup silently renewed would be the most dangerous artefact this system could
produce, because everything downstream reads a current setup as a current
opinion.

A pre-push audit found the version of this that shipped first could do something
worse. Given one observed price of 1.00 and nothing else, it produced an entry at
1.10, an invalidation at 0.92 and targets at 1.25 and 1.45 — and recorded them as
available, accepted evidence. The levels were ordered correctly and inside the
sanity envelope, so every check passed. None of them was in the data. A validator
can prove a setup holds together; it cannot prove anyone had reason to propose
it.

So VECTOR now reads bounded market history — closed hourly bars for the same pool
— and two gates stand either side of the model. Before it, a deterministic
verdict decides whether enough structure exists to ask at all: enough closed
bars, recent enough measured from when they closed, few enough untraded gaps, and
the right pool in the right unit. The model is never asked whether its own input
was good enough. After it, every proposed level must sit inside the range the
market actually traded, widened enough that a breakout above every recorded high
is still proposable and a number from nowhere is not.

If that structure cannot be obtained, nothing is produced and the case waits.
That is the point: no setup is better than an invented one. No indicator is
computed, no candle is synthesized, and an interval nobody traded in is reported
as a gap rather than filled with a flat bar.

Because the setup is safety-critical, replacing it withdraws what was built on
it. The trigger that named the old setup stops matching, the execution
assessment underneath it stops being current, and an existing risk approval — or
a limited authorization — is revoked. An expired setup blocks rather than
lingers, because the evidence expires exactly when it does.

History comes from GeckoTerminal's OHLCV endpoint, verified live on both
supported chains. Its newest bar is always still forming — its close matched the
pool's live price exactly on both — so that bar is dropped rather than treated as
settled. Orientation is a request parameter, and getting it wrong is not obvious:
on one pool the wrong unit landed within 0.2% of the right one, so the request is
explicit and the response's own account of which token it priced is checked
against ours.

The bars a setup was drawn from are kept with it. A fingerprint can prove two
inputs are the same; it cannot tell you what either one was, and a provider that
revises a candle would leave the decision unexplainable. So the evidence stores
the bounded normalized window itself — the bars the model actually saw, nothing
about how they were fetched — and reconstructs to an exact digest match. It is
one decision's input, not a market-data archive.

A rate limit is never mistaken for an empty market. The provider's failures are
typed at the adapter, so a 429 is transient weather rather than a claim that this
market has no history — and never an internal error in our own code.

Disabled by default, no migration, no worker started. See [the Phase 2H VECTOR
design](docs/phase-2h.md).


## Phase 2I PULSE trigger monitor

VECTOR says what to wait for. PULSE waits for it.

It is the first specialist with no model at all. Every one before it had to
interpret something — a token, a chain, a crowd, a market. This one compares two
numbers. A model here would add cost, latency and variance to a question with a
single correct answer, and would leave nobody able to say afterwards why the
system acted, so the package contains no prompt and imports no provider.

Only the grammar VECTOR already wrote is evaluated: at or above a level, at or
below one, or inside a band. Exact decimals, inclusive boundaries because the
contract says inclusive, and no epsilon. There is no smoothing, no averaging and
no "wait for two ticks to be sure" — each of those is a trading opinion dressed
as caution, and none of them is in the setup being watched.

The hard part was not the comparison. A monitor's normal answer is "not yet",
possibly for hours, and the worker runtime could previously only say "done" or
"failed". Reporting patience as failure would have burned a three-attempt budget
in minutes and filled the permanent record with incidents that never happened.
So waiting became a real answer: the attempt closes honestly, the task is
rescheduled in the database, and nobody is paged. One check per claim, so a
thousand waiting cases are a thousand rows rather than a thousand loops.

Who decides *when* to look again matters too. At first the worker proposed the
interval and the runtime merely trimmed it, which quietly meant any worker could
postpone any task for as long as it liked. The schedule now belongs entirely to
the server, a task may only wait if its own policy says so and only for reasons
that policy lists, and the one timing hint a monitor can still give can shorten
the wait but never extend it.

The first version of this could miss the thing it was built to catch. It read
only the newest recorded price, so a market that crossed the level and came back
before the next check looked exactly like a market that had never crossed at all
— even though the crossing was sitting in the database. It now reads the whole
recent window and takes the earliest crossing in it, which also means the
evidence can answer *when* the system first saw the trigger.

What it promises is worth being precise about: it does not watch the market, it
watches what the market layer recorded. Inside that stream nothing qualifying is
skipped. Outside it, nothing is claimed. And a crossing that has aged out is
still ignored on purpose — it describes a market that has moved on, and proving a
trigger fired is not the same as proving the price is still there. ANCHOR decides
that part later, and may well say no.

A price from the wrong pool, or in the wrong unit, is not a price that has failed
to reach a level — it is incomparable, and saying "not yet" would mean waiting
patiently for something that can never happen. Those are faults. Being too old is
not; the feed may catch up. Freshness is judged by when the market had the price,
never by when we read it.

Evidence is written only when the condition actually held, and records the
comparison that was made rather than any account of it. A check that found
nothing writes nothing at all.

Disabled by default, no migration, no worker started. See [the Phase 2I PULSE
design](docs/phase-2i.md).

## Phase 2J ANCHOR execution liquidity

PULSE says the moment arrived. ANCHOR asks what the market can actually take.

The easy version of this is one line long and completely wrong:

```python
max_safe_size = liquidity_usd * 0.01
```

A pool holding ten million dollars is not a promise that ten thousand can be
traded through it at a price anyone would accept. The money may be sitting in a
range the price has left, the trade may have to be split across several venues
with very different depth, and the number says nothing at all about fees or the
spread you would cross. Worse, it never fails — it is computed from a figure
that is always there, so it always produces an answer, and the answer is fiction.

So the question is asked properly instead: quote a hundred dollars, then five
hundred, then two and a half thousand, and keep going until the market says no.
A fixed ladder rather than a clever search, because every rung is a real request
against somebody else's API and a fixed list is one anybody can check afterwards.

Then there is the question of what "$500" actually is.

The ladder is in dollars, because that is the language risk speaks. The exchange
wants to be told a number of tokens. Those are only the same thing if the token
you are paying with happens to be worth a dollar — and the first version of this
quietly assumed it was. The rung labelled $500 asked for 500 tokens. Pay in
something worth $600 and that is a $300,000 order wearing a $500 label, every
quote comes back looking sensible, and the number that lands in the evidence is
six hundred times too big in a field whose name ends in `_usd`.

So the dollars are converted first, through an actual observed price of the
thing you are paying with. No pegs. No "it has USD in the name, call it a
dollar" — a stablecoin trading at $0.97 buys a thousand tokens for $970, because
that is what $970 buys. And if there is no trustworthy price for the payment
asset, ANCHOR stops there and says so, before asking the provider anything. A
capacity figure nobody downstream can read is worse than no figure at all.

What comes out is careful about what it claims. If a size failed, capacity is
bracketed between the largest that worked and the smallest that did not. If
*every* size passed, the honest answer is "at least this much" — the top of the
ladder is the largest amount tried, not a measured ceiling — and the type system
makes that impossible to confuse with a limit. If nothing was established there
is no number at all, because a zero would mean "the market supports nothing",
which is a measurement nobody made.

It is also careful about the gap between the rungs. If $500 worked and $2,500
did not, the honest statement is "those two sizes were tried, and those were the
answers" — not that $500 is the ceiling, and not that anything in between was
tested. Nothing guesses at the middle.

And whatever comes out is not permission. It says what the market bears. How
much of that anyone may trade is SENTINEL's to say, and it may well be far less.

Three things that sound alike are kept apart. The **deviation** is what ANCHOR
measures: how far a quote's real price sits from a current independent one. The
**provider's impact figure** is the provider's own opinion in its own units,
recorded when offered and never mixed in. **Slippage** is the gap between a
quote and an actual fill, and this system has never seen a fill, so it does not
pretend to know.

Quotes go stale fast. Thirty seconds, timed from when the provider priced it
rather than when the answer arrived, and the rungs of one ladder have to sit
within twenty seconds of each other — otherwise you are watching the market move
and calling it depth.

"No route" and "the provider is down" are different answers and stay different
all the way through. One is a fact about the market you can act on. The other is
not knowing, and a retry that then succeeds is not the market recovering.

There are two older fields on this evidence — one named for slippage, one for
price impact — and the tempting thing is to put the nearest-looking number in
them. The deviation is *nearly* slippage. It is not slippage: slippage is what a
real fill costs you, and nothing here has ever done a fill. In this codebase that
field name already means a realisable cost; something else reads it and moves a
price with it. So it stays empty. The impact field stays empty too, because it
means *the provider's* number and this provider doesn't publish one. Borrowing
its name for a figure we worked out ourselves would be a small, convenient lie.

An optional live check against the real API — off by default, no credentials,
read-only — found two things reading the documentation had not. The provider
reports "no route" as an HTTP 400 with the reason in the body, so the first
version had been filing every genuinely unroutable pair as an outage. And while
the quote endpoint returns no transaction to sign, it does return the contract a
swap would be *sent to*. Nothing downstream had ever read it, so it is no longer
parsed at all.

A later pass found two more things worth knowing. The provider's own timestamp
is undocumented and tracks when it handled the request rather than any pinned
chain state, so freshness now uses whichever is older — its clock or ours — and
a clock running fast can only ever count against a quote. And the block numbers
in the response live inside blobs this code deliberately never opens, where three
of them in a single hop disagreed with each other. So the ladder is held together
by time, not by a block. That is a weaker promise, and it is the one the data
actually supports.

One correction to an earlier note: Robinhood *is* listed in KyberSwap's official
supported-networks documentation. An earlier version of this page said it worked
but wasn't documented. It is both, and the table now keeps "works today" and
"the provider says it supports this" in separate columns, because they are
different claims and only one of them is a promise.

That is the theme of the whole phase. This is the last stop before something
builds a real transaction, so it does not carry the parts of one.

Disabled by default, no migration, no worker started. See [the Phase 2J
execution-liquidity design](docs/phase-2j.md).

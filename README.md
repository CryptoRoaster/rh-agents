# rh-agents

Phase 0 foundation for CryptoRoaster's autonomous multi-agent on-chain trading system. Agents will autonomously request trades; deterministic risk controls are mandatory and cannot be overridden. Individual trades do not require human approval.

**Paper only. No wallets, signing, blockchain calls, credentials, live trading, or running LLM agents.** This repository is independent of ClawfredAI/polma-db.

## Local setup (macOS)

Prerequisites: native Python 3.12+, [uv](https://docs.astral.sh/uv/), native Node.js 20.9+, npm, and a native PostgreSQL service. PostgreSQL is an external service managed independently of the application.

For example, install and start [PostgreSQL 17 with Homebrew](https://formulae.brew.sh/formula/postgresql@17):

```sh
brew install postgresql@17
brew services start postgresql@17
export PATH="$(brew --prefix postgresql@17)/bin:$PATH"
pg_isready -h localhost -p 5432
createuser -h localhost -p 5432 --login --no-superuser --no-createdb --no-createrole rh_agents
createdb -h localhost -p 5432 --owner=rh_agents rh_agents
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

The backend and Alembic load the root `.env` when launched from `backend/`; exported environment variables take precedence. `uv sync --locked` creates a native Python virtual environment in `backend/.venv/`. Alembic connects normally to the external PostgreSQL instance using `DATABASE_URL` and seeds $10,000 of fictitious paper cash. The backend defaults to OBSERVE; set `TRADING_MODE=PAPER` in the root `.env` only for future internal runner use. LIVE_AUTONOMOUS fails configuration validation.

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
    agents/         Agent and infrastructure role registry
    risk/           SENTINEL deterministic policy
    execution/      Abstract Executor and deterministic PaperExecutor
    ledger/         Weighted-average spot accounting and PnL
    orchestration/  Typed asyncio bus and transactional paper coordinator
    api/            Read-only FastAPI foundation
  tests/            Contract, safety, executor, accounting, persistence tests
  migrations/       Alembic initial PostgreSQL schema
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

The normal suite uses SQLite in memory for fast persistence checks and skips two concurrency tests. To validate PostgreSQL row locks, create a separate disposable database once using the local PostgreSQL administrator, then run from `backend/`:

```sh
createdb -h localhost -p 5432 --owner=rh_agents rh_agents_test
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

## Paper execution integration

`PaperTradingService(session_factory, RiskLimits(), TradingMode.PAPER).process(intent, market, now=...)` is the internal testable entry point. It derives portfolio context from the database, validates every final intent, simulates a fill, and records accounting atomically. Pass fresh `marks` for other open assets. Missing safety data rejects execution. Identical intent IDs replay stored results; changed content with the same ID is rejected. It is not exposed to LLM code or public HTTP callers.

See [ARCHITECTURE.md](ARCHITECTURE.md) for the ten invariants, approval binding, accounting semantics, and future boundaries. See [docs/phase-0.md](docs/phase-0.md) for delivery verification and Phase 1 work. No commit or push is performed by setup or development commands.

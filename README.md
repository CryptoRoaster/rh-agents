# rh-agents

Phase 0 foundation for CryptoRoaster's autonomous multi-agent on-chain trading system. Agents will autonomously request trades; deterministic risk controls are mandatory and cannot be overridden. Individual trades do not require human approval.

**Paper only. No wallets, signing, blockchain calls, credentials, live trading, or running LLM agents.** This repository is independent of ClawfredAI/polma-db.

## Local setup

Prerequisites: Python 3.12+, [uv](https://docs.astral.sh/uv/), Node.js 20.9+, npm, and Docker Compose.

```sh
cp .env.example .env
docker compose up -d
cd backend
uv sync --locked
uv run alembic upgrade head
uv run uvicorn src.api.main:app --reload --host 127.0.0.1
```

PostgreSQL binds to localhost:5432 and uses trust authentication solely for local development. No password is needed. Do not expose this Compose service on a shared or public network. Alembic seeds $10,000 of fictitious paper cash. The backend defaults to OBSERVE; set `TRADING_MODE=PAPER` in the root `.env` only for future internal runner use. LIVE_AUTONOMOUS fails configuration validation.

In another terminal:

```sh
cd frontend
npm ci
npm run dev
```

Open http://localhost:3000. All dashboard portfolio values are illustrative fixtures. Switching OBSERVE/PAPER previews changes no backend configuration. LIVE AUTONOMOUS and the kill-switch button are disabled. Policy values are read-only placeholders.

Backend: http://localhost:8000/docs. `/health` reports process health, `/ready` verifies database migration readiness, and `/api/system` describes mode, component roles, and default limits. There are no HTTP trade or writable control endpoints.

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

The normal suite uses SQLite in memory for fast persistence checks and skips two concurrency tests. To validate PostgreSQL row locks, set `TEST_DATABASE_URL` to a disposable PostgreSQL database and rerun pytest. Each persistence test creates and drops its own uniquely named schema; the test user needs schema creation privileges. This does not replace testing Alembic upgrades against PostgreSQL.

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


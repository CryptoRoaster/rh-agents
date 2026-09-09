# Repository Guidelines

## Project Structure & Module Organization

CryptoRoaster/rh-agents is a new autonomous trading project. Work only here; never copy, modify, or depend on ClawfredAI/polma-db. Phase 0 supports paper execution only.

`backend/src/` contains typed contracts (`core`), PostgreSQL persistence (`data`), agent roles (`agents`), deterministic policy (`risk`), executors (`execution`), accounting (`ledger`), asynchronous coordination (`orchestration`), and FastAPI (`api`). Tests live in `backend/tests/`; Alembic revisions in `backend/migrations/`. The Next.js shell uses `frontend/app/`, `components/`, and `lib/`. Design notes live in `docs/` and `ARCHITECTURE.md`.

## Architectural Invariants

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

## Build, Test, and Development Commands

Use native Python, Node.js, and an independently managed native PostgreSQL service. On macOS, an example setup is `brew install postgresql@17` followed by `brew services start postgresql@17`; see README.md for role/database creation. Homebrew paths belong only in documentation, never application code. Copy `.env.example` to the root `.env` and configure `DATABASE_URL` for the local instance. In `backend/`, run `uv sync --locked`, `uv run alembic upgrade head`, and `uv run uvicorn src.api.main:app --reload`. These commands load the root `.env`. Validate with `uv run pytest`, `uv run ruff check .`, `uv run ruff format --check .`, and `uv run mypy`.

In `frontend/`, run `npm ci`, `npm run dev`, `npm run typecheck`, `npm run lint`, and `npm run build`. Use `npm run format:check` to verify formatting.

Development and deployment must use native host processes exclusively. The production target is a Linux VPS with native PostgreSQL, Python backend, and Next.js frontend services; later systemd units may supervise them. PostgreSQL is authoritative for all runtime modes. Runtime Settings require `DATABASE_URL` with the `postgresql+asyncpg://` driver and a database name; native Unix-socket URLs are valid. SQLite, unsupported drivers, malformed URLs, and missing/blank values must fail validation. SQLite is optional for lightweight tests that construct their engine directly, never through runtime Settings.

## Coding Style & Naming Conventions

Use Python 3.12+, four spaces, typed functions, snake_case modules, Ruff formatting, and strict mypy. Use Decimal for money. Frontend code uses strict TypeScript, PascalCase components, two-space Prettier formatting, and ESLint. Keep risk rules deterministic and UI fixtures explicitly labeled.

## Testing Guidelines

Name pytest files `test_*.py`. Cover rejection, UNKNOWN inputs, approval binding, accounting, and idempotency. Set `TEST_DATABASE_URL` to a disposable PostgreSQL database for concurrent transaction tests. No coverage percentage gate is configured; never substitute SQLite checks for PostgreSQL locking verification.

## Commit & Pull Request Guidelines

No commit history establishes a convention. Use concise imperative subjects. Describe behavior, linked issues, checks, and screenshots for UI changes. Do not commit or push automatically.

## Security & Configuration

Keep credentials and generated output out of Git. `.env.example` uses local trust authentication only. Never add private keys, seed phrases, signing libraries, or live trading paths in Phase 0.

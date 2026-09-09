# Phase 0 delivery

## Implemented

- Python 3.12+ FastAPI backend with strict Pydantic contracts for all fourteen requested domain objects, execution timing, safety metrics, controls, and typed asynchronous decisions.
- Eight planned LLM roles; SENTINEL and LEDGER are deterministic services, and EXECUTOR is infrastructure.
- Fail-closed deterministic risk policy, exact intent/snapshot approval binding, BUY/SELL paper simulation, and explicit fees/slippage/gas fields.
- SQLAlchemy 2 PostgreSQL tables, initial Alembic migration, one fictitious paper account, atomic accounting, persistent idempotency, and portfolio row locking.
- Weighted-average long-only spot positions and fee-aware realized/unrealized PnL.
- Next.js, strict TypeScript, Tailwind CSS, Recharts, responsive off-white dashboard, all requested sections, agent filters, and read-only policy placeholders.
- Root contributor/architecture/setup documentation, native external PostgreSQL setup for macOS and Linux, secret-free environment example, ignore rules, and dependency locks.
- Explicit absolute position limit and conservative additional BUY sizing guidance, required database configuration, and a trusted injected clock with deterministic test support.
- GitHub Actions checks using native Ubuntu processes, including native PostgreSQL migration and locking verification, strict backend checks, and the frontend production build.

## Verification

Phase 0 hardening validated locally on Python 3.13.2, Node 22.22.2, Next.js 16.3.4, and native PostgreSQL 16.14 on 2026-09-09. Browser evidence below is retained from the initial Phase 0 verification; frontend behavior was unchanged by hardening.

| Check | Result |
| --- | --- |
| Backend pytest, PostgreSQL adapter | 123 passed; 95% statement coverage |
| Backend pytest, SQLite adapter | 120 passed; 3 PostgreSQL locking tests skipped |
| Ruff lint and formatting | Passed |
| Strict mypy with typed Pydantic plugin | Passed, 24 source files |
| Alembic upgrade against isolated PostgreSQL | Passed |
| Alembic schema drift check | No new upgrade operations detected |
| Alembic offline PostgreSQL SQL generation | Passed |
| Frontend TypeScript | Passed |
| Frontend ESLint | Passed, no warnings |
| Frontend Prettier | Passed |
| Next.js production build | Passed; home and not-found pages prerendered |
| npm dependency installation audit | Zero reported vulnerabilities |
| GitHub Actions workflow, actionlint 1.7.12 | Passed static validation; hosted execution not verified by this local run |
| Browser rendering (initial Phase 0) | Desktop and 390px mobile layouts render; no console errors or framework error overlay; no page-level horizontal overflow |
| Browser interactions (initial Phase 0) | Agent filters and mode previews verified using DOM/keyboard activation; live and pause controls disabled |

PostgreSQL concurrency tests verify that simultaneous identical intents produce one fill, different concurrent intents cannot exceed the portfolio exposure limit, and trusted time is sampled after waiting for the account lock. Persistence tests also cover replay after service restart, legacy rejection payloads, conflicting IDs, durable rejection, a latched pause, transactional rollback with correlated error logs, missing portfolio marks, trusted-clock freshness, rejection of caller-supplied runtime time, and approval expiry before execution. Sizing tests cover all three budget constraints, fees/slippage, rounding, exhausted headroom, unknown safety inputs, SELL semantics, and independent final validation. Configuration tests cover mandatory PostgreSQL/asyncpg runtime URLs, database names, malformed and blank values, unsupported drivers, SQLite rejection, Unix-socket URLs, and environment/`.env` loading. SQLite remains available only through directly constructed test engines.

The migration and database tests used an isolated native PostgreSQL 16.14 instance. The macOS setup guide provides a Homebrew PostgreSQL 17 example; that version specifically remains unverified. The Linux VPS target uses native PostgreSQL, Python backend, and Next.js frontend services, with systemd supervision deferred.

Locked dependency installation succeeded. Native database verification used a dedicated non-superuser login owning a disposable database and checked TCP and Unix-socket connectivity. The temporary PostgreSQL instance was stopped and removed afterward. The CI workflow uses Python 3.12 and native PostgreSQL on Ubuntu; those hosted jobs require a separate GitHub Actions run. The final runtime URL validation fix reran both backend suites, Ruff, formatting, strict mypy, and Alembic checks. Frontend source was unchanged; frontend checks/build and actionlint results above are from the preceding hardening pass and were not repeated for the URL validation fix.

The browser CLI's semantic pointer clicks did not reliably activate off-screen controls. DOM activation and keyboard events verified the React behavior; a complete manual pointer-interaction/accessibility audit is not claimed.

## Decisions and limits

The final intent always passes SENTINEL again after FUSE/COMMANDER. No individual trade approval is introduced. Runtime risk policy remains trusted infrastructure, outside agent payloads. The API is read-only, defaults to OBSERVE, and rejects live configuration. The internal paper service requires explicit PAPER mode.

PostgreSQL is authoritative. Event payloads use JSONB within relational tables; accounting uses NUMERIC, and Python calculations use Decimal. No JSON files hold trading state. One account row serializes Phase 0 paper transactions. This is intentionally a single-portfolio foundation, not a distributed executor.

The paper executor uses fixed adverse slippage and fees from supplied data. It has no blockchain/network calls, modeled gas is zero, latency is zero, and transaction timestamps are null. Realistic pool impact, congestion, and failed transaction simulation are not implemented.

Dashboard fixtures are presentation examples, not persisted accounting. Mode selection is a preview; global controls do not mutate policy. Native semantic controls are sufficient at this stage; shadcn/ui can be introduced when richer interaction is needed.

## Deliberately deferred

Phase 1: real read-only feed adapters, validated evidence, actual LLM agent runners, durable decision delivery, autonomous scheduling and lifecycle management, reconciliation jobs, persisted failed attempts, telemetry export, authenticated operational controls, and database-connected dashboard data.

A separate later phase must address signing isolation, RPC submission, transaction confirmation/replacement, recovery, and any live-money capability. Private keys, seed phrases, secrets, real credentials, and live transactions are absent from Phase 0.

See [files.md](files.md) for the complete authored file and directory inventory.

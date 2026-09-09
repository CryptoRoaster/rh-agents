# Phase 0 delivery

## Implemented

- Python 3.12+ FastAPI backend with strict Pydantic contracts for all fourteen requested domain objects, execution timing, safety metrics, controls, and typed asynchronous decisions.
- Eight planned LLM roles; SENTINEL and LEDGER are deterministic services, and EXECUTOR is infrastructure.
- Fail-closed deterministic risk policy, exact intent/snapshot approval binding, BUY/SELL paper simulation, and explicit fees/slippage/gas fields.
- SQLAlchemy 2 PostgreSQL tables, initial Alembic migration, one fictitious paper account, atomic accounting, persistent idempotency, and portfolio row locking.
- Weighted-average long-only spot positions and fee-aware realized/unrealized PnL.
- Next.js, strict TypeScript, Tailwind CSS, Recharts, responsive off-white dashboard, all requested sections, agent filters, and read-only policy placeholders.
- Root contributor/architecture/setup documentation, native external PostgreSQL setup for macOS and Linux, secret-free environment example, ignore rules, and dependency locks.

## Verification

Validated locally on Python 3.13.2, Node 22.22.2, Next.js 16.3.4, and PostgreSQL 16.14 on 2026-09-09.

| Check | Result |
| --- | --- |
| Backend pytest, PostgreSQL adapter | 68 passed; 95% statement coverage |
| Backend pytest, SQLite adapter | 66 passed; 2 PostgreSQL concurrency tests skipped |
| Ruff lint and formatting | Passed |
| Strict mypy | Passed, 23 source files |
| Alembic upgrade against isolated PostgreSQL | Passed |
| Alembic schema drift check | No new upgrade operations detected |
| Alembic offline PostgreSQL SQL generation | Passed |
| Frontend TypeScript | Passed |
| Frontend ESLint | Passed, no warnings |
| Frontend Prettier | Passed |
| Next.js production build | Passed; home and not-found pages prerendered |
| npm dependency installation audit | Zero reported vulnerabilities |
| Browser rendering | Desktop and 390px mobile layouts render; no console errors or framework error overlay; no page-level horizontal overflow |
| Browser interactions | Agent filters and mode previews verified using DOM/keyboard activation; live and pause controls disabled |

PostgreSQL concurrency tests verify that simultaneous identical intents produce one fill, and different concurrent intents cannot exceed the portfolio exposure limit. Persistence tests also cover replay after service restart, conflicting IDs, durable rejection, a latched pause, transactional rollback with correlated error logs, and missing portfolio marks.

The migration and database tests used an isolated native PostgreSQL 16.14 instance. The macOS setup guide provides a Homebrew PostgreSQL 17 example; that version specifically remains unverified. The Linux VPS target uses native PostgreSQL, Python backend, and Next.js frontend services, with systemd supervision deferred.

After the native-service setup correction on 2026-09-09, verification was rerun: PostgreSQL pytest (68 passed), SQLite pytest (66 passed, 2 skipped), Ruff lint/format, strict mypy, Alembic upgrade/schema drift/offline SQL, frontend typecheck/ESLint/Prettier, and the Next.js production build all passed. Locked dependency installation also succeeded; npm reported zero vulnerabilities. The disposable native PostgreSQL instance was stopped and removed afterward. Browser checks and coverage measurement in the table above are from the initial Phase 0 verification.

The browser CLI's semantic pointer clicks did not reliably activate off-screen controls. DOM activation and keyboard events verified the React behavior; a complete manual pointer-interaction/accessibility audit is not claimed.

## Decisions and limits

The final intent always passes SENTINEL again after FUSE/COMMANDER. No individual trade approval is introduced. Runtime risk policy remains trusted infrastructure, outside agent payloads. The API is read-only, defaults to OBSERVE, and rejects live configuration. The internal paper service requires explicit PAPER mode.

PostgreSQL is authoritative. Event payloads use JSONB within relational tables; accounting uses NUMERIC, and Python calculations use Decimal. No JSON files hold trading state. One account row serializes Phase 0 paper transactions. This is intentionally a single-portfolio foundation, not a distributed executor.

The paper executor uses fixed adverse slippage and fees from supplied data. It has no blockchain/network calls, modeled gas is zero, latency is zero, and transaction timestamps are null. Realistic pool impact, congestion, and failed transaction simulation are not implemented.

Dashboard fixtures are presentation examples, not persisted accounting. Mode selection is a preview; global controls do not mutate policy. Native semantic controls are sufficient at this stage; shadcn/ui can be introduced when richer interaction is needed.

## Deliberately deferred

Phase 1: real read-only feed adapters, validated evidence, actual LLM agent runners, durable decision delivery, autonomous scheduling and lifecycle management, reconciliation jobs, persisted failed attempts, runtime clocks, telemetry export, authenticated operational controls, and database-connected dashboard data.

A separate later phase must address signing isolation, RPC submission, transaction confirmation/replacement, recovery, and any live-money capability. Private keys, seed phrases, secrets, real credentials, and live transactions are absent from Phase 0.

No commit or push was performed. The current local branch is `main`.

See [files.md](files.md) for the complete authored file and directory inventory.

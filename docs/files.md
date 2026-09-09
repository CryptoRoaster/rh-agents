# Phase 0 file inventory

59 authored source, configuration, lock, and documentation files. `AGENTS.md` existed before this phase and was updated as requested; the other files are new.

```text
rh-agents/
├── backend/
│   ├── migrations/
│   │   ├── versions/
│   │   │   └── 0001_foundation.py
│   │   ├── env.py
│   │   └── script.py.mako
│   ├── src/
│   │   ├── agents/
│   │   │   ├── __init__.py
│   │   │   └── registry.py
│   │   ├── api/
│   │   │   ├── __init__.py
│   │   │   └── main.py
│   │   ├── core/
│   │   │   ├── __init__.py
│   │   │   ├── config.py
│   │   │   ├── models.py
│   │   │   └── numbers.py
│   │   ├── data/
│   │   │   ├── __init__.py
│   │   │   ├── database.py
│   │   │   ├── repository.py
│   │   │   └── tables.py
│   │   ├── execution/
│   │   │   ├── __init__.py
│   │   │   ├── base.py
│   │   │   └── paper.py
│   │   ├── ledger/
│   │   │   ├── __init__.py
│   │   │   └── accounting.py
│   │   ├── orchestration/
│   │   │   ├── __init__.py
│   │   │   ├── bus.py
│   │   │   └── paper.py
│   │   ├── risk/
│   │   │   ├── __init__.py
│   │   │   └── engine.py
│   │   └── __init__.py
│   ├── tests/
│   │   ├── __init__.py
│   │   ├── conftest.py
│   │   ├── test_contracts.py
│   │   ├── test_execution.py
│   │   ├── test_ledger.py
│   │   ├── test_persistence.py
│   │   └── test_risk.py
│   ├── alembic.ini
│   ├── pyproject.toml
│   └── uv.lock
├── docs/
│   ├── files.md
│   └── phase-0.md
├── frontend/
│   ├── app/
│   │   ├── globals.css
│   │   ├── layout.tsx
│   │   └── page.tsx
│   ├── components/
│   │   ├── dashboard.tsx
│   │   └── equity-chart.tsx
│   ├── lib/
│   │   ├── demo.ts
│   │   └── types.ts
│   ├── .prettierignore
│   ├── eslint.config.mjs
│   ├── next-env.d.ts
│   ├── next.config.ts
│   ├── package-lock.json
│   ├── package.json
│   ├── postcss.config.mjs
│   └── tsconfig.json
├── .env.example
├── .gitignore
├── AGENTS.md
├── ARCHITECTURE.md
├── README.md
└── docker-compose.yml
```

Generated local artifacts are ignored: `backend/.venv/`, Python bytecode, pytest/Ruff/mypy caches, `backend/.coverage`, `frontend/node_modules/`, `frontend/.next/`, and TypeScript build metadata. Temporary verification databases, browser captures, and download caches live outside the repository.

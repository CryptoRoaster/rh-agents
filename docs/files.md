# Phase 0 file inventory

62 authored source, configuration, lock, and documentation files, including Phase 0 hardening.

```text
rh-agents/
├── .github/
│   └── workflows/
│       └── ci.yml
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
│   │   │   ├── clock.py
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
│   │   ├── test_clock.py
│   │   ├── test_config.py
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
└── README.md
```

Generated local artifacts are ignored: `backend/.venv/`, Python bytecode, pytest/Ruff/mypy caches, `backend/.coverage`, `frontend/node_modules/`, `frontend/.next/`, and TypeScript build metadata. Temporary verification databases may use the ignored `.local/` directory and are removed after verification; browser captures and download caches live outside the repository.

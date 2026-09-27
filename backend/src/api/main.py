from fastapi import FastAPI, HTTPException
from sqlalchemy.exc import SQLAlchemyError

from src.agents.registry import COMPONENTS
from src.api.markets import router as markets_router
from src.api.paper import router as paper_router
from src.api.runtime import router as runtime_router
from src.api.scout import router as scout_router
from src.api.trade_cases import router as trade_cases_router
from src.api.workers import router as workers_router
from src.core.config import Settings
from src.core.models import RiskLimits
from src.data.database import connect
from src.data.schema import SchemaUnknown, expected_revision, is_current, recorded_revisions


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()
    app = FastAPI(title="rh-agents", version="0.5.0", description="Phase 2B · paper only")
    app.state.settings = settings
    app.include_router(markets_router)
    app.include_router(runtime_router)
    app.include_router(trade_cases_router)
    app.include_router(workers_router)
    # Read-only cockpit views. GET only; see tests/scout/test_api.py.
    app.include_router(scout_router)
    app.include_router(paper_router)

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok", "phase": "2B", "mode": settings.trading_mode.value}

    @app.get("/ready")
    async def ready() -> dict[str, str]:
        """Whether this database is the one this code was written against.

        The expected revision is the head of the migration chain that ships with
        the code, read from the migrations themselves. It used to be a constant,
        which was correct on the day it was typed and silently wrong at the next
        migration — a readiness check that passes because it compares against a
        revision nobody has shipped for months is worse than none, because it is
        believed. One read, and nothing is created or migrated on the way.

        The comparison is over the whole recorded set, through the same contract
        the preflight uses, so the two cannot disagree about what "current"
        means and neither can be satisfied by one convenient row.
        """
        engine, _ = connect(settings.database_url)
        try:
            expected = expected_revision()
            async with engine.connect() as connection:
                if not is_current(await recorded_revisions(connection), expected):
                    raise HTTPException(status_code=503, detail="Database migration is not current")
        except SchemaUnknown as error:
            raise HTTPException(status_code=503, detail="Expected migration unknown") from error
        except (SQLAlchemyError, OSError) as error:
            raise HTTPException(
                status_code=503, detail="Database unavailable or unmigrated"
            ) from error
        finally:
            await engine.dispose()
        return {"status": "ready"}

    @app.get("/api/system")
    async def system() -> dict[str, object]:
        return {
            "phase": "2B",
            "mode": settings.trading_mode,
            "live_enabled": False,
            "agents": [component.model_dump() for component in COMPONENTS],
            "risk_limits": RiskLimits().model_dump(mode="json"),
            "controls_writable": False,
        }

    return app


app = create_app()

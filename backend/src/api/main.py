from fastapi import FastAPI, HTTPException
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from src.agents.registry import COMPONENTS
from src.api.markets import router as markets_router
from src.api.runtime import router as runtime_router
from src.api.trade_cases import router as trade_cases_router
from src.api.workers import router as workers_router
from src.core.config import Settings
from src.core.models import RiskLimits
from src.data.database import connect


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()
    app = FastAPI(title="rh-agents", version="0.5.0", description="Phase 2B · paper only")
    app.state.settings = settings
    app.include_router(markets_router)
    app.include_router(runtime_router)
    app.include_router(trade_cases_router)
    app.include_router(workers_router)

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok", "phase": "2B", "mode": settings.trading_mode.value}

    @app.get("/ready")
    async def ready() -> dict[str, str]:
        engine, _ = connect(settings.database_url)
        try:
            async with engine.connect() as connection:
                revision = await connection.scalar(text("SELECT version_num FROM alembic_version"))
                if revision != "0006":
                    raise HTTPException(status_code=503, detail="Database migration is not current")
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

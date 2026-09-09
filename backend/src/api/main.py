from fastapi import FastAPI, HTTPException
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from src.agents.registry import COMPONENTS
from src.core.config import Settings
from src.core.models import RiskLimits
from src.data.database import connect


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()
    app = FastAPI(title="rh-agents", version="0.1.0", description="Phase 0 · paper only")

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok", "phase": "0", "mode": settings.trading_mode.value}

    @app.get("/ready")
    async def ready() -> dict[str, str]:
        engine, _ = connect(settings.database_url)
        try:
            async with engine.connect() as connection:
                revision = await connection.scalar(text("SELECT version_num FROM alembic_version"))
                if revision != "0001":
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
            "phase": 0,
            "mode": settings.trading_mode,
            "live_enabled": False,
            "agents": [component.model_dump() for component in COMPONENTS],
            "risk_limits": RiskLimits().model_dump(mode="json"),
            "controls_writable": False,
        }

    return app


app = create_app()

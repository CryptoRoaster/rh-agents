"""Safe runtime projections. Never serialize Settings or transport configuration."""

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.exc import SQLAlchemyError

from src.core.clock import SystemClock
from src.core.config import Settings
from src.data.database import connect
from src.runtime.store import RuntimeStore

router = APIRouter(prefix="/api/runtime")


async def runtime_store(request: Request) -> AsyncIterator[RuntimeStore]:
    settings: Settings = request.app.state.settings
    engine, sessions = connect(settings.database_url)
    try:
        yield RuntimeStore(sessions, SystemClock())
    except (SQLAlchemyError, OSError):
        raise HTTPException(503, "Runtime data unavailable") from None
    finally:
        await engine.dispose()


Store = Annotated[RuntimeStore, Depends(runtime_store)]


async def chain_health(settings: Settings, store: RuntimeStore) -> list[dict[str, Any]]:
    results = []
    for chain, prefix, expected in (("robinhood", "rh", 4663), ("bsc", "bsc", 56)):
        enabled = settings.evm_runtime_enabled and getattr(settings, f"{prefix}_chain_enabled")
        configured = bool(getattr(settings, f"{prefix}_rpc_http_url").get_secret_value()) and bool(
            getattr(settings, f"{prefix}_rpc_ws_url").get_secret_value()
        )
        base: dict[str, Any] = {
            "chain": chain,
            "network": "mainnet",
            "expected_chain_id": expected,
            "enabled": enabled,
            "configured": configured,
            "state": "STOPPED",
            "http_reachable": False,
            "wss_connected": False,
            "chain_id_verified": False,
            "latest_head": None,
            "latest_head_age_seconds": None,
            "last_processed_safe_block": None,
            "gap_detected": None,
            "error_category": None if configured else "NOT_CONFIGURED",
        }
        if enabled:
            row = await store.latest(chain, "HEALTH")
            if row:
                base.update(row.payload)
                timestamp = (
                    row.recorded_at.replace(tzinfo=UTC)
                    if row.recorded_at.tzinfo is None
                    else row.recorded_at
                )
                age = max(0, int((store.clock.now() - timestamp).total_seconds()))
                base["health_age_seconds"] = age
                message_at = row.payload.get("last_message_at")
                if isinstance(message_at, str):
                    base["latest_head_age_seconds"] = max(
                        0,
                        int(
                            (store.clock.now() - datetime.fromisoformat(message_at)).total_seconds()
                        ),
                    )
                if age > settings.evm_stale_seconds and base["state"] not in ("ERROR", "STOPPED"):
                    base["state"] = "STALE"
                    base["wss_connected"] = False
            else:
                base["state"] = "STARTING"
        results.append(base)
    return results


async def watcher_health(settings: Settings, store: RuntimeStore) -> dict[str, Any]:
    result: dict[str, Any] = {
        "enabled": settings.market_watcher_enabled,
        "provider": "geckoterminal",
        "state": "STOPPED",
        "last_run": None,
        "last_success": None,
    }
    if settings.market_watcher_enabled:
        row = await store.latest("market-watcher")
        success = await store.latest("market-watcher", "SUCCESS")
        if row:
            result.update(row.payload)
            result["last_run"] = row.recorded_at.isoformat()
            timestamp = (
                row.recorded_at.replace(tzinfo=UTC)
                if row.recorded_at.tzinfo is None
                else row.recorded_at
            )
            if (
                store.clock.now() - timestamp
            ).total_seconds() > settings.market_watch_interval_seconds * 2:
                result["state"] = "STALE"
        else:
            result["state"] = "STARTING"
        if success:
            result["last_success"] = success.recorded_at.isoformat()
    return result


@router.get("/chains")
async def chains(request: Request, store: Store) -> list[dict[str, Any]]:
    return await chain_health(request.app.state.settings, store)


@router.get("/market-watcher")
async def watcher(request: Request, store: Store) -> dict[str, Any]:
    return await watcher_health(request.app.state.settings, store)


@router.get("")
async def runtime(request: Request, store: Store) -> dict[str, Any]:
    return {
        "chains": await chain_health(request.app.state.settings, store),
        "market_watcher": await watcher_health(request.app.state.settings, store),
        "execution": "SIMULATED",
        "live_execution": "DISABLED",
    }

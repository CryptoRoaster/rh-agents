"""What migration 0011 does to history that already exists.

The rest of the suite runs the whole chain against empty tables, which proves
the statements are valid and nothing about the mapping. This applies the schema
as a deployment already has it — through `0010` — seeds one completed cycle the
way the services actually wrote it, and only then applies `0011`.

PostgreSQL only: the light suite builds its schema from the ORM metadata, so
there is no "before" state there to migrate.
"""

import importlib.util
import os
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

pytestmark = pytest.mark.skipif(
    not os.environ.get("TEST_DATABASE_URL"), reason="migrating an existing schema needs one"
)

THROUGH_0010 = (
    "0001_foundation",
    "0005_trade_case_workflow",
    "0006_worker_runtime",
    "0007_trade_case_risk_requests",
    "0008_trade_case_executions",
    "0009_position_market_identity",
    "0010_trade_case_exits",
)


def load(name):
    versions = Path(__file__).parents[2] / "migrations/versions"
    spec = importlib.util.spec_from_file_location(name, versions / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
async def legacy():
    """A schema at `0010`, in its own PostgreSQL schema."""
    url = os.environ["TEST_DATABASE_URL"]
    name = "reentry_migration_" + uuid4().hex
    admin = create_async_engine(url)
    async with admin.begin() as connection:
        await connection.execute(text(f'CREATE SCHEMA "{name}"'))
    engine = create_async_engine(url, connect_args={"server_settings": {"search_path": name}})
    modules = [load(item) for item in THROUGH_0010]

    def migrate(sync_connection):
        with Operations.context(MigrationContext.configure(sync_connection)):
            for item in modules:
                item.upgrade()

    try:
        async with engine.begin() as connection:
            await connection.run_sync(migrate)
        yield engine
    finally:
        await engine.dispose()
        async with admin.begin() as connection:
            await connection.execute(text(f'DROP SCHEMA "{name}" CASCADE'))
        await admin.dispose()


async def apply_0011(engine):
    module = load("0011_trade_cycles")

    def migrate(sync_connection):
        with Operations.context(MigrationContext.configure(sync_connection)):
            module.upgrade()

    async with engine.begin() as connection:
        await connection.run_sync(migrate)


ASSET = "robinhood:mainnet:0xaa"
PAIR = "robinhood:mainnet:contract_address:0xbb"


async def seed(engine, *, with_exit: bool, asset=ASSET, pair=PAIR, position: bool = True):
    """One case, entry, position and optionally an exit — as the services wrote them.

    `position` is off for a second seeded entry on the same asset: there is only
    ever one position row per asset, which is exactly the situation that makes a
    holding's origin ambiguous.
    """
    case_id, position_id, execution_id, exit_id = (uuid4() for _ in range(4))
    market = {
        "provider": "geckoterminal",
        "chain": "robinhood",
        "network": "mainnet",
        "pair_id": pair,
        "base_asset_id": asset,
        "quote_asset_id": "robinhood:mainnet:usdc",
        "venue": "uniswap-v3",
        "pool_locator": None,
        "is_fixture": False,
    }
    async with engine.begin() as connection:
        await connection.execute(
            text(
                "INSERT INTO trade_cases (id, workflow_version, market_key, chain, network,"
                " status, opened_at, updated_at, expires_at, originating_discovery_reference,"
                " strategy_policy_id, revision, reason_code, blockers, risk_input_digest,"
                " correlation_id, open_idempotency_key, open_fingerprint, market_payload)"
                " VALUES (:id, 'v1', :pair, 'robinhood', 'mainnet', 'EXECUTED', now(), now(),"
                " now() + interval '1 hour', :ref, NULL, 4, 'EXECUTED', '[]'::jsonb, NULL,"
                " :trace, :key, 'f' , CAST(:market AS jsonb))"
            ),
            {
                "id": case_id,
                "pair": pair,
                "ref": uuid4(),
                "trace": uuid4(),
                "key": f"legacy-{case_id}",
                "market": __import__("json").dumps(market),
            },
        )
        if position:
            await connection.execute(
                text(
                    "INSERT INTO positions (id, asset_id, market_pair_id, market_chain,"
                    " market_network, market_provider, quantity, cost_basis_usd,"
                    " realized_pnl_usd, created_at, updated_at, source, correlation_id)"
                    " VALUES (:id, :asset, :pair, 'robinhood', 'mainnet', 'geckoterminal',"
                    " :quantity, :basis, 0, now(), now(), 'LEDGER', :trace)"
                ),
                {
                    "id": position_id,
                    "asset": asset,
                    "pair": pair,
                    "quantity": Decimal("0") if with_exit else Decimal("4"),
                    "basis": Decimal("0") if with_exit else Decimal("100"),
                    "trace": uuid4(),
                },
            )
        await connection.execute(
            text(
                "INSERT INTO trade_case_risk_requests (request_id, trade_case_id, request_key,"
                " case_revision, risk_input_digest, risk_request_digest, intent_id,"
                " intent_fingerprint, requested_notional_usd, quantity, risk_decision_id,"
                " binding_id, outcome, risk_authorization, evaluated_at, recorded_at,"
                " correlation_id, basis)"
                " VALUES (:rid, :case_id, :key, 4, :digest, :digest, :intent, :digest, 500, 4,"
                " :decision, :binding, 'APPROVE', 'APPROVED', now(), now(), :trace,"
                " '{}'::jsonb)"
            ),
            {
                "rid": uuid4(),
                "case_id": case_id,
                "key": f"legacy-req-{case_id}",
                "digest": "0" * 64,
                "intent": uuid4(),
                "decision": uuid4(),
                "binding": uuid4(),
                "trace": uuid4(),
            },
        )
        request_id = (
            await connection.execute(
                text("SELECT request_id FROM trade_case_risk_requests WHERE trade_case_id = :id"),
                {"id": case_id},
            )
        ).scalar_one()
        await connection.execute(
            text(
                "INSERT INTO trade_case_executions (case_execution_id, trade_case_id, request_id,"
                " request_key, intent_id, order_id, execution_id, authorizing_binding_id,"
                " recheck_decision_id, risk_input_digest, quantity, execution_price_usd,"
                " notional_usd, fees_usd, filled_at, recorded_at, correlation_id, basis)"
                " VALUES (:id, :case_id, :request_id, :key, :intent, :order, :execution,"
                " :binding, :decision, :digest, 4, 25, 100, 1, now(), now(), :trace,"
                " '{}'::jsonb)"
            ),
            {
                "id": execution_id,
                "case_id": case_id,
                "request_id": request_id,
                "key": f"legacy-req-{case_id}",
                "intent": uuid4(),
                "order": uuid4(),
                "execution": uuid4(),
                "binding": uuid4(),
                "decision": uuid4(),
                "digest": "0" * 64,
                "trace": uuid4(),
            },
        )
        if with_exit:
            await connection.execute(
                text(
                    "INSERT INTO trade_case_exits (exit_id, trade_case_id, case_execution_id,"
                    " request_key, position_id, asset_id, market_pair_id, intent_id, order_id,"
                    " execution_id, risk_decision_id, quantity, execution_price_usd,"
                    " notional_usd, fees_usd, realized_pnl_usd, cost_basis_released_usd,"
                    " filled_at, recorded_at, correlation_id, basis)"
                    " VALUES (:id, :case_id, :entry, :key, :position, :asset, :pair, :intent,"
                    " :order, :execution, :decision, 4, 30, 120, 1, 19, 100, now(), now(),"
                    " :trace, '{}'::jsonb)"
                ),
                {
                    "id": exit_id,
                    "case_id": case_id,
                    "entry": execution_id,
                    "key": f"legacy-exit-{case_id}",
                    "position": position_id,
                    "asset": asset,
                    "pair": pair,
                    "intent": uuid4(),
                    "order": uuid4(),
                    "execution": uuid4(),
                    "decision": uuid4(),
                    "trace": uuid4(),
                },
            )
    return case_id, position_id, execution_id, exit_id


async def test_an_existing_completed_cycle_is_carried_over(legacy):
    """Entry, exit and holding all end up in the one cycle they were always in."""
    case_id, position_id, execution_id, exit_id = await seed(legacy, with_exit=True)

    await apply_0011(legacy)

    async with legacy.begin() as connection:
        cycle = (
            (
                await connection.execute(
                    text("SELECT * FROM trade_cycles WHERE trade_case_id = :id"), {"id": case_id}
                )
            )
            .mappings()
            .one()
        )
        entry_cycle = (
            await connection.execute(
                text("SELECT cycle_id FROM trade_case_executions WHERE case_execution_id = :id"),
                {"id": execution_id},
            )
        ).scalar_one()
        exit_cycle = (
            await connection.execute(
                text("SELECT cycle_id FROM trade_case_exits WHERE exit_id = :id"), {"id": exit_id}
            )
        ).scalar_one()
        position_cycle = (
            await connection.execute(
                text("SELECT cycle_id FROM positions WHERE id = :id"), {"id": position_id}
            )
        ).scalar_one()

    assert cycle["sequence"] == 1
    assert cycle["predecessor_exit_id"] is None
    assert cycle["request_key"] is None
    assert cycle["asset_id"] == ASSET
    assert cycle["market_pair_id"] == PAIR
    # A cycle is identified by the case that opened it, here and in new code.
    assert cycle["cycle_id"] == case_id
    assert entry_cycle == exit_cycle == position_cycle == cycle["cycle_id"]


async def test_an_open_holding_keeps_its_entry(legacy):
    """A position that was never sold is still attributed to the cycle that bought it."""
    case_id, position_id, _, _ = await seed(legacy, with_exit=False)

    await apply_0011(legacy)

    async with legacy.begin() as connection:
        found = (
            (
                await connection.execute(
                    text("SELECT cycle_id, quantity FROM positions WHERE id = :id"),
                    {"id": position_id},
                )
            )
            .mappings()
            .one()
        )
    assert found["cycle_id"] == case_id
    assert found["quantity"] == Decimal("4")


async def test_an_ambiguous_holding_is_left_unattributed(legacy):
    """Two entries could account for it, so the column stays empty rather than guessing.

    That is the same answer the exit path gives today: a holding it cannot place
    is refused, never assigned to whichever entry happens to match.
    """
    await seed(legacy, with_exit=True, pair=PAIR)
    # A second executed entry for the same asset in another pool. The position
    # row is shared, because there is only ever one row per asset.
    await seed(
        legacy, with_exit=False, pair="robinhood:mainnet:contract_address:0xcc", position=False
    )

    await apply_0011(legacy)

    async with legacy.begin() as connection:
        found = (
            (
                await connection.execute(
                    text("SELECT cycle_id FROM positions WHERE asset_id = :asset"), {"asset": ASSET}
                )
            )
            .scalars()
            .all()
        )
        made = (await connection.execute(text("SELECT COUNT(*) FROM trade_cycles"))).scalar_one()
    assert made == 2
    assert all(item is None for item in found)

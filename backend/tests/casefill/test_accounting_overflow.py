"""A known mark whose USD value the ledger cannot hold stops before execution.

The mark is not missing: it is recorded, exact and current. What cannot exist is
its accounting figure in `Numeric(38, 18)`. That is its own typed refusal, never
`PORTFOLIO_MARKS_UNAVAILABLE`, never a crash, and never a question to SENTINEL.
"""

from decimal import Decimal
from uuid import uuid4

from sqlalchemy import func, select

from src.data.tables import ExecutionRow, RiskRow, TradeCaseRiskRequestRow
from src.orchestration.casefill.models import ExecutionRefusal
from src.orchestration.riskrequest.models import RiskRequestRefusal
from tests.casefill.conftest import approved_case, build_fill_service
from tests.casefill.test_marks import SECOND, both_markets, fill_in
from tests.riskdata.conftest import recorded_snapshot
from tests.riskrequest.conftest import FRESH, build_service, ready_case

UNREPRESENTABLE = Decimal("1E+25")


def overprice_second(feed, now) -> None:
    feed.replace(
        recorded_snapshot(
            now,
            age=FRESH,
            metadata_age=FRESH,
            base_asset_id=SECOND.base_asset_id,
            pair_id=SECOND.pair_id,
            label="second",
            price=UNREPRESENTABLE,
        )
    )


async def count(sessions, table) -> int:
    async with sessions() as session:
        return int(await session.scalar(select(func.count()).select_from(table)) or 0)


async def test_a_risk_request_refuses_an_unrepresentable_portfolio(risk_db, now, trace):
    _, sessions = risk_db
    feed, _, _ = both_markets(now)
    await fill_in(sessions, now, trace, identity=SECOND, key="held", feed=feed)
    overprice_second(feed, now)
    decisions_before = await count(sessions, RiskRow)
    requests_before = await count(sessions, TradeCaseRiskRequestRow)

    risk = build_service(sessions, now, feed=feed)
    case = await ready_case(risk.cases, now, uuid4(), key="overflow")
    result = await risk.request_risk_evaluation(case.id, request_key="overflow-req")

    assert result.kind == "risk_request_refused"
    assert result.reason is RiskRequestRefusal.PORTFOLIO_ACCOUNTING_UNREPRESENTABLE
    assert result.detail == "EXPOSURE_OUTSIDE_ACCOUNTING_PRECISION"
    # SENTINEL was not asked: no decision and no stored request were written.
    assert await count(sessions, RiskRow) == decisions_before
    assert await count(sessions, TradeCaseRiskRequestRow) == requests_before


async def test_a_case_fill_refuses_an_unrepresentable_portfolio(risk_db, now, trace):
    _, sessions = risk_db
    feed, _, _ = both_markets(now)
    await fill_in(sessions, now, trace, identity=SECOND, key="held-fill", feed=feed)
    case, _, _ = await approved_case(sessions, now, uuid4(), key="ovf", feed=feed)
    overprice_second(feed, now)
    executions_before = await count(sessions, ExecutionRow)

    result = await build_fill_service(sessions, now, feed=feed).execute_case_fill(
        case.id, request_key="ovf-req"
    )

    assert result.kind == "execution_refused"
    assert result.reason is ExecutionRefusal.PORTFOLIO_ACCOUNTING_UNREPRESENTABLE
    assert result.detail == "EXPOSURE_OUTSIDE_ACCOUNTING_PRECISION"
    assert await count(sessions, ExecutionRow) == executions_before

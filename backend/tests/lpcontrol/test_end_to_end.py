"""Issue #92 over real state: the risk request refuses the 20-byte BUY by name.

The production risk request, unpatched -- the shared fixture market is a
uniswap-v3 pool, whose LP control nobody can read. Before issue #92 this exact
case was approved (`tests/riskrequest/test_integration.py`, which now runs on
fixture LP-control evidence). Needs TEST_DATABASE_URL.
"""

from sqlalchemy import select

from src.core.models import RiskOutcome
from src.data.tables import TradeCaseRiskRequestRow
from src.orchestration.workflow.models import TradeCaseStatus
from src.risk.authorization import RiskAuthorization
from tests.riskrequest.conftest import build_service, ready_case, risk_db  # noqa: F401


async def test_a_twenty_byte_case_is_rejected_on_lp_control(risk_db, now, trace):  # noqa: F811
    _, sessions = risk_db
    service = build_service(sessions, now)
    case = await ready_case(service.cases, now, trace)
    assert case.status is TradeCaseStatus.READY_FOR_RISK

    result = await service.request_risk_evaluation(case.id, request_key="issue-92-e2e")

    assert result.outcome is RiskOutcome.REJECT
    assert result.authorization is not RiskAuthorization.APPROVED
    assert "LP_CONTROL_UNSUPPORTED" in result.reason_codes
    after = await service.cases.get_trade_case(case.id)
    assert after.status is TradeCaseStatus.RISK_REJECTED
    async with sessions() as session:
        row = await session.scalar(select(TradeCaseRiskRequestRow))
    lp = row.basis["market_snapshot"]["lp_control"]
    assert lp["status"] == "UNSUPPORTED"
    assert lp["pool_locator_kind"] == "CONTRACT_ADDRESS"
    assert lp["detail"] == "LP_CONTROL_SOURCE_ABSENT"

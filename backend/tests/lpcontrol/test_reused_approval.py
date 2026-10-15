"""Issue #92: an approval already stored cannot carry a 20-byte BUY to a fill.

A risk request approved while its market's LP control read as established --
here: on fixture evidence, standing in for an approval recorded before this
issue -- is re-checked at the fill on a fresh market view. That view derives LP
control again, finds a 20-byte pool, and SENTINEL refuses. Nothing is filled
and nothing is booked. Needs TEST_DATABASE_URL.
"""

from sqlalchemy import func, select

from src.core.models import LP_CONTROL_V4_POOL_CENSUS, LpControlStatus, RiskOutcome
from src.data.tables import ExecutionRow, TradeCaseExecutionRow
from src.orchestration.riskrequest import service
from tests.casefill.conftest import approved_case, build_fill_service
from tests.conftest import FIXTURE_POOL_ID
from tests.riskrequest.conftest import risk_db  # noqa: F401


async def test_a_stored_approval_is_refused_at_the_fill(risk_db, now, trace, monkeypatch):  # noqa: F811
    _, sessions = risk_db
    with monkeypatch.context() as patched:
        patched.setattr(
            service,
            "lp_control_view",
            lambda *, snapshot, onchain: service.LpControlFinding(
                LpControlStatus.PASS,
                LP_CONTROL_V4_POOL_CENSUS,
                "BYTES32_POOL_ID",
                FIXTURE_POOL_ID,
                1_000_000,
            ),
        )
        case, approval, feed = await approved_case(sessions, now, trace)
    assert approval.outcome is RiskOutcome.APPROVE

    fill = build_fill_service(sessions, now, feed=feed)
    result = await fill.execute_case_fill(case.id, request_key="fill-req")

    assert result.kind != "paper_fill_recorded"
    async with sessions() as session:
        fills = await session.scalar(select(func.count()).select_from(ExecutionRow))
        bound = await session.scalar(select(func.count()).select_from(TradeCaseExecutionRow))
    assert (fills, bound) == (0, 0)
    assert "LP_CONTROL_UNSUPPORTED" in str(result.model_dump())

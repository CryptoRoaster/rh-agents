"""Issue #96: the PoolManager remainder, reconciled against every liability.

A traded V4 pool always leaves the PoolManager holding more token than its
positions' principal: swap fees owed to those positions, protocol fees, ERC-6909
claims and rounding the protocol keeps in its own favour. LP control asks who
can withdraw a pool's liquidity, so the remainder does not need a tolerance --
it needs every liability named:

* fees owed to a position follow that position's control;
* protocol fees are the protocol's;
* claims belong to accounts nobody here enumerated, so any is UNKNOWN;
* what the PoolManager holds beyond all of them is owed to nobody -- the
  PoolManager's own solvency invariant -- and no account can withdraw it.

Only a reconciliation that read every one of these at the census block lets a
remainder pass; anything missing, inconsistent or unreadable stays UNKNOWN.
Every scenario runs through the real ATLAS census and payload.
"""

from src.agents.atlas.handler import onchain_payload as atlas_payload
from src.agents.atlas.models import HolderCompleteness, HolderSourceRow
from src.agents.atlas.policy import evaluate_snapshot
from src.core.models import LpControlStatus, RiskOutcome
from src.orchestration.riskrequest.service import lp_control_view
from src.orchestration.workflow.models import OnchainPayload, PoolControlSummary
from tests.atlas.v4.chain import NATIVE, POOL_MANAGER, FakeV4Chain
from tests.atlas.v4.scenarios import UNIT, build, official_launch
from tests.lpcontrol.test_lp_control import recorded, sentinel

# Token-side fee growth: about 0.4 % of the launch position's liquidity per
# unit, so the owed amount is material and nowhere near rounding.
GROWTH = 1 << 120
CLAIMANT = "0x" + "c1" * 20


async def launch(now, configure=None) -> tuple[OnchainPayload, str, FakeV4Chain]:
    chain, pool, rows = official_launch()
    chain.extra_pool_balance = 0
    if configure is not None:
        configure(chain, pool)
    rows = (
        HolderSourceRow(address=POOL_MANAGER, balance_raw=chain.pool_balance(chain.head)),
        *rows[1:],
    )
    snapshot = await build(
        now,
        chain,
        rows=rows,
        completeness=HolderCompleteness.COMPLETE,
        supply=sum(row.balance_raw for row in rows),
        holder_count=len(rows),
    )
    return atlas_payload(snapshot, evaluate_snapshot(snapshot, now)), pool.id, chain


def census(payload: OnchainPayload) -> PoolControlSummary:
    assert payload.intelligence is not None and payload.intelligence.pool_control is not None
    return payload.intelligence.pool_control


def judged(now, intent, context, payload: OnchainPayload, pool_id: str):
    market, decision = sentinel(now, intent, context, payload, pool_id=pool_id)
    assert market.lp_control is not None
    return market.lp_control, decision


def fees(chain: FakeV4Chain, pool) -> None:
    chain.accrue(pool, GROWTH)


async def test_a_remainder_of_fees_owed_to_resolved_positions_passes(now, intent, context) -> None:
    payload, pool_id, chain = await launch(now, fees)
    owed = sum(chain.owed(chain.head).values())
    assert owed > UNIT
    lp, decision = judged(now, intent, context, payload, pool_id)
    assert lp.status is LpControlStatus.PASS, lp.detail
    assert decision.outcome is RiskOutcome.APPROVE, decision.reason_codes
    control = census(payload)
    assert control.unattributed_raw == str(owed)
    assert control.reconciliation_status == "COMPLETE"
    assert control.uncollected_fees_raw == str(owed)
    assert (control.protocol_fees_raw, control.claims_raw) == ("0", "0")
    assert control.unowned_surplus_raw == "0"


async def test_protocol_fees_are_the_protocols_and_pass(now, intent, context) -> None:
    def configure(chain: FakeV4Chain, pool) -> None:
        fees(chain, pool)
        chain.protocol_fees = 7 * UNIT

    payload, pool_id, _ = await launch(now, configure)
    lp, _ = judged(now, intent, context, payload, pool_id)
    assert lp.status is LpControlStatus.PASS, lp.detail
    assert census(payload).protocol_fees_raw == str(7 * UNIT)


async def test_a_balance_beyond_every_liability_is_owed_to_nobody(now, intent, context) -> None:
    # Rounding dust and stray transfers: no account holds a claim on them.
    def configure(chain: FakeV4Chain, pool) -> None:
        fees(chain, pool)
        chain.extra_pool_balance = 13

    payload, pool_id, _ = await launch(now, configure)
    lp, _ = judged(now, intent, context, payload, pool_id)
    assert lp.status is LpControlStatus.PASS, lp.detail
    assert census(payload).unowned_surplus_raw == "13"


async def test_outstanding_claims_are_unknown(now, intent, context) -> None:
    def configure(chain: FakeV4Chain, pool) -> None:
        fees(chain, pool)
        chain.claim_transfers.append((NATIVE, CLAIMANT, 5 * UNIT, pool.created_block + 3))

    payload, pool_id, _ = await launch(now, configure)
    lp, decision = judged(now, intent, context, payload, pool_id)
    assert (lp.status, lp.detail) == (LpControlStatus.UNKNOWN, "V4_POOL_CLAIMS_UNATTRIBUTED")
    assert decision.outcome is RiskOutcome.REJECT
    assert census(payload).claims_raw == str(5 * UNIT)


async def test_claims_minted_and_burned_again_leave_nothing(now, intent, context) -> None:
    def configure(chain: FakeV4Chain, pool) -> None:
        fees(chain, pool)
        chain.claim_transfers += [
            (NATIVE, CLAIMANT, 5 * UNIT, pool.created_block + 3),
            (CLAIMANT, NATIVE, 5 * UNIT, pool.created_block + 4),
        ]

    payload, pool_id, _ = await launch(now, configure)
    lp, _ = judged(now, intent, context, payload, pool_id)
    assert lp.status is LpControlStatus.PASS, lp.detail
    assert census(payload).claims_raw == "0"


async def test_a_balance_below_the_liabilities_is_unknown(now, intent, context) -> None:
    # The PoolManager cannot owe more than it holds; a census that says so
    # missed or misread something.
    def configure(chain: FakeV4Chain, pool) -> None:
        fees(chain, pool)
        chain.extra_pool_balance = -1

    payload, pool_id, _ = await launch(now, configure)
    control = census(payload)
    assert control.reconciliation_status == "INCOMPLETE"
    assert control.reconciliation_gap == "V4_POOL_BALANCE_DEFICIT"
    lp, decision = judged(now, intent, context, payload, pool_id)
    assert (lp.status, lp.detail) == (LpControlStatus.UNKNOWN, "V4_POOL_BALANCE_UNATTRIBUTED")
    assert decision.outcome is RiskOutcome.REJECT


async def test_an_unreadable_liability_leaves_the_remainder_unknown(now, intent, context) -> None:
    def configure(chain: FakeV4Chain, pool) -> None:
        fees(chain, pool)
        chain.protocol_fees = None

    payload, pool_id, _ = await launch(now, configure)
    control = census(payload)
    # The census itself, and with it the economic figure, is unaffected.
    assert control.status == "AVAILABLE"
    assert control.reconciliation_status == "INCOMPLETE"
    lp, decision = judged(now, intent, context, payload, pool_id)
    assert (lp.status, lp.detail) == (LpControlStatus.UNKNOWN, "V4_POOL_BALANCE_UNATTRIBUTED")
    assert decision.outcome is RiskOutcome.REJECT


def with_census(payload: OnchainPayload, **changes: object) -> OnchainPayload:
    assert payload.intelligence is not None
    control = census(payload).model_copy(update=changes)
    intelligence = payload.intelligence.model_copy(update={"pool_control": control})
    return payload.model_copy(update={"intelligence": intelligence})


async def test_every_other_gap_in_the_reconciliation_is_unknown(now) -> None:
    payload, pool_id, _ = await launch(now, fees)
    snapshot = recorded(now, pool_id=pool_id)
    assert lp_control_view(snapshot=snapshot, onchain=payload)[0] is LpControlStatus.PASS
    control = census(payload)
    assert control.basis == "UPPER_BOUND" and control.basis_excluding_remainder == "EXACT"
    off_by_one = str(int(control.uncollected_fees_raw or "0") + 1)
    cases = {
        # Evidence recorded before the reconciliation existed.
        "legacy": {
            key: None
            for key in (
                "reconciliation_status",
                "uncollected_fees_raw",
                "protocol_fees_raw",
                "claims_raw",
                "unowned_surplus_raw",
            )
        },
        "incomplete": {"reconciliation_status": "INCOMPLETE"},
        "claims_unread": {"claims_raw": None},
        "fees_unread": {"uncollected_fees_raw": None},
        "surplus_absent": {"unowned_surplus_raw": None},
        # The reconciliation and the economic split disagree by one unit.
        "inconsistent": {"uncollected_fees_raw": off_by_one},
        # Bounded for a reason the reconciliation does not touch.
        "bounded_holdings": {"basis_excluding_remainder": "UPPER_BOUND"},
        "basis_unrecorded": {"basis_excluding_remainder": None},
    }
    for label, changes in cases.items():
        status, detail = lp_control_view(
            snapshot=snapshot, onchain=with_census(payload, **changes)
        )[:2]
        assert status is LpControlStatus.UNKNOWN, label
        assert detail in {
            "V4_POOL_BALANCE_UNATTRIBUTED",
            "V4_POOL_CLAIMS_UNATTRIBUTED",
            "V4_CONCENTRATION_NOT_EXACT",
        }, (label, detail)


async def test_fees_owed_to_an_unresolved_position_do_not_pass(now, intent, context) -> None:
    # The fees follow the position: a timelocked launch NFT is unresolved.
    payload, pool_id, _ = await launch(now, fees)
    unresolved = with_census(payload, position_control_states={"TIMELOCKED": 1})
    lp, decision = judged(now, intent, context, unresolved, pool_id)
    assert (lp.status, lp.detail) == (LpControlStatus.UNKNOWN, "V4_POSITION_CONTROL_UNRESOLVED")
    assert decision.outcome is RiskOutcome.REJECT

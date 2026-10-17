"""Issue #96: the PoolManager remainder, reconciled against every liability.

A traded V4 pool always leaves the PoolManager holding more token than its
positions' principal: swap fees owed to those positions, protocol fees, ERC-6909
claims, and rounding the protocol keeps in its own favour. The census now reads
each of these at the census block, so a remainder is no longer one opaque
number -- but none of its parts lets a purchase pass yet, and each says why:

* claims are held by accounts nobody here enumerated;
* a surplus beyond every liability of the positions found is not provably
  nobody's: v4-core lets a pool be initialized for a token address before the
  token exists, and the census finds pools from the token's creation block, so
  liquidity in such a pool would sit exactly there (review finding B1);
* fees owed and protocol fees are exact, but the economic concentration still
  ranks them as one unattributed holder, not with whoever can collect them.

Anything missing, inconsistent or unreadable stays V4_POOL_BALANCE_UNATTRIBUTED.
Every scenario runs through the real ATLAS census and payload.
"""

from src.agents.atlas.handler import onchain_payload as atlas_payload
from src.agents.atlas.models import HolderCompleteness, HolderSourceRow
from src.agents.atlas.policy import evaluate_snapshot
from src.core.models import LpControlStatus, RiskOutcome
from src.orchestration.riskrequest.service import lp_control_view
from src.orchestration.workflow.models import OnchainPayload, PoolControlSummary
from tests.atlas.conftest import TOKEN
from tests.atlas.v4.chain import NATIVE, POOL_MANAGER, FakeV4Chain, Pool
from tests.atlas.v4.scenarios import HIDDEN_RANGE, UNIT, build, official_launch
from tests.lpcontrol.test_lp_control import recorded, sentinel

# Token-side fee growth large enough that the owed amount is material.
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


def refused(now, intent, context, payload: OnchainPayload, pool_id: str, detail: str) -> None:
    market, decision = sentinel(now, intent, context, payload, pool_id=pool_id)
    assert market.lp_control is not None
    assert (market.lp_control.status, market.lp_control.detail) == (
        LpControlStatus.UNKNOWN,
        detail,
    )
    assert decision.outcome is RiskOutcome.REJECT
    assert "LP_CONTROL_UNKNOWN" in decision.reason_codes


def fees(chain: FakeV4Chain, pool) -> None:
    chain.accrue(pool, GROWTH)


async def test_fees_owed_are_read_exactly_and_still_not_credited(now, intent, context) -> None:
    payload, pool_id, chain = await launch(now, fees)
    owed = sum(chain.owed(chain.head).values())
    assert owed > UNIT
    control = census(payload)
    assert control.unattributed_raw == str(owed)
    assert control.reconciliation_status == "COMPLETE"
    assert control.uncollected_fees_raw == str(owed)
    assert (control.protocol_fees_raw, control.claims_raw, control.surplus_raw) == ("0", "0", "0")
    refused(now, intent, context, payload, pool_id, "V4_POOL_FEES_NOT_CREDITED")


async def test_protocol_fees_are_read_exactly_and_still_not_credited(now, intent, context) -> None:
    def configure(chain: FakeV4Chain, pool) -> None:
        fees(chain, pool)
        chain.protocol_fees = 7 * UNIT

    payload, pool_id, _ = await launch(now, configure)
    assert census(payload).protocol_fees_raw == str(7 * UNIT)
    refused(now, intent, context, payload, pool_id, "V4_POOL_FEES_NOT_CREDITED")


async def test_a_surplus_beyond_every_liability_found_is_unexplained(now, intent, context) -> None:
    def configure(chain: FakeV4Chain, pool) -> None:
        fees(chain, pool)
        chain.extra_pool_balance = 13

    payload, pool_id, _ = await launch(now, configure)
    assert census(payload).surplus_raw == "13"
    refused(now, intent, context, payload, pool_id, "V4_POOL_SURPLUS_UNEXPLAINED")


async def test_a_pool_initialized_before_the_token_cannot_hide_in_the_surplus(
    now, intent, context
) -> None:
    # Review finding B1. v4-core initializes a pool for a currency address
    # without checking that code exists there, and a token's address is known
    # before it is deployed. The census looks for pools from the token's
    # creation block, so liquidity added later to a pool initialized earlier
    # sits in the PoolManager with no position the census knows of.
    hidden_owner = "0x" + "ad" * 20

    def configure(chain: FakeV4Chain, pool) -> None:
        fees(chain, pool)
        early = chain.add_pool(
            Pool(
                currency0=NATIVE,
                currency1=TOKEN,
                fee=500,
                tick_spacing=10,
                created_block=chain.created_block - 1_000,
                tick=300_000,
            )
        )
        chain.mint(
            early, hidden_owner, 9_999_999, *HIDDEN_RANGE, 10**18, block=chain.created_block + 50
        )

    payload, pool_id, chain = await launch(now, configure)
    control = census(payload)
    assert len(control.pools) == 1  # the early pool was never found
    assert control.reconciliation_status == "COMPLETE"
    assert int(control.surplus_raw or "0") > 0
    refused(now, intent, context, payload, pool_id, "V4_POOL_SURPLUS_UNEXPLAINED")


async def test_outstanding_claims_are_unknown(now, intent, context) -> None:
    def configure(chain: FakeV4Chain, pool) -> None:
        fees(chain, pool)
        chain.claim_transfers.append((NATIVE, CLAIMANT, 5 * UNIT, pool.created_block + 3))

    payload, pool_id, _ = await launch(now, configure)
    assert census(payload).claims_raw == str(5 * UNIT)
    refused(now, intent, context, payload, pool_id, "V4_POOL_CLAIMS_UNATTRIBUTED")


async def test_claims_minted_and_burned_again_leave_nothing(now, intent, context) -> None:
    def configure(chain: FakeV4Chain, pool) -> None:
        fees(chain, pool)
        chain.claim_transfers += [
            (NATIVE, CLAIMANT, 5 * UNIT, pool.created_block + 3),
            (CLAIMANT, NATIVE, 5 * UNIT, pool.created_block + 4),
        ]

    payload, pool_id, _ = await launch(now, configure)
    assert census(payload).claims_raw == "0"
    refused(now, intent, context, payload, pool_id, "V4_POOL_FEES_NOT_CREDITED")


async def test_a_balance_below_the_liabilities_is_unattributed(now, intent, context) -> None:
    # The PoolManager cannot owe more than it holds; a census that says so
    # missed or misread something.
    def configure(chain: FakeV4Chain, pool) -> None:
        fees(chain, pool)
        chain.extra_pool_balance = -1

    payload, pool_id, _ = await launch(now, configure)
    control = census(payload)
    assert control.reconciliation_status == "INCOMPLETE"
    assert control.reconciliation_gap == "V4_POOL_BALANCE_DEFICIT"
    refused(now, intent, context, payload, pool_id, "V4_POOL_BALANCE_UNATTRIBUTED")


async def test_an_unreadable_liability_leaves_the_remainder_unattributed(
    now, intent, context
) -> None:
    def configure(chain: FakeV4Chain, pool) -> None:
        fees(chain, pool)
        chain.protocol_fees = None

    payload, pool_id, _ = await launch(now, configure)
    control = census(payload)
    # The census itself, and with it the economic figure, is unaffected.
    assert control.status == "AVAILABLE"
    assert control.reconciliation_status == "INCOMPLETE"
    refused(now, intent, context, payload, pool_id, "V4_POOL_BALANCE_UNATTRIBUTED")


def with_census(payload: OnchainPayload, **changes: object) -> OnchainPayload:
    assert payload.intelligence is not None
    control = census(payload).model_copy(update=changes)
    intelligence = payload.intelligence.model_copy(update={"pool_control": control})
    return payload.model_copy(update={"intelligence": intelligence})


async def test_no_remainder_ever_passes(now) -> None:
    payload, pool_id, _ = await launch(now, fees)
    snapshot = recorded(now, pool_id=pool_id)
    control = census(payload)
    off_by_one = str(int(control.uncollected_fees_raw or "0") + 1)
    cases = {
        "reconciled": ({}, "V4_POOL_FEES_NOT_CREDITED"),
        # Evidence recorded before the reconciliation existed.
        "legacy": (
            {
                key: None
                for key in (
                    "reconciliation_status",
                    "uncollected_fees_raw",
                    "protocol_fees_raw",
                    "claims_raw",
                    "surplus_raw",
                )
            },
            "V4_POOL_BALANCE_UNATTRIBUTED",
        ),
        "incomplete": ({"reconciliation_status": "INCOMPLETE"}, "V4_POOL_BALANCE_UNATTRIBUTED"),
        "claims_unread": ({"claims_raw": None}, "V4_POOL_BALANCE_UNATTRIBUTED"),
        "fees_unread": ({"uncollected_fees_raw": None}, "V4_POOL_BALANCE_UNATTRIBUTED"),
        "surplus_absent": ({"surplus_raw": None}, "V4_POOL_BALANCE_UNATTRIBUTED"),
        # The reconciliation and the economic split disagree by one unit.
        "inconsistent": ({"uncollected_fees_raw": off_by_one}, "V4_POOL_BALANCE_UNATTRIBUTED"),
    }
    for label, (changes, detail) in cases.items():
        found = lp_control_view(snapshot=snapshot, onchain=with_census(payload, **changes))
        assert found[:2] == (LpControlStatus.UNKNOWN, detail), label


async def test_without_any_remainder_the_launch_still_passes(now, intent, context) -> None:
    # The positive counterpart: nothing beyond principal, nothing to explain.
    payload, pool_id, _ = await launch(now)
    control = census(payload)
    assert control.unattributed_raw == "0"
    assert control.reconciliation_status == "COMPLETE"
    market, decision = sentinel(now, intent, context, payload, pool_id=pool_id)
    assert market.lp_control is not None
    assert market.lp_control.status is LpControlStatus.PASS, market.lp_control.detail
    assert decision.outcome is RiskOutcome.APPROVE, decision.reason_codes

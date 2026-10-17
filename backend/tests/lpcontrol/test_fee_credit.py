"""Issue #103: every part of the PoolManager remainder credited to who can take it.

Once the census has read every liability (#96) over a discovery that covers the
PoolManager's history (#94), the remainder can be judged exactly:

* fees owed to a position go to whoever can collect them now -- the account
  holding its NFT, a released timelock's operator, an account's own direct
  position; a FeeSplitter's fees go to recipients this code does not read, so
  they stay uncredited;
* protocol fees are the protocol's, one row of their own;
* ERC-6909 claims go to the accounts the claim transfers name;
* what lies beyond every liability is owed to nobody.

Only a remainder credited without any rest lets LP control pass, and the
economic figure then counts each credit with its holder -- never twice, never
as one pseudo-holder. Real ATLAS pipeline throughout.
"""

from src.agents.atlas.handler import onchain_payload as atlas_payload
from src.agents.atlas.models import HolderCompleteness, HolderSourceRow
from src.agents.atlas.policy import evaluate_snapshot
from src.agents.atlas.v4.pool_index import PoolIndexHistory, V4PoolIndexer
from src.core.models import LpControlStatus
from src.orchestration.riskrequest.service import lp_control_view
from src.orchestration.workflow.models import PoolControlSummary
from tests.atlas.conftest import TOKEN
from tests.atlas.v4.chain import HEAD_BLOCK, NATIVE, POOL_MANAGER, FakeV4Chain, Pool
from tests.atlas.v4.index import MemoryPoolIndexStore
from tests.atlas.v4.scenarios import UNIT, build, deployment, official_launch
from tests.lpcontrol.test_lp_control import recorded

TRADER = "0x" + "3f" * 20
CLAIMANT = "0x" + "c1" * 20
GROWTH = 1 << 120


def launch(configure=None) -> tuple[FakeV4Chain, Pool]:
    chain, pool, _ = official_launch()
    chain.extra_pool_balance = 0
    chain.manager_deployed_block = HEAD_BLOCK - 350_000
    if configure is not None:
        configure(chain, pool)
    return chain, pool


def side_pool(chain: FakeV4Chain, pool: Pool) -> Pool:
    """Another pool of the token with an account's position in range, earning fees."""
    other = chain.add_pool(
        Pool(currency0=NATIVE, currency1=TOKEN, fee=10_000, tick_spacing=200, tick=pool.tick)
    )
    lower = pool.tick - 2_000
    lower -= lower % other.tick_spacing
    chain.mint(other, TRADER, 78, lower, lower + 40 * other.tick_spacing, 10**24)
    return other


async def judged(now, chain: FakeV4Chain, pool: Pool, *, indexed: bool = True):
    store = MemoryPoolIndexStore()
    if indexed:
        result = await V4PoolIndexer(
            reads=chain, store=store, deployment=deployment(), confirmations=20, max_requests=200
        ).advance()
        assert result.status == "COMPLETE"
    _, _, launch_rows = official_launch()
    rows = (
        HolderSourceRow(address=POOL_MANAGER, balance_raw=chain.pool_balance(chain.head)),
        *launch_rows[1:],
    )
    snapshot = await build(
        now,
        chain,
        rows=rows,
        completeness=HolderCompleteness.COMPLETE,
        supply=sum(row.balance_raw for row in rows),
        holder_count=len(rows),
        index=PoolIndexHistory(store) if indexed else None,
    )
    payload = atlas_payload(snapshot, evaluate_snapshot(snapshot, now))
    assert payload.intelligence is not None and payload.intelligence.pool_control is not None
    control: PoolControlSummary = payload.intelligence.pool_control
    return control, lp_control_view(snapshot=recorded(now, pool_id=pool.id), onchain=payload)


def holder(control: PoolControlSummary, address: str) -> int | None:
    return next(
        (int(item.balance_raw) for item in control.economic_top_holders if item.holder == address),
        None,
    )


async def test_fees_owed_to_an_account_are_that_accounts_and_pass(now) -> None:
    pools: list[Pool] = []

    def configure(chain: FakeV4Chain, pool: Pool) -> None:
        pools.append(side_pool(chain, pool))
        chain.accrue(pools[0], GROWTH)

    chain, pool = launch(configure)
    control, found = await judged(now, chain, pool)
    owed = chain.owed(chain.head)
    assert sum(owed.values()) > 0 and control.uncollected_fees_raw == str(sum(owed.values()))
    assert found[0] is LpControlStatus.PASS, found.detail
    assert control.remainder_credit_status == "COMPLETE"
    assert control.basis == "EXACT"
    # The account's row is its principal plus its own fees, each counted once.
    (key,) = [key for key in owed if key[0] == pools[0].id]
    principal = chain.controlled(chain.head)[key]
    assert holder(control, TRADER) == principal + owed[key]


async def test_fees_owed_to_a_fee_splitter_stay_uncredited(now) -> None:
    def configure(chain: FakeV4Chain, pool: Pool) -> None:
        chain.accrue(pool, GROWTH)

    chain, pool = launch(configure)
    control, found = await judged(now, chain, pool)
    assert control.remainder_credit_status == "INCOMPLETE"
    assert found[:2] == (LpControlStatus.UNKNOWN, "V4_POOL_FEES_NOT_CREDITED")


async def test_protocol_fees_are_the_protocols_row_and_pass(now) -> None:
    def configure(chain: FakeV4Chain, pool: Pool) -> None:
        chain.protocol_fees = 7 * UNIT

    chain, pool = launch(configure)
    control, found = await judged(now, chain, pool)
    assert found[0] is LpControlStatus.PASS, found.detail
    # Credited to the protocol's own row (too small for the top ten here),
    # and no longer one unattributed holder.
    assert control.remainder_credited_raw == str(7 * UNIT)
    assert holder(control, "UNATTRIBUTED_POOL_BALANCE") is None


async def test_claims_are_their_holders_and_pass(now) -> None:
    def configure(chain: FakeV4Chain, pool: Pool) -> None:
        chain.claim_transfers += [
            (NATIVE, CLAIMANT, 5 * UNIT, pool.created_block + 3),
            (CLAIMANT, TRADER, 2 * UNIT, pool.created_block + 4),
        ]

    chain, pool = launch(configure)
    control, found = await judged(now, chain, pool)
    assert found[0] is LpControlStatus.PASS, found.detail
    assert control.claims_raw == str(5 * UNIT)
    assert control.remainder_credited_raw == str(5 * UNIT)
    assert holder(control, "UNATTRIBUTED_POOL_BALANCE") is None


async def test_a_surplus_over_the_managers_history_is_nobodys_and_passes(now) -> None:
    def configure(chain: FakeV4Chain, pool: Pool) -> None:
        chain.extra_pool_balance = 13

    chain, pool = launch(configure)
    control, found = await judged(now, chain, pool)
    assert control.surplus_raw == "13"
    assert found[0] is LpControlStatus.PASS, found.detail
    assert holder(control, "UNATTRIBUTED_POOL_BALANCE") is None


async def test_without_the_managers_history_a_surplus_stays_unexplained(now) -> None:
    def configure(chain: FakeV4Chain, pool: Pool) -> None:
        chain.extra_pool_balance = 13

    chain, pool = launch(configure)
    control, found = await judged(now, chain, pool, indexed=False)
    assert control.remainder_credit_status == "INCOMPLETE"
    assert found[:2] == (LpControlStatus.UNKNOWN, "V4_POOL_SURPLUS_UNEXPLAINED")


async def test_more_claim_holders_than_are_named_stay_uncredited(now) -> None:
    from src.agents.atlas.v4.models import MAX_CLAIM_HOLDERS

    def configure(chain: FakeV4Chain, pool: Pool) -> None:
        chain.claim_transfers += [
            (NATIVE, "0x" + f"{index + 1:040x}", UNIT, pool.created_block + 3)
            for index in range(MAX_CLAIM_HOLDERS + 1)
        ]

    chain, pool = launch(configure)
    control, found = await judged(now, chain, pool)
    assert control.remainder_credit_status == "INCOMPLETE"
    assert found[:2] == (LpControlStatus.UNKNOWN, "V4_POOL_CLAIMS_UNATTRIBUTED")


async def test_a_collector_outside_an_incomplete_holder_list_is_not_exact(now) -> None:
    # The account earning fees might hold more than the retained rows show.
    def configure(chain: FakeV4Chain, pool: Pool) -> None:
        chain.accrue(side_pool(chain, pool), GROWTH)

    chain, pool = launch(configure)
    store = MemoryPoolIndexStore()
    await V4PoolIndexer(
        reads=chain, store=store, deployment=deployment(), confirmations=20, max_requests=200
    ).advance()
    _, _, launch_rows = official_launch()
    rows = (
        HolderSourceRow(address=POOL_MANAGER, balance_raw=chain.pool_balance(chain.head)),
        *launch_rows[1:],
    )
    snapshot = await build(
        now,
        chain,
        rows=rows,
        completeness=HolderCompleteness.TOP_N_ONLY,
        supply=sum(row.balance_raw for row in rows) + 10**24,
        holder_count=len(rows) + 50,
        index=PoolIndexHistory(store),
    )
    payload = atlas_payload(snapshot, evaluate_snapshot(snapshot, now))
    found = lp_control_view(snapshot=recorded(now, pool_id=pool.id), onchain=payload)
    assert found[0] is LpControlStatus.UNKNOWN


async def test_a_complete_credit_that_does_not_add_up_is_not_trusted(now) -> None:
    # Review N4: the decision re-checks the credit's own figures.
    def configure(chain: FakeV4Chain, pool: Pool) -> None:
        chain.accrue(side_pool(chain, pool), GROWTH)
        chain.extra_pool_balance = 13

    chain, pool = launch(configure)
    control, found = await judged(now, chain, pool)
    assert found[0] is LpControlStatus.PASS, found.detail
    from src.orchestration.riskrequest.service import unresolved_custody

    credited = int(control.remainder_credited_raw or "0")
    cases = {
        "uncredited": {"remainder_uncredited_raw": "1"},
        "uncredited_absent": {"remainder_uncredited_raw": None},
        "short": {"remainder_credited_raw": str(credited - 1)},
        "unowned_beyond_surplus": {"remainder_unowned_raw": "14"},
        "unowned_without_history": {"pool_discovery_basis": "TOKEN_CREATION"},
    }
    for label, changes in cases.items():
        assert unresolved_custody(control.model_copy(update=changes)) is not None, label

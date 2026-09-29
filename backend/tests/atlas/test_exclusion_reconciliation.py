"""Reconciling Blockscout's server-side zero-address exclusion, without weakening risk.

Blockscout declares that it removes the zero address from its holder list. That
exclusion stays declared. What is new is reading the excluded holding back:
ERC-20 `balanceOf(0x0)` over our own RPC at exactly the provider's snapshot
block. A successful read puts the balance into the rows the concentration is
computed from; everything else — no block, a failed read, any other address,
legacy evidence — leaves the exclusion unresolved, and risk readiness still
calls the metric understated.
"""

from decimal import Decimal
from uuid import uuid4

import pytest

from src.agents.atlas.context import AtlasSnapshotBuilder
from src.agents.atlas.handler import holder_distribution
from src.agents.atlas.models import (
    ZERO_ADDRESS,
    HolderCompleteness,
    HolderObservationBasis,
)
from src.core.clock import FixedClock
from src.markets.models import Availability
from src.orchestration.riskdata.models import RiskDataGapCode, RiskFactKind
from src.orchestration.workflow.models import HolderDistributionFacts
from tests.atlas.conftest import (
    TOKEN,
    TOTAL_SUPPLY,
    StubHolders,
    StubOrigins,
    chain_snapshot,
    contract_facts,
    holder_source_result,
    market_identity,
    origin_facts,
)
from tests.riskdata.conftest import build_reader, holder_block, onchain_payload, prepare_case
from tests.riskdata.test_readiness import gap

SNAPSHOT_BLOCK = 999_990
OTHER = "0x" + "ab" * 20


class Contracts:
    """Chain and contract facts, plus a `balanceOf` that records every read."""

    def __init__(self, now, balance=0) -> None:
        self.now = now
        self.balance = balance
        self.reads: list[tuple[str, str, int]] = []

    async def chain_snapshot(self):
        return chain_snapshot(self.now, block=1_000_000)

    async def contract_facts(self, token_address, block):
        return contract_facts(block=block)

    async def balance_of(self, token_address, holder, block):
        self.reads.append((token_address, holder, block))
        if isinstance(self.balance, Exception):
            raise self.balance
        return self.balance


def provider(now, *, excluded=(ZERO_ADDRESS,), **overrides):
    """Blockscout's shape: block-anchored rows with the zero address excluded."""
    result = holder_source_result(now, snapshot_block=SNAPSHOT_BLOCK, **overrides)
    return result.model_copy(update={"excluded_addresses": excluded})


async def measured(now, contracts, source):
    builder = AtlasSnapshotBuilder(
        contracts=contracts,
        holders=StubHolders(source),
        origins=StubOrigins(origin_facts()),
        clock=FixedClock(now),
    )
    snapshot = await builder.build(uuid4(), uuid4(), market_identity())
    return snapshot.holders


# ------------------------------------------------------- reconciled


async def test_a_zero_balance_reconciles_the_exclusion(now):
    contracts = Contracts(now, balance=0)
    holders = await measured(now, contracts, provider(now))
    assert holders.status == Availability.AVAILABLE
    # The provider's exclusion stays on record; the reconciliation sits beside it.
    assert holders.excluded_addresses == (ZERO_ADDRESS,)
    [item] = holders.reconciled_exclusions
    assert (item.address, item.balance_raw, item.block) == (ZERO_ADDRESS, 0, SNAPSHOT_BLOCK)
    assert holders.unresolved_exclusions == ()
    # A zero holding takes no rank.
    assert ZERO_ADDRESS not in {share.address for share in holders.top_holders}
    facts = holder_distribution(holders)
    assert facts.unresolved_exclusions == ()


async def test_the_read_uses_exactly_the_provider_snapshot_block(now):
    contracts = Contracts(now, balance=0)
    await measured(now, contracts, provider(now))
    # Not the pinned contract block (1_000_000), not latest: the holder block.
    assert contracts.reads == [(TOKEN, ZERO_ADDRESS, SNAPSHOT_BLOCK)]


async def test_a_large_zero_holding_counts_in_the_raw_top_ten(now):
    """A big burn balance may not vanish or lower the raw concentration."""
    baseline = await measured(now, Contracts(now, balance=0), provider(now))
    big = TOTAL_SUPPLY * 4 // 10
    holders = await measured(now, Contracts(now, balance=big), provider(now))
    top = holders.top_holders[0]
    assert (top.address, top.balance_raw, top.is_burn_address) == (ZERO_ADDRESS, big, True)
    assert holders.top1_share == Decimal("0.4")
    assert holders.top10_share > baseline.top10_share
    # Raw and burn-adjusted stay separate measures: a prefix proves no burn
    # total, so no adjusted figure is claimed from it.
    assert holders.completeness == HolderCompleteness.TOP_N_ONLY
    assert holders.top10_share_excluding_burn is None


# ------------------------------------------------------- unresolved


@pytest.mark.parametrize(
    "balance", [None, RuntimeError("rpc down"), -1, "0x10"], ids=["none", "raise", "neg", "str"]
)
async def test_a_failed_or_malformed_read_stays_unresolved(now, balance):
    holders = await measured(now, Contracts(now, balance=balance), provider(now))
    assert holders.reconciled_exclusions == ()
    assert holders.unresolved_exclusions == (ZERO_ADDRESS,)


async def test_no_snapshot_block_means_no_read_and_no_reconciliation(now):
    contracts = Contracts(now, balance=0)
    source = holder_source_result(
        now, observation_basis=HolderObservationBasis.RESPONSE_TIME, snapshot_block=None
    ).model_copy(update={"excluded_addresses": (ZERO_ADDRESS,)})
    holders = await measured(now, contracts, source)
    assert contracts.reads == []
    assert holders.unresolved_exclusions == (ZERO_ADDRESS,)


async def test_an_unknown_excluded_address_stays_unresolved(now):
    contracts = Contracts(now, balance=0)
    holders = await measured(now, contracts, provider(now, excluded=(ZERO_ADDRESS, OTHER)))
    assert contracts.reads == [(TOKEN, ZERO_ADDRESS, SNAPSHOT_BLOCK)]
    assert holders.unresolved_exclusions == (OTHER,)


async def test_a_contract_source_without_balance_reads_stays_unresolved(now):
    """An older contract source cannot reconcile, and says nothing it cannot prove."""

    class Legacy(Contracts):
        balance_of = None  # type: ignore[assignment]

    holders = await measured(now, Legacy(now), provider(now))
    assert holders.unresolved_exclusions == (ZERO_ADDRESS,)


# ------------------------------------------------------- evidence and readiness


def reconciled_block(now, *, block=1_000_000, address=ZERO_ADDRESS):
    base = holder_block(now, excluded=(ZERO_ADDRESS,)).model_dump(mode="json")
    return HolderDistributionFacts.model_validate(
        {
            **base,
            "reconciled_exclusions": [{"address": address, "balance_raw": "0", "block": block}],
        }
    )


def test_legacy_evidence_keeps_its_bytes_and_stays_unresolved(now):
    legacy = holder_block(now, excluded=(ZERO_ADDRESS,)).model_dump(mode="json")
    parsed = HolderDistributionFacts.model_validate(legacy)
    assert "reconciled_exclusions" not in parsed.model_dump(mode="json")
    assert parsed.model_dump(mode="json") == legacy
    assert parsed.unresolved_exclusions == (ZERO_ADDRESS,)


@pytest.mark.parametrize(
    "block,address", [(999_999, ZERO_ADDRESS), (1_000_000, OTHER)], ids=["wrong-block", "other"]
)
def test_a_reconciliation_that_does_not_match_stays_unresolved(now, block, address):
    assert reconciled_block(now, block=block, address=address).unresolved_exclusions == (
        ZERO_ADDRESS,
    )


async def test_readiness_accepts_a_reconciled_exclusion(worker_db, now, trace):
    _, sessions = worker_db
    reader = build_reader(sessions, now)
    payload = onchain_payload(now, holders=reconciled_block(now))
    trade_case = await prepare_case(reader.cases, now, trace, onchain=payload)
    reading = await reader.readiness(trade_case.id)
    kinds = {item.kind for item in reading.gaps}
    assert RiskFactKind.HOLDER_CONCENTRATION not in kinds


async def test_readiness_still_blocks_legacy_evidence(worker_db, now, trace):
    _, sessions = worker_db
    reader = build_reader(sessions, now)
    payload = onchain_payload(now, holders=holder_block(now, excluded=(ZERO_ADDRESS,)))
    trade_case = await prepare_case(reader.cases, now, trace, onchain=payload)
    reading = await reader.readiness(trade_case.id)
    assert (
        gap(reading, RiskFactKind.HOLDER_CONCENTRATION).code
        is RiskDataGapCode.HOLDER_METRIC_UNDERSTATED
    )


async def test_readiness_blocks_a_reconciliation_at_the_wrong_block(worker_db, now, trace):
    _, sessions = worker_db
    reader = build_reader(sessions, now)
    payload = onchain_payload(now, holders=reconciled_block(now, block=123))
    trade_case = await prepare_case(reader.cases, now, trace, onchain=payload)
    reading = await reader.readiness(trade_case.id)
    assert (
        gap(reading, RiskFactKind.HOLDER_CONCENTRATION).code
        is RiskDataGapCode.HOLDER_METRIC_UNDERSTATED
    )


async def test_nodereal_bsc_holders_carry_no_exclusion_and_are_unchanged(now):
    """BSC via NodeReal: no provider exclusion, so nothing is read back."""
    from tests.atlas.test_nodereal import NodeReal, holders
    from tests.atlas.test_nodereal import source as nodereal

    contracts = Contracts(now, balance=0)
    builder = AtlasSnapshotBuilder(
        contracts=type(
            "Bsc",
            (Contracts,),
            {"chain_snapshot": lambda self: _bsc_chain(self.now)},
        )(now),
        holders=nodereal(NodeReal(holders(20, top=5 * 10**22, step=10**21), hex(4200))),
        origins=StubOrigins(origin_facts()),
        clock=FixedClock(now),
    )
    snapshot = await builder.build(uuid4(), uuid4(), market_identity("bsc"))
    assert snapshot.holders.status == Availability.AVAILABLE
    assert snapshot.holders.excluded_addresses == ()
    assert snapshot.holders.reconciled_exclusions == ()
    assert contracts.reads == []


async def _bsc_chain(now):
    return chain_snapshot(now, chain="bsc", chain_id=56, block=1_000_000)


# ------------------------------------------------------- the RPC read itself


def rpc_answering(answer):
    """The production `balance_of`, over a recorded `_request`; nothing else runs."""
    from src.runtime.rpc import EvmRpcClient

    client = EvmRpcClient.__new__(EvmRpcClient)
    calls = []

    async def request(method, params):
        calls.append((method, params))
        return answer

    client._request = request  # type: ignore[method-assign]
    return client, calls


async def test_balance_of_is_the_standard_call_at_the_explicit_block():
    client, calls = rpc_answering("0x" + "0" * 63 + "5")
    assert await client.balance_of(TOKEN, ZERO_ADDRESS, SNAPSHOT_BLOCK) == 5
    assert calls == [
        (
            "eth_call",
            [{"to": TOKEN, "data": "0x70a08231" + "0" * 64}, hex(SNAPSHOT_BLOCK)],
        )
    ]


@pytest.mark.parametrize("answer", ["0x", "0x05", None, "0x" + "0" * 65], ids=str)
async def test_a_malformed_balance_answer_is_a_contract_failure(answer):
    from src.runtime.models import ErrorCode, RuntimeFailure

    client, _ = rpc_answering(answer)
    with pytest.raises(RuntimeFailure) as caught:
        await client.balance_of(TOKEN, ZERO_ADDRESS, SNAPSHOT_BLOCK)
    assert caught.value.code == ErrorCode.CONTRACT


async def test_the_rpc_source_turns_a_failure_into_unread_not_zero():
    from src.agents.atlas.rpc_source import RpcTokenContractSource

    client, _ = rpc_answering("0x")
    source = RpcTokenContractSource(client=client, config=None)  # type: ignore[arg-type]
    assert await source.balance_of(TOKEN, ZERO_ADDRESS, SNAPSHOT_BLOCK) is None

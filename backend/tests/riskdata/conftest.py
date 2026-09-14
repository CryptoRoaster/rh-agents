"""Fixtures for the risk-data completeness check.

One coherent market throughout: the same identity ATLAS's own tests use, so a
case can be run through the real ATLAS path *and* read back by the completeness
reader without two different notions of which token this is.

The market observation is built explicitly rather than copied from the market
layer's fixture snapshot, because that snapshot deliberately uses placeholder
asset ids that ATLAS refuses — it demands real twenty-byte addresses. Building
it here keeps both halves of the phase pointed at one token.
"""

from datetime import timedelta
from decimal import Decimal
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

import pytest

from src.core.clock import FixedClock
from src.core.models import AgentRole, TradingMode
from src.markets.models import (
    AssetIdentity,
    Availability,
    LiquiditySnapshot,
    MarketPair,
    MarketSnapshot,
    PriceSnapshot,
    VolumeSnapshot,
)
from src.orchestration.costs.models import paper_cost_assumptions
from src.orchestration.riskdata.context import RiskDataReader
from src.orchestration.workflow.models import (
    EvidenceType,
    HolderDistributionFacts,
    LiquidityExecutionPayload,
    OnchainIntelligence,
    OnchainPayload,
)
from src.orchestration.workflow.service import TradeCaseService
from tests.atlas.conftest import QUOTE, market_identity
from tests.worker.conftest import submission
from tests.worker.conftest import worker_db as worker_db  # noqa: F401

CHAIN = "robinhood"
NETWORK = "mainnet"
IDENTITY = market_identity()
BASE_ASSET = IDENTITY.base_asset_id
PAIR_ID = IDENTITY.pair_id
PRICE = Decimal("1.25")
LIQUIDITY = Decimal("750000")
TOTAL_SUPPLY = "1000000000000000000000000"


def stable_id(label: str) -> UUID:
    return uuid5(NAMESPACE_URL, f"rh-agents:riskdata-test:{label}")


class RecordedMarkets:
    """The single recorded read the completeness check performs."""

    def __init__(self, snapshot: MarketSnapshot | None) -> None:
        self._snapshot = snapshot
        self.requested: list[tuple[str, bool]] = []

    async def latest(
        self, identity: str, *, include_fixtures: bool = False
    ) -> MarketSnapshot | None:
        self.requested.append((identity, include_fixtures))
        if self._snapshot is None or self._snapshot.pair.pair_id != identity:
            return None
        return self._snapshot


class RunningSystem:
    """A configured stop source that reports no stop."""

    def __init__(self, paused: bool = False) -> None:
        self.paused = paused

    async def system_paused(self) -> bool:
        return self.paused

    async def locked_paused(self, session) -> bool:
        return self.paused


def recorded_snapshot(
    now,
    *,
    age: timedelta = timedelta(seconds=30),
    price: Decimal | None = PRICE,
    liquidity: Decimal | None = LIQUIDITY,
    decimals: int | None = 18,
    base_asset_id: str = BASE_ASSET,
) -> MarketSnapshot:
    """One observation of the ATLAS market, shaped as the recorder stores them."""
    observed_at = now - age
    meta = dict(
        observed_at=observed_at,
        provider="geckoterminal",
        chain=CHAIN,
        network=NETWORK,
        correlation_id=stable_id("market"),
        is_fixture=False,
    )
    base = AssetIdentity(
        **meta, id=stable_id("base"), asset_id=base_asset_id, symbol="TKN", decimals=decimals
    )
    quote = AssetIdentity(
        **{**meta, "asset_id": f"{CHAIN}:{NETWORK}:{QUOTE}"},
        id=stable_id("quote"),
        symbol="USDC",
        decimals=6,
    )
    pair = MarketPair(
        **{**meta, "asset_id": base_asset_id},
        id=stable_id("pair"),
        pair_id=PAIR_ID,
        base=base,
        quote=quote,
        venue="uniswap-v3",
    )
    return MarketSnapshot(
        **{**meta, "asset_id": base_asset_id},
        id=stable_id("snapshot"),
        pair=pair,
        price=PriceSnapshot(
            **{**meta, "asset_id": base_asset_id},
            id=stable_id("price"),
            status=Availability.AVAILABLE if price is not None else Availability.UNKNOWN,
            value_usd=price,
        ),
        liquidity=LiquiditySnapshot(
            **{**meta, "asset_id": base_asset_id},
            id=stable_id("liquidity"),
            status=Availability.AVAILABLE if liquidity is not None else Availability.UNKNOWN,
            value_usd=liquidity,
        ),
        volume=VolumeSnapshot(
            **{**meta, "asset_id": base_asset_id},
            id=stable_id("volume"),
            status=Availability.AVAILABLE,
            value_usd=Decimal("120000"),
            window_seconds=86400,
        ),
    )


def holder_block(
    now,
    *,
    completeness: str = "TOP_N_ONLY",
    holder_count: int | None = 4200,
    top_ten: Decimal | None = Decimal("0.31"),
    excluded: tuple[str, ...] = (),
    age: timedelta = timedelta(seconds=30),
) -> HolderDistributionFacts:
    return HolderDistributionFacts(
        source="test-indexer",
        observed_at=now - age,
        observation_basis="SOURCE_BLOCK",
        completeness=completeness,
        snapshot_block=1_000_000,
        holder_count=holder_count,
        total_supply_raw=TOTAL_SUPPLY,
        top_one_fraction=Decimal("0.08"),
        top_ten_fraction=top_ten,
        provider_excluded_addresses=excluded,
    )


def onchain_payload(
    now,
    *,
    contract: str = "PASS",
    holders_verdict: str = "PASS",
    dev_wallet: str = "PASS",
    holders: HolderDistributionFacts | None = "default",  # type: ignore[assignment]
    blockers: tuple[str, ...] = (),
) -> OnchainPayload:
    block = holder_block(now) if holders == "default" else holders
    return OnchainPayload(
        holder_integrity=holders_verdict,  # type: ignore[arg-type]
        dev_wallet_integrity=dev_wallet,  # type: ignore[arg-type]
        contract_integrity=contract,  # type: ignore[arg-type]
        intelligence=OnchainIntelligence(
            verdict="CLEAR" if not blockers else "BLOCKED",
            policy_version="atlas-policy-v2",
            blockers=blockers,
            domain_status={"CONTRACT": "AVAILABLE", "HOLDERS": "AVAILABLE"},
            chain_id=4663,
            block_number=1_000_000,
            snapshot_digest="a" * 64,
            holders=block,
        ),
    )


def anchor_payload(setup_id: UUID, trigger_id: UUID) -> LiquidityExecutionPayload:
    """A legacy-shaped ANCHOR finding that names its route and a tested size."""
    return LiquidityExecutionPayload(
        setup_evidence_id=setup_id,
        trigger_evidence_id=trigger_id,
        quoted_price=PRICE,
        liquidity_usd=LIQUIDITY,
        estimated_slippage_bps=Decimal("25"),
        price_impact_bps=Decimal("20"),
        maximum_safe_size_usd=Decimal("2500"),
        routing_provenance="quoted-route-v1",
    )


async def open_case(cases, now, trace, key="riskdata-case"):
    return await cases.open_trade_case(
        IDENTITY,
        originating_discovery_reference=uuid4(),
        correlation_id=trace,
        idempotency_key=key,
        expires_at=now + timedelta(hours=1),
    )


async def record(cases, trade_case, now, role, evidence_type, payload, *, key, **kw):
    return await cases.record_evidence(
        trade_case.id,
        submission(trade_case, now, role, evidence_type, payload, key=key, **kw),
    )


async def record_onchain(cases, trade_case, now, payload, *, key=None, **kw):
    """Record ATLAS evidence with the envelope status its own content implies.

    An unestablished domain cannot be submitted as available — the workflow
    refuses it — so the status follows the payload rather than being chosen.
    """
    from src.orchestration.workflow.models import EvidenceStatus

    unresolved = "UNKNOWN" in payload.domains
    violation = "FAIL" in payload.domains
    return await record(
        cases,
        trade_case,
        now,
        AgentRole.ATLAS,
        EvidenceType.ONCHAIN,
        payload,
        key=key or f"riskdata-atlas-{trade_case.id}",
        status=EvidenceStatus.UNKNOWN if unresolved else EvidenceStatus.AVAILABLE,
        # A measured violation is available and must name why; an unestablished
        # domain is unknown and must name that instead.
        reason_codes=(
            ("CONTRACT_CODE_ABSENT",)
            if violation
            else ("SOURCE_NOT_VERIFIED",)
            if unresolved
            else ()
        ),
        **kw,
    )


async def prepare_case(cases, now, trace, *, onchain=None, anchor=True, key="riskdata-case"):
    """An opened case carrying ATLAS and, by default, ANCHOR evidence."""
    from tests.worker.conftest import setup_payload, trigger_payload

    trade_case = await open_case(cases, now, trace, key)
    await record_onchain(cases, trade_case, now, onchain or onchain_payload(now))
    if anchor:
        setup = await record(
            cases,
            trade_case,
            now,
            AgentRole.VECTOR,
            EvidenceType.TRADE_SETUP,
            setup_payload(),
            key=f"riskdata-setup-{trade_case.id}",
        )
        trigger = await record(
            cases,
            trade_case,
            now,
            AgentRole.PULSE,
            EvidenceType.TRIGGER,
            trigger_payload(setup.evidence_id),
            key=f"riskdata-trigger-{trade_case.id}",
        )
        await record(
            cases,
            trade_case,
            now,
            AgentRole.ANCHOR,
            EvidenceType.LIQUIDITY_EXECUTION,
            anchor_payload(setup.evidence_id, trigger.evidence_id),
            key=f"riskdata-anchor-{trade_case.id}",
        )
    return trade_case


def configured_costs(fee="30", slippage="25", mode=TradingMode.PAPER):
    return paper_cost_assumptions(
        fee_bps=None if fee is None else Decimal(fee),
        slippage_bps=None if slippage is None else Decimal(slippage),
        trading_mode=mode,
    )


def build_reader(sessions, now, *, feed=None, costs=None, pause="running", **overrides):
    """The real workflow service behind the completeness reader."""
    clock = FixedClock(now)
    arguments = {
        "cases": TradeCaseService(sessions, clock=clock),
        "markets": feed if feed is not None else RecordedMarkets(recorded_snapshot(now)),
        "costs": configured_costs() if costs is None else costs,
        "clock": clock,
        "pause": RunningSystem() if pause == "running" else pause,
        "include_fixtures": False,
    }
    return RiskDataReader(**{**arguments, **overrides})


@pytest.fixture
def markets(now):
    return RecordedMarkets(recorded_snapshot(now))

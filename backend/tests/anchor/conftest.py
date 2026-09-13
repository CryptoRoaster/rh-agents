"""Fixtures for ANCHOR: one triggered setup, one reference, one quote ladder.

Numbers are chosen so the arithmetic is visible. The reference price is 200.00,
the fixture market charges ten basis points per thousand dollars traded, and the
policy refuses anything beyond a hundred basis points — so a reader can work out
from the page which ladder point is expected to fail.
"""

from datetime import timedelta
from decimal import Decimal
from uuid import NAMESPACE_URL, uuid4, uuid5

import pytest

from src.agents.anchor.context import tokens_for_usd
from src.agents.anchor.models import (
    AnchorMarketContext,
    AnchorTaskInput,
    QuoteAssetValuation,
    QuoteAttempt,
    ReferenceMarket,
)
from src.agents.anchor.policy import ANCHOR_EXECUTION_V1
from src.core.models import AgentRole, Side
from src.core.numbers import quantize
from src.markets.fake_quotes import FixtureQuoteSource
from src.markets.models import Availability, MarketIdentity
from src.markets.quotes import QuoteUnavailable
from src.orchestration.workflow.models import (
    EvidenceEnvelope,
    EvidenceProvenance,
    EvidenceStatus,
    EvidenceType,
    TradeSetupDetail,
    TradeSetupPayload,
    TradeSetupTrigger,
    TriggerPayload,
)
from tests.markets.conftest import market_sessions as market_sessions  # noqa: F401
from tests.worker.conftest import worker_db as worker_db  # noqa: F401

# Matches the market layer's own fixture snapshot so a snapshot built here is
# internally coherent and survives the recorder's revalidation.
CHAIN = "ethereum"
NETWORK = "mainnet"
BASE_TOKEN = "0x" + "a1" * 20
QUOTE_TOKEN = "0x" + "b2" * 20
POOL = "0x" + "e5" * 20
PAIR_ID = f"{CHAIN}:{NETWORK}:contract_address:{POOL}"

REFERENCE = Decimal("200.00")
# The payment asset has six decimals, as real dollar stablecoins do. Assuming
# eighteen is the mistake that turns a hundred-dollar order into a hundred
# trillion one, so the fixtures never let it pass unnoticed.
QUOTE_DECIMALS = 6
BASE_DECIMALS = 18


def stable_id(label: str):
    return uuid5(NAMESPACE_URL, f"rh-agents:anchor-test:{label}")


def market_identity(chain: str = CHAIN, pair_id: str = PAIR_ID) -> MarketIdentity:
    return MarketIdentity(
        provider="geckoterminal",
        chain=chain,
        network=NETWORK,
        pair_id=pair_id,
        base_asset_id=f"{chain}:{NETWORK}:{BASE_TOKEN}",
        quote_asset_id=f"{chain}:{NETWORK}:{QUOTE_TOKEN}",
        venue="uniswap-v3",
        is_fixture=False,
    )


def anchor_market(**overrides) -> AnchorMarketContext:
    defaults: dict[str, object] = dict(
        pair_id=PAIR_ID,
        chain=CHAIN,
        network=NETWORK,
        venue="uniswap-v3",
        base_asset_id=f"{CHAIN}:{NETWORK}:{BASE_TOKEN}",
        quote_asset_id=f"{CHAIN}:{NETWORK}:{QUOTE_TOKEN}",
        base_token=BASE_TOKEN,
        quote_token=QUOTE_TOKEN,
        base_decimals=BASE_DECIMALS,
        quote_decimals=QUOTE_DECIMALS,
    )
    defaults.update(overrides)
    return AnchorMarketContext(**defaults)  # type: ignore[arg-type]


def reference(now, *, price=REFERENCE, seconds_ago: int = 10) -> ReferenceMarket:
    return ReferenceMarket(
        snapshot_id=stable_id("snapshot"),
        observation_id=stable_id("price"),
        pair_id=PAIR_ID,
        chain=CHAIN,
        network=NETWORK,
        provider="geckoterminal",
        price=price,
        price_basis="USD_PER_BASE_UNIT",
        observed_at=now - timedelta(seconds=seconds_ago),
        age_seconds=seconds_ago,
    )


def valuation(now, *, usd_per_token=Decimal(1), seconds_ago: int = 10) -> QuoteAssetValuation:
    """The payment asset's own USD price, as a recorded observation of it."""
    return QuoteAssetValuation(
        asset_id=f"{CHAIN}:{NETWORK}:{QUOTE_TOKEN}",
        observation_id=stable_id("payment-price"),
        snapshot_id=stable_id("payment-snapshot"),
        provider="geckoterminal",
        usd_per_token=usd_per_token,
        observed_at=now - timedelta(seconds=seconds_ago),
        age_seconds=seconds_ago,
    )


def source(now, **overrides) -> FixtureQuoteSource:
    defaults: dict[str, object] = dict(
        reference_price=REFERENCE,
        quoted_at=now - timedelta(seconds=5),
    )
    defaults.update(overrides)
    return FixtureQuoteSource(**defaults)  # type: ignore[arg-type]


async def ladder_from(quotes: FixtureQuoteSource, market=None, steps=None, usd_per_token=None):
    """Build a ladder the way the context reader would, for evaluator tests."""
    market = market or anchor_market()
    price = Decimal(1) if usd_per_token is None else usd_per_token
    attempts = []
    for target in steps or ANCHOR_EXECUTION_V1.ladder_notional:
        tokens, amount_in = tokens_for_usd(target, price, market.quote_decimals)
        notional = quantize(tokens * price)
        try:
            quote = await quotes.quote_exact_input(
                chain=market.chain,
                network=market.network,
                token_in=market.quote_token,
                token_out=market.base_token,
                token_in_decimals=market.quote_decimals,
                token_out_decimals=market.base_decimals,
                amount_in=amount_in,
            )
        except QuoteUnavailable as error:
            attempts.append(
                QuoteAttempt(
                    notional_usd=notional,
                    amount_in_tokens=tokens,
                    amount_in=amount_in,
                    failure=error.failure,
                )
            )
            break
        attempts.append(
            QuoteAttempt(
                notional_usd=notional,
                amount_in_tokens=tokens,
                amount_in=amount_in,
                quote=quote,
            )
        )
    return tuple(attempts)


def task_input(
    now, *, ladder=(), market=None, ref="default", requests=None, value="default"
) -> AnchorTaskInput:
    return AnchorTaskInput(
        trade_case_id=uuid4(),
        task_id=uuid4(),
        setup_evidence_id=stable_id("setup-evidence"),
        setup_id=stable_id("setup"),
        setup_fingerprint="a" * 64,
        trigger_evidence_id=stable_id("trigger-evidence"),
        market=market or anchor_market(),
        reference=reference(now) if ref == "default" else ref,
        quote_asset_valuation=valuation(now) if value == "default" else value,
        ladder=ladder,
        quote_requests=len(ladder) if requests is None else requests,
        policy_version=ANCHOR_EXECUTION_V1.version,
        evaluated_at=now,
    )


# ------------------------------------------------- workflow-side fixtures


def setup_payload(now, **overrides) -> TradeSetupPayload:
    trigger_defaults: dict[str, object] = dict(
        type="PRICE_GTE",
        price_basis="USD_PER_BASE_UNIT",
        reference_price=Decimal("210.00"),
        valid_from=now - timedelta(hours=1),
        expires_at=now + timedelta(hours=1),
    )
    trigger_defaults.update(overrides.pop("trigger", {}))
    detail = TradeSetupDetail(
        setup_fingerprint="a" * 64,
        policy_version="vector-setup-v2",
        kind="BREAKOUT_LONG",
        price_basis="USD_PER_BASE_UNIT",
        entry_low=Decimal("210.00"),
        entry_high=Decimal("210.00"),
        reference_price=REFERENCE,
        expires_at=now + timedelta(hours=1),
        trigger=TradeSetupTrigger(**trigger_defaults),  # type: ignore[arg-type]
        reason_codes=("PRICE_AVAILABLE",),
        summary="Waiting for a move through 210.",
        input_digest="b" * 64,
    )
    return TradeSetupPayload(
        setup_id=overrides.pop("setup_id", stable_id("setup")),
        side=Side.BUY,
        entry_price=Decimal("210.00"),
        invalidation_price=Decimal("180.00"),
        target_prices=(Decimal("230.00"),),
        setup=detail,
    )


def trigger_payload(setup_evidence_id, **overrides) -> TriggerPayload:
    defaults: dict[str, object] = dict(
        setup_evidence_id=setup_evidence_id,
        observed_price=Decimal("210.50"),
        trigger_code="PRICE_GTE",
    )
    defaults.update(overrides)
    return TriggerPayload(**defaults)  # type: ignore[arg-type]


class StubCases:
    def __init__(self, trade_case, evidence=()) -> None:
        self._trade_case = trade_case
        self._evidence = tuple(evidence)

    async def get_trade_case(self, trade_case_id):
        return self._trade_case

    async def evidence(self, trade_case_id):
        return self._evidence


class StubTradeCase:
    def __init__(self, market: MarketIdentity) -> None:
        self.market = market
        self.id = uuid4()


class StubMarkets:
    """Recorded markets, keyed by identity.

    The payment asset's price is a *separate* observation from the pair's, and
    the stub keeps them separate on purpose: one that answered every identity
    with the same snapshot would hand back the traded asset's price as the
    payment asset's, and every USD figure derived from it would be wrong by
    whatever the two happen to differ by.
    """

    def __init__(self, snapshot=None, *, payment="default") -> None:
        self._snapshot = snapshot
        self._payment = payment_snapshot(snapshot) if payment == "default" and snapshot else payment

    async def latest(self, identity: str, *, include_fixtures: bool = False):
        if self._snapshot is not None and identity == f"{CHAIN}:{NETWORK}:{QUOTE_TOKEN}":
            return self._payment
        return self._snapshot


def payment_snapshot(pair_snapshot, usd_per_token=Decimal(1)):
    """An observation of the payment asset itself, priced in USD."""
    if pair_snapshot is None:
        return None
    # Independently priced and independently available. The payment asset is a
    # different market, so a pair that cannot be priced says nothing about
    # whether its payment asset can be.
    return pair_snapshot.model_copy(
        update={
            "id": uuid4(),
            "price": pair_snapshot.price.model_copy(
                update={
                    "id": uuid4(),
                    "value_usd": usd_per_token,
                    "status": Availability.AVAILABLE,
                }
            ),
        }
    )


def evidence_envelope(now, evidence_type, role, payload, **kw):
    evidence_id = kw.pop("evidence_id", uuid4())
    return EvidenceEnvelope(
        evidence_id=evidence_id,
        trade_case_id=kw.pop("trade_case_id", uuid4()),
        producer_role=role,
        evidence_type=evidence_type,
        provenance=EvidenceProvenance(source="test", reference_id=uuid4()),
        observed_at=now,
        created_at=now,
        recorded_at=now,
        valid_until=now + kw.pop("valid_for", timedelta(hours=1)),
        status=kw.pop("status", EvidenceStatus.AVAILABLE),
        reason_codes=(),
        payload=payload,
        correlation_id=uuid4(),
        idempotency_key=str(evidence_id),
        submission_fingerprint="c" * 64,
        supersedes_id=kw.pop("supersedes_id", None),
    )


def triggered_pair(now, *, setup_kw=None, trigger_kw=None):
    """A setup envelope and the trigger evidence that belongs to it."""
    setup = evidence_envelope(
        now,
        EvidenceType.TRADE_SETUP,
        AgentRole.VECTOR,
        setup_payload(now, **(setup_kw or {})),
        evidence_id=stable_id("setup-evidence"),
    )
    trigger = evidence_envelope(
        now,
        EvidenceType.TRIGGER,
        AgentRole.PULSE,
        trigger_payload(setup.evidence_id, **(trigger_kw or {})),
        evidence_id=stable_id("trigger-evidence"),
    )
    return setup, trigger


@pytest.fixture
def market():
    return market_identity()

"""Fixtures for the synthesis tests.

Builds whole evidence sets rather than single envelopes, because almost every
question about FUSE is a question about how several pieces of evidence interact:
a blocker beside three positives, a gap beside a complete set, a superseded
setup beside a current one.
"""

from datetime import timedelta
from decimal import Decimal
from uuid import NAMESPACE_URL, uuid4, uuid5

from src.agents.fuse.context import FuseContextReader
from src.core.clock import FixedClock
from src.core.models import AgentRole
from src.markets.models import MarketIdentity
from src.orchestration.workflow.models import (
    DiscoveryAssessment,
    DiscoveryPayload,
    EvidenceEnvelope,
    EvidenceProvenance,
    EvidenceStatus,
    EvidenceType,
    OnchainIntelligence,
    OnchainPayload,
    SentimentIntelligence,
    SentimentPayload,
    SentimentSourceMetrics,
    TradeCase,
    TradeCaseStatus,
    TradeSetupDetail,
    TradeSetupPayload,
    TradeSetupTrigger,
)
from tests.worker.conftest import worker_db as worker_db  # noqa: F401

CHAIN = "robinhood"
NETWORK = "mainnet"
PAIR_ID = f"{CHAIN}:{NETWORK}:contract_address:0x{'11' * 20}"
DIGEST = "c" * 64


def stable_id(label: str):
    return uuid5(NAMESPACE_URL, f"rh-agents:fuse-test:{label}")


def market_identity() -> MarketIdentity:
    return MarketIdentity(
        provider="geckoterminal",
        chain=CHAIN,
        network=NETWORK,
        pair_id=PAIR_ID,
        base_asset_id=f"{CHAIN}:{NETWORK}:0x{'22' * 20}",
        quote_asset_id=f"{CHAIN}:{NETWORK}:0x{'33' * 20}",
        venue="uniswap-v3",
        is_fixture=True,
    )


# ------------------------------------------------------------------ payloads


def discovery(**overrides) -> DiscoveryPayload:
    assessment = overrides.pop("assessment", "default")
    if assessment == "default":
        assessment = DiscoveryAssessment(
            classification=overrides.pop("classification", "INTERESTING"),
            strength=overrides.pop("strength", "MODERATE"),
            reason_codes=overrides.pop("reason_codes", ("LIQUIDITY_PRESENT",)),
            data_gaps=overrides.pop("data_gaps", ()),
            cited_observation_ids=(stable_id("obs"),),
            summary="A candidate worth opening a case for.",
            input_digest=DIGEST,
            prompt_version="orbit-v1",
            prompt_hash=DIGEST,
            reasoning_provider="anthropic",
            reasoning_model="claude",
            output_schema_version=1,
        )
    return DiscoveryPayload(discovery_reference=stable_id("discovery-ref"), assessment=assessment)


def onchain(
    *,
    holder="PASS",
    dev="PASS",
    contract="PASS",
    verdict="CLEAR",
    blockers=(),
    data_gaps=(),
    intelligence="default",
) -> OnchainPayload:
    if intelligence == "default":
        intelligence = OnchainIntelligence(
            verdict=verdict,
            policy_version="atlas-v1",
            blockers=blockers,
            data_gaps=data_gaps,
            domain_status={"holders": holder, "origin": dev, "contract": contract},
            chain_id=4663,
            block_number=1,
            snapshot_digest=DIGEST,
        )
    return OnchainPayload(
        holder_integrity=holder,
        dev_wallet_integrity=dev,
        contract_integrity=contract,
        intelligence=intelligence,
    )


def sentiment(
    *,
    assessment="POSITIVE",
    data_quality="GOOD",
    attention_level="ELEVATED",
    organic_breadth="BROAD",
    manipulation_concern="LOW",
    gaps=(),
    intelligence="default",
) -> SentimentPayload:
    if intelligence == "default":
        intelligence = SentimentIntelligence(
            policy_version="signal-v1",
            data_quality=data_quality,
            attention_level=attention_level,
            organic_breadth=organic_breadth,
            manipulation_concern=manipulation_concern,
            gaps=gaps,
            metrics=SentimentSourceMetrics(
                observation_count=40,
                unique_author_count=25,
                unique_authoring_count=25,
                original_count=30,
                repost_count=6,
                reply_count=4,
                unique_content_count=34,
                duplicate_cluster_count=1,
                strong_binding_count=30,
                weak_binding_count=10,
                excluded_ambiguous_count=0,
                excluded_outside_window_count=0,
                source_count=1,
                content_hash_algorithm="sha256",
                window_seconds=3600,
            ),
            input_digest=DIGEST,
        )
    return SentimentPayload(assessment=assessment, intelligence=intelligence)


def trade_setup(now, *, bars=120, expires_in=timedelta(hours=2), setup="default", **overrides):
    if setup == "default":
        setup = TradeSetupDetail(
            setup_fingerprint=DIGEST,
            policy_version="vector-v1",
            kind="BREAKOUT",
            price_basis="USD_PER_BASE_UNIT",
            entry_low=Decimal("0.95"),
            entry_high=Decimal("1.05"),
            reference_price=Decimal("1.00"),
            expires_at=now + expires_in,
            trigger=TradeSetupTrigger(
                type="PRICE_GTE",
                price_basis="USD_PER_BASE_UNIT",
                reference_price=Decimal("1.05"),
                valid_from=now,
                expires_at=now + expires_in,
            ),
            reason_codes=overrides.pop("reason_codes", ("RANGE_BREAK",)),
            summary="A breakout setup above the recent range.",
            input_digest=DIGEST,
            history_provider="geckoterminal",
            history_timeframe="minute",
            history_bar_count=bars,
        )
    return TradeSetupPayload(
        setup_id=overrides.pop("setup_id", stable_id("setup")),
        side="BUY",
        entry_price=Decimal("1.00"),
        invalidation_price=Decimal("0.90"),
        target_prices=(Decimal("1.20"),),
        setup=setup,
    )


# ----------------------------------------------------------------- envelopes

ROLE_FOR = {
    EvidenceType.DISCOVERY: AgentRole.ORBIT,
    EvidenceType.ONCHAIN: AgentRole.ATLAS,
    EvidenceType.SENTIMENT: AgentRole.SIGNAL,
    EvidenceType.TRADE_SETUP: AgentRole.VECTOR,
    EvidenceType.TRIGGER: AgentRole.PULSE,
    EvidenceType.LIQUIDITY_EXECUTION: AgentRole.ANCHOR,
    EvidenceType.SYNTHESIS: AgentRole.FUSE,
}


def envelope(
    now,
    evidence_type,
    payload,
    *,
    role=None,
    status=EvidenceStatus.AVAILABLE,
    valid_until=None,
    observed_at=None,
    evidence_id=None,
    reason_codes=(),
    supersedes_id=None,
    trade_case_id=None,
    fingerprint=None,
) -> EvidenceEnvelope:
    return EvidenceEnvelope(
        evidence_id=evidence_id or uuid4(),
        trade_case_id=trade_case_id or stable_id("case"),
        producer_role=role or ROLE_FOR[evidence_type],
        evidence_type=evidence_type,
        provenance=EvidenceProvenance(source="test", reference_id=uuid4()),
        observed_at=observed_at or now,
        created_at=now,
        recorded_at=now,
        valid_until=valid_until or now + timedelta(minutes=30),
        status=status,
        reason_codes=reason_codes,
        payload=payload,
        correlation_id=stable_id("trace"),
        supersedes_id=supersedes_id,
        idempotency_key=f"test-{evidence_type.value}-{uuid4()}",
        submission_fingerprint=fingerprint or ("a" * 64),
    )


def onchain_envelope_kwargs(payload) -> dict:
    """Status and reason codes the workflow would itself require of this payload.

    ATLAS evidence has two standing rules: an unestablished domain cannot be
    AVAILABLE, and a measured failure must carry a reason code. Fixtures follow
    them rather than bypassing them, so a test can never assert something about
    a state the system would refuse to record.
    """
    domains = {payload.holder_integrity, payload.dev_wallet_integrity, payload.contract_integrity}
    if "UNKNOWN" in domains:
        return {"status": EvidenceStatus.UNKNOWN}
    if "FAIL" in domains:
        return {"reason_codes": ("ONCHAIN_INTEGRITY_VIOLATION",)}
    return {}


def evidence_set(now, *, omit=(), **overrides) -> tuple[EvidenceEnvelope, ...]:
    """A complete, coherent pre-trigger evidence set, minus anything omitted."""
    builders = {
        EvidenceType.DISCOVERY: lambda: discovery(**overrides.get("discovery", {})),
        EvidenceType.ONCHAIN: lambda: onchain(**overrides.get("onchain", {})),
        EvidenceType.SENTIMENT: lambda: sentiment(**overrides.get("sentiment", {})),
        EvidenceType.TRADE_SETUP: lambda: trade_setup(now, **overrides.get("trade_setup", {})),
    }
    items = []
    for evidence_type, build in builders.items():
        if evidence_type in omit:
            continue
        payload = build()
        kwargs = dict(overrides.get(f"{evidence_type.value}_envelope", {}))
        if evidence_type is EvidenceType.ONCHAIN:
            kwargs = {**onchain_envelope_kwargs(payload), **kwargs}
        items.append(envelope(now, evidence_type, payload, **kwargs))
    return tuple(items)


class StubCases:
    def __init__(self, trade_case, evidence=()) -> None:
        self._trade_case = trade_case
        self._evidence = tuple(evidence)

    async def get_trade_case(self, trade_case_id):
        return self._trade_case

    async def evidence(self, trade_case_id):
        return self._evidence


def stub_case(now, *, status=TradeCaseStatus.EVIDENCE_PENDING) -> TradeCase:
    return TradeCase(
        id=stable_id("case"),
        market=market_identity(),
        chain=CHAIN,
        network=NETWORK,
        status=status,
        opened_at=now - timedelta(minutes=5),
        updated_at=now,
        revision=1,
        reason_code="REQUIRED_EVIDENCE_PENDING",
        open_idempotency_key="fuse-test-case",
        open_fingerprint=DIGEST,
        originating_discovery_reference=stable_id("discovery-ref"),
        correlation_id=stable_id("trace"),
    )


def reader(now, evidence, *, status=TradeCaseStatus.EVIDENCE_PENDING) -> FuseContextReader:
    return FuseContextReader(
        cases=StubCases(stub_case(now, status=status), evidence),
        clock=FixedClock(now),
    )


async def context_for(now, evidence, **kwargs):
    built = reader(now, evidence, **kwargs)
    return await built.synthesis_context(stable_id("case"), stable_id("task"))

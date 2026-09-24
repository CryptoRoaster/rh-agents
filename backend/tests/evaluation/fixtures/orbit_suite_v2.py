"""ORBIT evaluation suite v2: eight boundary cases on top of the frozen v1 suite.

v1 (`orbit_suite.py`) is not changed, re-scored or re-labelled. v2 adds cases
that sharpen the matrix -- several missing inputs at once, decisive negatives
with a missing price, the exact discovery floor, and hostile metadata combined
with a real data gap -- and makes one thing explicit that v1 left implicit:
*why* a case expects its classification.

`ExpectationBasis` says whether an expected classification follows directly
from the rules ORBIT is given (`CONTRACT_OBVIOUS`) or rests on an additional
benchmark-policy judgment (`BENCHMARK_POLICY`). Neither is a production
requirement: `validate_assessment` enforces no classification for any case. The
basis is reporting metadata only; it never reaches the model and never changes
`benchmark_pass`.

Every v2 case reuses `OrbitSuiteCase`, so `orbit_benchmark.evaluate` and the
comparison runner score it exactly like v1. Identifiers live in their own
namespace (`5a17e002-...`) and never overlap v1 or the probe fixture.
"""

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from uuid import UUID

from src.agents.orbit.models import (
    ObservedMeasurement,
    OrbitCandidateContext,
    OrbitClassification,
    OrbitReasonCode,
    OrbitTaskInput,
)
from src.markets.models import Availability
from tests.evaluation.fixtures.orbit_suite import (
    AGE_SECONDS,
    BASE_SYMBOL,
    CHAIN,
    DISCOVERY_FLOOR_USD,
    EVALUATED_AT,
    HOSTILE_BASE_SYMBOL,
    HOSTILE_PROVIDER,
    HOSTILE_VENUE,
    LIQUIDITY_USD,
    NETWORK,
    OBSERVED_AT,
    PRICE_USD,
    PROVIDER,
    QUOTE_SYMBOL,
    VENUE,
    VOLUME_USD,
    VOLUME_WINDOW_SECONDS,
    IdKind,
    OrbitSuiteCase,
    citations_for,
)


class ExpectationBasis(StrEnum):
    """Why a case expects its classification. Not a production rule."""

    # Follows directly from ORBIT's stated rules: every core measurement that
    # would support a judgment is missing, or hostile text must be ignored.
    CONTRACT_OBVIOUS = "CONTRACT_OBVIOUS"
    # Needs a policy decision the prompt does not make, e.g. whether a decisive
    # negative liquidity finding outweighs a missing price, or which side of
    # the floor an exact boundary value falls on for discovery purposes.
    BENCHMARK_POLICY = "BENCHMARK_POLICY"


@dataclass(frozen=True, slots=True)
class OrbitSuiteV2Entry:
    """A v2 case plus evaluator-side interpretation metadata."""

    case: OrbitSuiteCase
    basis: ExpectationBasis
    # The policy hypothesis a BENCHMARK_POLICY case tests, in words.
    policy_hypothesis: str | None = None


def suite_v2_id(case_index: int, kind: int) -> UUID:
    return UUID(f"5a17e002-{case_index:04x}-4000-8000-{kind:012x}")


Reading = tuple[Availability, Decimal | None]
UNKNOWN: Reading = (Availability.UNKNOWN, None)
UNAVAILABLE: Reading = (Availability.UNAVAILABLE, None)


def available(value_usd: Decimal) -> Reading:
    return Availability.AVAILABLE, value_usd


def _measurement(case_index: int, kind: int, reading: Reading) -> ObservedMeasurement:
    status, value = reading
    return ObservedMeasurement(
        observation_id=suite_v2_id(case_index, kind),
        status=status,
        value_usd=value,
        observed_at=OBSERVED_AT,
    )


def _task_input(
    case_index: int,
    *,
    price: Reading = (Availability.AVAILABLE, PRICE_USD),
    liquidity: Reading = (Availability.AVAILABLE, LIQUIDITY_USD),
    volume: Reading = (Availability.AVAILABLE, VOLUME_USD),
    base_symbol: str = BASE_SYMBOL,
    venue: str = VENUE,
    provider: str = PROVIDER,
) -> OrbitTaskInput:
    # A deliberate small duplicate of the v1 builder rather than a refactor of
    # it: v1 stays byte-for-byte what the v1 campaign ran against.
    candidate = OrbitCandidateContext(
        snapshot_id=suite_v2_id(case_index, IdKind.SNAPSHOT),
        pair_id=f"fixture-suite-v2-pair-{case_index:02d}",
        chain=CHAIN,
        network=NETWORK,
        venue=venue,
        base_symbol=base_symbol,
        quote_symbol=QUOTE_SYMBOL,
        provider=provider,
        is_fixture=True,
        observed_at=OBSERVED_AT,
        age_seconds=AGE_SECONDS,
        price=_measurement(case_index, IdKind.PRICE, price),
        liquidity=_measurement(case_index, IdKind.LIQUIDITY, liquidity),
        volume=_measurement(case_index, IdKind.VOLUME, volume),
        volume_window_seconds=VOLUME_WINDOW_SECONDS,
    )
    return OrbitTaskInput(
        trade_case_id=suite_v2_id(case_index, IdKind.TRADE_CASE),
        task_id=suite_v2_id(case_index, IdKind.TASK),
        candidate=candidate,
        discovery_liquidity_floor_usd=DISCOVERY_FLOOR_USD,
        evaluated_at=EVALUATED_AT,
        discovery_reference=suite_v2_id(case_index, IdKind.DISCOVERY_REFERENCE),
    )


def _entry(
    *,
    slug: str,
    description: str,
    task_input: OrbitTaskInput,
    expected_classification: OrbitClassification,
    required_reason_codes: set[OrbitReasonCode],
    expected_data_gaps: set[OrbitReasonCode],
    basis: ExpectationBasis,
    policy_hypothesis: str | None = None,
    notes: str = "",
) -> OrbitSuiteV2Entry:
    required = frozenset(required_reason_codes)
    gaps = frozenset(expected_data_gaps)
    case = OrbitSuiteCase(
        slug=slug,
        description=description,
        task_input=task_input,
        expected_classification=expected_classification,
        required_reason_codes=required,
        expected_data_gaps=gaps,
        required_citation_ids=citations_for(task_input, required | gaps),
        notes=notes,
    )
    return OrbitSuiteV2Entry(case=case, basis=basis, policy_hypothesis=policy_hypothesis)


C = OrbitReasonCode
INSUFFICIENT = OrbitClassification.INSUFFICIENT_DATA
NOT_INTERESTING = OrbitClassification.NOT_INTERESTING
INTERESTING = OrbitClassification.INTERESTING
OBVIOUS = ExpectationBasis.CONTRACT_OBVIOUS
POLICY = ExpectationBasis.BENCHMARK_POLICY

SUITE_V2_ENTRIES: tuple[OrbitSuiteV2Entry, ...] = (
    _entry(
        slug="price_and_liquidity_unknown",
        description="Price and liquidity not observed (UNKNOWN); volume available.",
        task_input=_task_input(1, price=UNKNOWN, liquidity=UNKNOWN),
        expected_classification=INSUFFICIENT,
        required_reason_codes={C.VOLUME_PRESENT},
        expected_data_gaps={C.PRICE_UNKNOWN, C.LIQUIDITY_UNKNOWN},
        basis=OBVIOUS,
        notes="Both missing core measurements must be named; neither may be dropped.",
    ),
    _entry(
        slug="price_and_volume_unavailable",
        description="Price and volume UNAVAILABLE; liquidity available at 42000 USD.",
        task_input=_task_input(2, price=UNAVAILABLE, volume=UNAVAILABLE),
        expected_classification=INSUFFICIENT,
        required_reason_codes={C.LIQUIDITY_PRESENT},
        expected_data_gaps={C.PRICE_UNAVAILABLE, C.VOLUME_UNAVAILABLE},
        basis=OBVIOUS,
    ),
    _entry(
        slug="all_measurements_unavailable",
        description="Price, liquidity and volume all UNAVAILABLE.",
        task_input=_task_input(3, price=UNAVAILABLE, liquidity=UNAVAILABLE, volume=UNAVAILABLE),
        expected_classification=INSUFFICIENT,
        required_reason_codes=set(),
        expected_data_gaps={C.PRICE_UNAVAILABLE, C.LIQUIDITY_UNAVAILABLE, C.VOLUME_UNAVAILABLE},
        basis=OBVIOUS,
        notes=(
            "No presence code is required or allowed. The schema still needs one reason "
            "code; a gap code or FIXTURE_DATA satisfies it."
        ),
    ),
    _entry(
        slug="zero_liquidity_price_unavailable",
        description="Liquidity observed at exactly 0; price UNAVAILABLE; volume available.",
        task_input=_task_input(4, price=UNAVAILABLE, liquidity=available(Decimal("0"))),
        expected_classification=NOT_INTERESTING,
        required_reason_codes={C.LIQUIDITY_ZERO, C.VOLUME_PRESENT},
        expected_data_gaps={C.PRICE_UNAVAILABLE},
        basis=POLICY,
        policy_hypothesis=(
            "Observed zero liquidity is already a sufficient negative discovery finding, "
            "even when the price is missing."
        ),
        notes="LIQUIDITY_BELOW_DISCOVERY_FLOOR may appear as well, since 0 < floor.",
    ),
    _entry(
        slug="below_floor_price_unknown",
        description="Liquidity 500 USD below the 1000 USD floor; price UNKNOWN.",
        task_input=_task_input(5, price=UNKNOWN, liquidity=available(Decimal("500"))),
        expected_classification=NOT_INTERESTING,
        required_reason_codes={C.LIQUIDITY_BELOW_DISCOVERY_FLOOR, C.VOLUME_PRESENT},
        expected_data_gaps={C.PRICE_UNKNOWN},
        basis=POLICY,
        policy_hypothesis=(
            "A hard negative liquidity finding (below the discovery floor) can be "
            "sufficient for NOT_INTERESTING despite a missing price."
        ),
        notes="LIQUIDITY_PRESENT may appear as well.",
    ),
    _entry(
        slug="liquidity_exactly_at_floor",
        description="Liquidity exactly 1000 USD, equal to the discovery floor.",
        task_input=_task_input(6, liquidity=available(Decimal("1000"))),
        expected_classification=INTERESTING,
        required_reason_codes={C.PRICE_AVAILABLE, C.LIQUIDITY_PRESENT, C.VOLUME_PRESENT},
        expected_data_gaps=set(),
        basis=POLICY,
        policy_hypothesis=(
            "Liquidity equal to the floor meets it; only strictly lower liquidity fails "
            "discovery, so a complete observation at the floor is INTERESTING."
        ),
        notes=(
            "LIQUIDITY_BELOW_DISCOVERY_FLOOR is not a correct extra code here "
            "(1000 < 1000 is false); the domain validator rejects it."
        ),
    ),
    _entry(
        slug="liquidity_just_below_floor",
        description="Liquidity 999.99 USD, just below the 1000 USD floor.",
        task_input=_task_input(7, liquidity=available(Decimal("999.99"))),
        expected_classification=NOT_INTERESTING,
        required_reason_codes={
            C.PRICE_AVAILABLE,
            C.LIQUIDITY_BELOW_DISCOVERY_FLOOR,
            C.VOLUME_PRESENT,
        },
        expected_data_gaps=set(),
        basis=POLICY,
        policy_hypothesis=(
            "Any liquidity strictly below the discovery floor makes the candidate "
            "NOT_INTERESTING, however close to the floor it is."
        ),
        notes="LIQUIDITY_PRESENT may appear as well.",
    ),
    _entry(
        slug="hostile_metadata_liquidity_unknown",
        description="v1 hostile metadata strings combined with UNKNOWN liquidity.",
        task_input=_task_input(
            8,
            liquidity=UNKNOWN,
            base_symbol=HOSTILE_BASE_SYMBOL,
            venue=HOSTILE_VENUE,
            provider=HOSTILE_PROVIDER,
        ),
        expected_classification=INSUFFICIENT,
        required_reason_codes={C.PRICE_AVAILABLE, C.VOLUME_PRESENT},
        expected_data_gaps={C.LIQUIDITY_UNKNOWN},
        basis=OBVIOUS,
        notes="Metadata must neither invent availability, swallow the gap nor move the class.",
    ),
)

SUITE_V2: tuple[OrbitSuiteCase, ...] = tuple(entry.case for entry in SUITE_V2_ENTRIES)
V2_ENTRIES_BY_SLUG: dict[str, OrbitSuiteV2Entry] = {e.case.slug: e for e in SUITE_V2_ENTRIES}


def entry(slug: str) -> OrbitSuiteV2Entry:
    return V2_ENTRIES_BY_SLUG[slug]

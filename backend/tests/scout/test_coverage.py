"""Discovery coverage: an offline model of the observed rate, and measured coverage.

The model replays the live observation of 2026-10-19 — about six new pools a
minute, a 300-second poll, 20 pools per page — through the same page-2 rule the
scout uses: page 2 only after a full page 1 that shares nothing with what was
already seen, and only within the daily page-2 budget.

Measured coverage needs a reference set. Without one the total is UNKNOWN,
never assumed complete.
"""

from decimal import Decimal

from src.scout.coverage import measured_coverage, simulate_discovery

DAY = 288  # five-minute polls in a day


def test_page_one_alone_misses_a_third_at_the_observed_rate():
    result = simulate_discovery(per_poll=[30] * DAY, page2=False, page2_per_day=0)
    assert result.created == 30 * DAY
    assert result.captured == 20 * DAY
    assert result.gap_polls == DAY - 1
    assert result.page2_reads == 0
    assert result.requests == DAY


def test_page_two_on_saturation_closes_every_gap_after_the_first_poll():
    result = simulate_discovery(per_poll=[30] * DAY, page2=True, page2_per_day=DAY)
    # The first poll has no reference, exactly as in the scout: 10 are missed once.
    assert result.captured == 30 * DAY - 10
    assert result.gap_polls == 0
    assert result.page2_reads == DAY - 1
    assert result.requests == DAY + DAY - 1


def test_a_page_two_budget_below_the_need_leaves_gaps():
    result = simulate_discovery(per_poll=[30] * DAY, page2=True, page2_per_day=72)
    assert result.page2_reads == 72
    assert result.gap_polls == DAY - 1 - 72
    assert result.captured == 20 * DAY + 10 * 72


def test_a_burst_beyond_two_pages_stays_a_recorded_gap():
    result = simulate_discovery(per_poll=[20, 50, 20], page2=True, page2_per_day=DAY)
    assert result.captured == 20 + 40 + 20
    assert result.gap_polls == 1


def test_a_poll_rate_that_never_fills_page_one_needs_no_page_two():
    result = simulate_discovery(per_poll=[18] * 480, page2=True, page2_per_day=DAY)
    assert result.captured == result.created
    assert result.page2_reads == 0


def test_coverage_without_a_reference_is_unknown():
    coverage = measured_coverage(reference=None, observed={"a", "b"})
    assert coverage.status == "UNKNOWN"
    assert coverage.ratio is None
    assert coverage.missing is None


def test_coverage_against_a_reference_counts_what_was_missed():
    coverage = measured_coverage(reference={"a", "b", "c", "d"}, observed={"a", "b", "c", "x"})
    assert coverage.status == "KNOWN"
    assert coverage.ratio == Decimal("0.75")
    assert coverage.missing == 1
    assert coverage.outside_reference == 1


def test_an_empty_reference_is_unknown_not_complete():
    coverage = measured_coverage(reference=set(), observed={"a"})
    assert coverage.status == "UNKNOWN"

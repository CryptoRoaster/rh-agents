from datetime import UTC, datetime, timedelta, timezone

import pytest

from src.core.clock import FixedClock, SystemClock


def test_system_clock_returns_current_utc_time():
    before = datetime.now(UTC)
    actual = SystemClock().now()
    after = datetime.now(UTC)
    assert before <= actual <= after
    assert actual.tzinfo == UTC


def test_fixed_clock_is_repeatable_and_normalizes_to_utc():
    instant = datetime(2026, 9, 9, 14, tzinfo=timezone(timedelta(hours=2)))
    clock = FixedClock(instant)
    assert clock.now() == clock.now() == datetime(2026, 9, 9, 12, tzinfo=UTC)
    assert clock.now().tzinfo == UTC


def test_fixed_clock_rejects_naive_time():
    with pytest.raises(ValueError, match="timezone-aware"):
        FixedClock(datetime(2026, 9, 9))

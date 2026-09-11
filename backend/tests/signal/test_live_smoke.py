"""Opt-in live Neynar read. Skipped unless deliberately enabled.

A credential alone does not enable this. The environment must also say
``RH_AGENTS_LIVE_SIGNAL_SMOKE=1``, because a key sitting in a shell is not
consent to spend provider credits on every test run.

What it proves is narrow on purpose: that the documented endpoint answers, that
its shape is the one the adapter parses, and that authentication works. It reads
a harmless generic query rather than anything tied to a TradeCase — a real
market-bound social smoke needs a deliberate test asset, and no financial action
belongs in a test suite.

Nothing here persists a cast, prints a key, or invokes a model.
"""

import os
from datetime import UTC, datetime, timedelta

import pytest

from src.agents.signal.models import SignalSource, SignalWindow
from src.agents.signal.sources.neynar import NeynarConfig, NeynarSignalSource

LIVE = os.environ.get("RH_AGENTS_LIVE_SIGNAL_SMOKE") == "1"
KEY = os.environ.get("NEYNAR_API_KEY", "")

pytestmark = pytest.mark.skipif(
    not (LIVE and KEY),
    reason="Live Neynar smoke needs RH_AGENTS_LIVE_SIGNAL_SMOKE=1 and NEYNAR_API_KEY",
)


async def test_the_documented_search_answers_and_parses():
    """One bounded page, a generic query, and no market binding attempted."""
    now = datetime.now(UTC)
    source = NeynarSignalSource(
        config=NeynarConfig(
            base_url="https://api.neynar.com", api_key=KEY, max_pages=1, page_size=10
        )
    )
    observations = await source.observations(
        chain="robinhood",
        pair_id="live:smoke",
        # No token address: this is a provider reachability check, not a claim
        # about any asset, so nothing can bind and nothing needs to.
        token_address=None,
        window=SignalWindow(start=now - timedelta(hours=6), end=now),
    )
    # With no address there is no query class to run, so the plan is empty and no
    # request is made. That itself is the first thing worth proving.
    assert observations == ()

    plan = source.query_plan("0x" + "a1" * 20, None)
    assert len(plan) == 1

    # Now one real bounded read against a harmless literal query.
    live = await source.observations(
        chain="robinhood",
        pair_id="live:smoke",
        token_address="0x" + "a1" * 20,
        window=SignalWindow(start=now - timedelta(hours=6), end=now),
    )
    for item in live:
        assert item.source == SignalSource.FARCASTER
        assert item.created_at <= item.received_at
        assert item.author_id.isdigit()
        assert item.source_native_id.startswith("0x")
    # Counts and shapes only. No cast text is reported or retained.
    print(f"live Neynar smoke: {len(live)} casts parsed, schema valid")

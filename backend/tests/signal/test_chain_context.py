"""Which chain a cast is talking about, and when we are entitled to say.

The whole file exists to stop one circular inference: we searched Farcaster for
our token's address, a cast came back containing it, therefore the cast is about
our token on our chain. The query cannot be evidence for its own premise, and a
20-byte address is chain-scoped — the same deployer and nonce reproduce it on
every EVM chain, which makes occupying it elsewhere cheap for anyone who wants
our sentiment reading to be about their token.
"""

import pytest

from src.agents.signal.sources.chain_context import (
    CHAIN_CONTEXT_ALGORITHM,
    addresses_in,
    resolve_chain,
)

TOKEN = "0x" + "a1" * 20
OTHER = "0x" + "b2" * 20


# ------------------------------------------------------- address extraction


@pytest.mark.parametrize("written", [TOKEN, TOKEN.upper(), "0x" + "A1" * 20])
def test_an_address_is_found_regardless_of_the_case_it_was_written_in(written):
    """Checksummed, lowercase and shouted forms are one address."""
    assert addresses_in(f"look at {written}") == frozenset({TOKEN})


def test_text_without_an_address_yields_nothing():
    assert addresses_in("DEMO is going to the moon") == frozenset()


def test_a_short_hex_string_is_not_an_address():
    assert addresses_in("0xdeadbeef is my lucky number") == frozenset()


def test_every_distinct_address_in_a_cast_is_found():
    assert addresses_in(f"{TOKEN} and {OTHER}") == frozenset({TOKEN, OTHER})


# ------------------------------------------------------------ explorer URLs


@pytest.mark.parametrize(
    ("url", "chain"),
    [
        (f"https://bscscan.com/token/{TOKEN}", "bsc"),
        (f"https://www.bscscan.com/address/{TOKEN}", "bsc"),
        (f"https://robinhoodchain.blockscout.com/token/{TOKEN}", "robinhood"),
        (f"https://robinhoodchain.blockscout.com/address/{TOKEN}", "robinhood"),
    ],
)
def test_an_allowlisted_explorer_link_places_the_address_on_its_chain(url, chain):
    assert resolve_chain(f"check this out {url}", TOKEN) == chain


@pytest.mark.parametrize(
    "url",
    [
        # A host that merely contains the name of a chain.
        "https://bsc-totally-real.example/token/{address}",
        # The right name in the wrong position.
        "https://evil.example/bscscan.com/token/{address}",
        # A suffix attack: ends with the allowlisted host and is another server.
        "https://bscscan.com.evil.example/token/{address}",
        # A link shortener, which proves nothing without dereferencing it.
        "https://t.co/abc123",
        # Plain HTTP.
        "http://bscscan.com/token/{address}",
    ],
)
def test_a_lookalike_link_grants_no_chain_context(url):
    """Including the one that spells a chain name inside its own hostname.

    Anyone can register ``bsc-totally-real.example``. If a domain could grant
    chain context by its spelling, the strongest half of an identity decision
    would belong to whoever bought the name.
    """
    assert resolve_chain(f"see {url.format(address=TOKEN)}", TOKEN) is None


def test_an_explorer_link_for_a_different_address_says_nothing_about_this_one():
    """A cast can link one token's page and mention another address in prose."""
    text = f"compare https://bscscan.com/token/{OTHER} with {TOKEN}"
    assert resolve_chain(text, TOKEN) is None


def test_an_explorer_link_without_an_address_path_is_not_context():
    assert resolve_chain(f"https://bscscan.com/blocks {TOKEN}", TOKEN) is None


def test_a_chain_name_inside_a_url_is_not_prose():
    """The same words, in a place where anyone could have put them."""
    assert resolve_chain(f"https://example.com/bnb-smart-chain/{TOKEN}", TOKEN) is None
    assert resolve_chain(f"bnb smart chain {TOKEN}", TOKEN) == "bsc"


def test_trailing_punctuation_does_not_break_a_valid_link():
    assert resolve_chain(f"here: https://bscscan.com/token/{TOKEN}.", TOKEN) == "bsc"


# ------------------------------------------------------------- chain names


@pytest.mark.parametrize(
    ("text", "chain"),
    [
        ("deployed on BNB Smart Chain", "bsc"),
        ("live on bnb chain now", "bsc"),
        ("this is a BSC token", "bsc"),
        ("binance smart chain listing", "bsc"),
        ("launched on Robinhood Chain", "robinhood"),
    ],
)
def test_an_explicit_chain_name_is_deterministic_context(text, chain):
    assert resolve_chain(f"{text} {TOKEN}", TOKEN) == chain


@pytest.mark.parametrize(
    "text",
    [
        # The asset, not the chain.
        "I swapped some BNB for this",
        # The brokerage, the app, the company — overwhelmingly not the chain.
        "saw this on Robinhood today",
        # Two letters that mean everything.
        "RH is pumping",
        # A word that merely contains an alias.
        "this is a bscx experiment",
        "welcome to bnbchainless finance",
    ],
)
def test_an_ambiguous_word_is_never_a_chain_claim(text):
    assert resolve_chain(f"{text} {TOKEN}", TOKEN) is None


def test_naming_two_supported_chains_resolves_to_neither():
    """Ambiguity resolves to nothing in every direction, never to a guess."""
    text = f"bridging {TOKEN} from BNB Smart Chain to Robinhood Chain"
    assert resolve_chain(text, TOKEN) is None


def test_an_explorer_link_contradicting_the_prose_resolves_to_nothing():
    text = f"Robinhood Chain token, see https://bscscan.com/token/{TOKEN}"
    assert resolve_chain(text, TOKEN) is None


def test_an_explorer_link_agreeing_with_the_prose_still_resolves():
    text = f"BSC token, see https://bscscan.com/token/{TOKEN}"
    assert resolve_chain(text, TOKEN) == "bsc"


def test_no_signal_at_all_resolves_to_nothing():
    assert resolve_chain(f"just bought {TOKEN}, lfg", TOKEN) is None


def test_the_algorithm_is_versioned():
    assert CHAIN_CONTEXT_ALGORITHM == "signal-chain-context-v1"

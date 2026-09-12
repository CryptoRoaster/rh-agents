"""Which chain a piece of social text is actually talking about.

This module exists because of one fact that is easy to forget: a 20-byte EVM
address is **chain-scoped**. The same deployer at the same nonce produces the
same address on every EVM chain, so occupying an address on a second chain costs
a scammer almost nothing. Farcaster search is not chain-aware, so a cast matching
a search for this TradeCase's address proves only that the string appears in it.

That makes one inference specifically forbidden: *"we searched for our token's
address, therefore this cast is about our token on our chain."* It is circular —
the query cannot be evidence for the binding, because the query is what produced
the result. Chain context must come from the cast's own content or not at all.

Two sources are accepted, both strict and both deterministic:

* an **allowlisted block-explorer URL** whose host is the canonical explorer for
  exactly one supported chain, and whose path names the address;
* an **explicit chain name** from a tiny closed alias set.

Everything else — a hostname that merely contains "bsc", a shortened link, a
user-written label, a ticker that resembles a chain — resolves to nothing. When
the content names two different supported chains, or an explorer disagrees with
the text, the answer is no context rather than a guess.
"""

import re
from urllib.parse import urlsplit

# Canonical explorer hosts, one chain each. Subdomain matching is exact: a host
# like "bscscan.com.evil.example" must never satisfy this, so hosts are compared
# in full rather than by suffix.
EXPLORER_HOSTS: dict[str, str] = {
    "bscscan.com": "bsc",
    "www.bscscan.com": "bsc",
    "robinhoodchain.blockscout.com": "robinhood",
}

# Explorer path segments that introduce a contract or account address.
ADDRESS_SEGMENTS = frozenset({"address", "token", "tokens"})

# A deliberately tiny closed alias set. Every entry names exactly one chain and
# is unlikely to appear as ordinary prose about something else.
#
# Excluded on purpose: bare "bnb" (the asset, not the chain), bare "robinhood"
# (the brokerage, the app, the company — overwhelmingly not the chain), and "rh"
# (two letters that mean everything). A chain claim that rests on those is not a
# claim worth making.
# Words that turn a nearby chain name into something other than a claim that the
# address lives there. Deliberately blunt: this is not a parser, and a false
# negative costs recall while a false positive costs a wrong identity.
NEGATING_WORDS = frozenset(
    {
        "not",
        "no",
        "never",
        "nor",
        "isn't",
        "isnt",
        "aren't",
        "arent",
        "unrelated",
        "avoid",
        "fake",
        "scam",
        "ignore",
        "wrong",
        "maybe",
        "might",
        "probably",
        "possibly",
        "perhaps",
        "unsure",
        "unclear",
        "allegedly",
        "supposedly",
        "rumour",
        "rumor",
        "if",
        "unless",
        "versus",
        "vs",
        "unlike",
        "except",
        "besides",
    }
)

# How far back to look. Four words reaches "this address is unrelated to BSC"
# without reaching the "not financial advice" that opens half of all crypto
# posts and has nothing to do with the chain named two sentences later.
NEGATION_WINDOW = 4

CHAIN_ALIASES: dict[str, str] = {
    "bnb smart chain": "bsc",
    "bnb chain": "bsc",
    "bsc": "bsc",
    "binance smart chain": "bsc",
    "robinhood chain": "robinhood",
}

# The prefix may be written either way in prose; the body is what matters.
ADDRESS = re.compile(r"0[xX][0-9a-fA-F]{40}")
URL = re.compile(r"https?://[^\s<>\"')]+", re.IGNORECASE)

CHAIN_CONTEXT_ALGORITHM = "signal-chain-context-v1"


def addresses_in(text: str) -> frozenset[str]:
    """Every 20-byte address the text contains, canonically lowercased."""
    return frozenset(match.group(0).lower() for match in ADDRESS.finditer(text))


def _explorer_chains(text: str, address: str) -> set[str]:
    """Chains named by an allowlisted explorer link pointing at this address.

    The link must be for the address in question. A cast that links one token's
    explorer page and mentions a different address in prose has not given chain
    context for the second one.
    """
    found: set[str] = set()
    for match in URL.finditer(text):
        parsed = urlsplit(match.group(0).rstrip(".,);"))
        if parsed.scheme.lower() != "https":
            continue
        host = (parsed.hostname or "").lower()
        chain = EXPLORER_HOSTS.get(host)
        if chain is None:
            continue
        segments = [segment.lower() for segment in parsed.path.split("/") if segment]
        if not any(segment in ADDRESS_SEGMENTS for segment in segments):
            continue
        if address.lower() in segments:
            found.add(chain)
    return found


def _positive_mention(lowered: str, match: re.Match[str]) -> bool:
    """Whether this occurrence reads as a claim rather than a denial or a guess.

    "not on BSC", "fake BSC contract", "avoid the BSC version" and "maybe BSC"
    all contain the word. None of them says the address is there, and a naive
    substring match would turn every one of them into a strong chain binding on
    the wrong chain — which is worse than having no context at all.

    The check is a short backward window plus a trailing question mark. It is not
    an attempt to understand the sentence, and it is not meant to be: anything it
    is unsure about falls through to no context, and an unscoped address is the
    honest record of that.
    """
    preceding = lowered[: match.start()].split()[-NEGATION_WINDOW:]
    if any(word.strip(".,;:()\"'") in NEGATING_WORDS for word in preceding):
        return False
    return not lowered[match.end() :].lstrip().startswith("?")


def _named_chains(text: str) -> set[str]:
    """Chains named explicitly and affirmatively in the prose.

    URLs are removed before matching. A hostname is not a claim: anyone can
    register ``bsc-totally-real.example``, and letting a link's spelling grant
    chain context would hand the strongest half of an identity decision to
    whoever chose the domain. Links only ever speak through the explorer
    allowlist above, where the host is compared in full.

    Two conditions, both necessary. Exactly one supported chain may be mentioned
    at all — a cast weighing "BSC or Robinhood Chain?" has named two and settled
    neither, however affirmative one of the mentions looks in isolation. And that
    single chain must be mentioned affirmatively somewhere: a text that only
    denies it has established nothing, and one that argues with itself has
    established less.
    """
    lowered = URL.sub(" ", text).lower()
    mentioned: set[str] = set()
    affirmed: set[str] = set()
    denied: set[str] = set()
    for alias, chain in CHAIN_ALIASES.items():
        for match in re.finditer(rf"(?<![0-9a-z]){re.escape(alias)}(?![0-9a-z])", lowered):
            mentioned.add(chain)
            (affirmed if _positive_mention(lowered, match) else denied).add(chain)
    if len(mentioned) != 1:
        return set()
    return affirmed - denied


def resolve_chain(text: str, address: str) -> str | None:
    """The one chain this text deterministically places the address on, or None.

    Ambiguity resolves to ``None`` in every direction. Two supported chains
    named, an explorer link that contradicts the prose, or no signal at all are
    all the same answer: we do not know, and an unscoped address is the honest
    record of that.
    """
    chains = _explorer_chains(text, address) | _named_chains(text)
    if len(chains) != 1:
        return None
    return chains.pop()

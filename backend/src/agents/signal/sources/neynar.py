"""Neynar adapter: real public Farcaster observations for SIGNAL.

Neynar is the selected Farcaster provider because its cast search documents
everything the deterministic layer needs and nothing it must not have: a source
timestamp, a stable numeric author identity, recast and reply counts, thread
parentage, literal chronological ordering and explicit time bounds.

Three provider-side choices are deliberate, and each one gives up recall on
purpose.

**Literal, chronological search only.** ``mode=semantic`` and ``hybrid`` rank by
relevance, and a ranked sample is a sample someone else selected. Chronological
literal search has a provenance we can state: these are the casts matching this
string in this interval, newest first. Anything ranked would quietly become a
sentiment prior.

**No viewer.** ``viewer_fid`` personalizes results through one account's mutes
and blocks. SIGNAL wants the unpersonalized public set, not what one wallet
would see.

**No provider-side spam filtering, and no use of the provider's user score.**
SIGNAL exists to measure duplication, author concentration, burstiness and
campaign structure. A provider that removes the spam first removes precisely the
evidence being measured. The score is carried nowhere and filters nothing.

What the adapter supplies is factual: what a cast said, who wrote it, when, and
what the text itself establishes about which chain it means. Every judgement
about whether that binds to this TradeCase stays in Phase 2F.
"""

import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from uuid import NAMESPACE_URL, UUID, uuid5

from src.agents.signal.models import (
    CollectionCoverage,
    MarketBindingBasis,
    ObservationCollection,
    ObservationEngagement,
    ObservationKind,
    SignalObservation,
    SignalSource,
    SignalWindow,
)
from src.agents.signal.ports import SignalSourceUnavailable
from src.agents.signal.sources.chain_context import addresses_in, resolve_chain
from src.agents.signal.sources.transport import (
    SignalSourceFailure,
    SignalTransportError,
    SocialTransport,
)
from src.core.clock import Clock, SystemClock

SEARCH_PATH = "v2/farcaster/cast/search"
PROVIDER = "neynar"

# Which query classes ran, and how they were built. Versioned so a later change
# to recall is auditable against the evidence it produced.
QUERY_PLAN_VERSION = "signal-neynar-query-v1"

# Farcaster cast hashes are 20-byte hex identifiers.
CAST_HASH = re.compile(r"^0x[0-9a-fA-F]{40}$")
# A deliberately small alphabet for anything interpolated into a provider query.
# Neynar's search language gives meaning to + | * " ( ) ~ - and to before:/after:,
# so a symbol is only ever sent when it cannot contain any of them: a token named
# ``A|B`` must not silently become a disjunction, and one named ``after:2020``
# must not rewrite the window.
SAFE_SYMBOL = re.compile(r"^[A-Za-z0-9]{2,16}$")

MAX_CASTS_PER_PAGE = 100
# Neynar's own cast length ceiling is well under this; anything longer is a shape
# we do not recognise rather than a post to truncate silently.
MAX_PROVIDER_TEXT = 4096


def _mapping(value: object) -> Mapping[str, object]:
    if not isinstance(value, dict):
        raise SignalTransportError(SignalSourceFailure.INVALID_RESPONSE)
    return value


def _sequence(value: object, *, limit: int) -> Sequence[object]:
    if not isinstance(value, list) or len(value) > limit:
        raise SignalTransportError(SignalSourceFailure.INVALID_RESPONSE)
    return value


def _text(value: object, *, limit: int) -> str:
    if not isinstance(value, str) or len(value) > limit:
        raise SignalTransportError(SignalSourceFailure.INVALID_RESPONSE)
    return value


def _count(value: object) -> int | None:
    """A documented engagement count, or nothing. Never a coerced default."""
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise SignalTransportError(SignalSourceFailure.INVALID_RESPONSE)
    return value


def _timestamp(value: object) -> datetime:
    if not isinstance(value, str) or len(value) > 40:
        raise SignalTransportError(SignalSourceFailure.INVALID_RESPONSE)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise SignalTransportError(SignalSourceFailure.INVALID_RESPONSE) from None
    if parsed.tzinfo is None:
        raise SignalTransportError(SignalSourceFailure.INVALID_RESPONSE)
    return parsed.astimezone(UTC)


def _bound(moment: datetime) -> str:
    """A window edge in the format the provider's ``before:``/``after:`` accepts."""
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S")


@dataclass(frozen=True)
class NeynarConfig:
    base_url: str
    api_key: str
    timeout_seconds: int = 10
    max_pages: int = 2
    page_size: int = 50

    def max_requests(self, query_classes: int) -> int:
        """The budget for the plan that will actually run.

        Derived from the query classes this TradeCase produces rather than from a
        constant. One reachable class is one class's worth of pages; reserving
        budget for a search nobody makes would overstate the cost of every
        assessment, and understating it later would be worse.
        """
        return max(1, query_classes) * self.max_pages


def transport_for(config: NeynarConfig, query_classes: int = 1) -> SocialTransport:
    # The key travels in a header, so it cannot leak through a URL in a log line,
    # a proxy access record or an exception.
    return SocialTransport(
        base_url=config.base_url,
        headers={"x-api-key": config.api_key},
        timeout_seconds=config.timeout_seconds,
        max_requests=config.max_requests(query_classes),
    )


@dataclass(frozen=True)
class QueryClass:
    """One search this plan runs, and what discovering a cast through it proves.

    Nothing, is the answer. A query class is recorded as *discovery provenance*
    so a reviewer can see how a cast was found, and it is deliberately not an
    input to binding: a cast returned by a search for our address is a cast
    containing a string, and what it means is decided from its own content.
    """

    name: str
    query: str


@dataclass(frozen=True)
class NeynarSignalSource:
    """Public Farcaster casts, normalized into Phase 2F observations."""

    config: NeynarConfig
    transport_factory: Callable[[NeynarConfig, int], SocialTransport] = field(default=transport_for)
    clock: Clock = field(default_factory=SystemClock)

    async def observations(
        self,
        *,
        chain: str,
        pair_id: str,
        token_address: str | None,
        window: SignalWindow,
    ) -> ObservationCollection:
        """Every distinct cast the plan found, normalized and deduplicated.

        A provider failure is raised as an explicit unavailability. It is never an
        empty result, because "nobody is talking about this" and "we could not
        find out" are different facts and the second must not be able to pass for
        the first.
        """
        plan = self.query_plan(token_address, None)
        if not plan:
            # Nothing identifies the asset, so there is no search to make. That
            # is an empty collection, not a provider failure, and no request is
            # sent to discover it.
            return ObservationCollection()
        transport = self.transport_factory(self.config, len(plan))
        try:
            return await self._collect(transport, plan, window, token_address)
        except SignalTransportError as error:
            raise SignalSourceUnavailable(error.failure.value) from None
        finally:
            await transport.aclose()

    def query_plan(self, token_address: str | None, symbol: str | None) -> tuple[QueryClass, ...]:
        """The bounded set of searches this phase runs. Two classes, no more.

        Recall is worth less than provenance here. A project-name search is
        deliberately absent: names are ambiguous, and Phase 2F has no
        deterministic binding rule that could consume one safely, so it would add
        observations nothing could resolve.
        """
        classes: list[QueryClass] = []
        if token_address is not None:
            classes.append(QueryClass(name="CONTRACT_ADDRESS", query=f'"{token_address}"'))
        if symbol is not None and SAFE_SYMBOL.fullmatch(symbol):
            classes.append(QueryClass(name="SYMBOL", query=f'"${symbol}"'))
        return tuple(classes)

    async def _collect(
        self,
        transport: SocialTransport,
        plan: tuple[QueryClass, ...],
        window: SignalWindow,
        token_address: str | None,
    ) -> ObservationCollection:
        found: dict[str, SignalObservation] = {}
        truncated = False
        for query_class in plan:
            casts, exhausted = await self._search(transport, query_class, window)
            truncated = truncated or not exhausted
            for cast in casts:
                observation = self._observation(cast, token_address)
                # The same cast can match several query classes. It is one post
                # by one author either way, so the first normalization wins and
                # nothing about it is counted twice.
                found.setdefault(observation.source_native_id, observation)
        return ObservationCollection(
            observations=tuple(found.values()),
            coverage=(
                CollectionCoverage.TRUNCATED_BY_LOCAL_BUDGET
                if truncated
                else CollectionCoverage.PROVIDER_RESULTS_EXHAUSTED
            ),
        )

    async def _search(
        self, transport: SocialTransport, query_class: QueryClass, window: SignalWindow
    ) -> tuple[list[Mapping[str, object]], bool]:
        """Bounded cursor paging. Returns the casts and whether the stream ended.

        A stream that ended is everything the provider had for this query. A
        stream we stopped reading is a deliberate cost decision, and saying so is
        the difference between a sample and a claim about the window.
        """
        casts: list[Mapping[str, object]] = []
        cursors: set[str] = set()
        cursor: str | None = None
        for _ in range(self.config.max_pages):
            params: dict[str, str | int] = {
                "q": (
                    f"{query_class.query} after:{_bound(window.start)} before:{_bound(window.end)}"
                ),
                # Stated rather than relied upon: both are the documented
                # defaults, and a default is a vendor's to change.
                "mode": "literal",
                "sort_type": "desc_chron",
                "limit": self.config.page_size,
            }
            if cursor is not None:
                params["cursor"] = cursor
            payload = _mapping(await transport.get_json(SEARCH_PATH, params))
            result = _mapping(payload.get("result"))
            page = [
                _mapping(item) for item in _sequence(result.get("casts"), limit=MAX_CASTS_PER_PAGE)
            ]
            casts.extend(page)
            cursor = _cursor(result.get("next"))
            if cursor is None:
                return casts, True
            if not page:
                # An empty page that still promises more is incoherent.
                raise SignalTransportError(SignalSourceFailure.PAGINATION_INCONSISTENT)
            if cursor in cursors:
                # A repeating cursor is how a paginator loops forever.
                raise SignalTransportError(SignalSourceFailure.PAGINATION_INCONSISTENT)
            cursors.add(cursor)
        # The loop ran out of pages while the provider still offered more.
        return casts, False

    def _observation(
        self, cast: Mapping[str, object], token_address: str | None
    ) -> SignalObservation:
        """One cast as a normalized observation. Facts only, no judgement."""
        cast_hash = _text(cast.get("hash"), limit=64)
        if CAST_HASH.fullmatch(cast_hash) is None:
            raise SignalTransportError(SignalSourceFailure.INVALID_RESPONSE)
        author = _mapping(cast.get("author"))
        fid = author.get("fid")
        if isinstance(fid, bool) or not isinstance(fid, int) or fid < 1:
            raise SignalTransportError(SignalSourceFailure.INVALID_RESPONSE)
        text = _text(cast.get("text"), limit=MAX_PROVIDER_TEXT)
        parent_hash = cast.get("parent_hash")
        reactions = _mapping(cast.get("reactions")) if cast.get("reactions") is not None else {}
        replies = _mapping(cast.get("replies")) if cast.get("replies") is not None else {}
        basis, address = _binding(text, token_address)
        return SignalObservation(
            observation_id=_observation_id(cast_hash),
            source=SignalSource.FARCASTER,
            # The provider's own identity for the post. Namespaced downstream by
            # Phase 2F, never compared to another platform's identifiers.
            source_native_id=cast_hash,
            # The numeric FID, not the username: usernames are rentable and
            # change, and a display name is not an identity at all.
            author_id=str(fid),
            kind=(
                ObservationKind.REPLY
                if isinstance(parent_hash, str) and parent_hash
                else ObservationKind.ORIGINAL
            ),
            created_at=_timestamp(cast.get("timestamp")),
            received_at=self.clock.now(),
            content=_bounded_content(text),
            engagement=ObservationEngagement(
                likes=_count(reactions.get("likes_count")),
                # A count of who amplified this, and nothing more. No observation
                # is synthesized from it: five hundred recasts are one post, not
                # five hundred authored positions, and inventing identities for
                # them would be inventing a crowd.
                reposts=_count(reactions.get("recasts_count")),
                replies=_count(replies.get("count")),
            ),
            referenced_observation_id=(
                _observation_id(parent_hash)
                if isinstance(parent_hash, str) and CAST_HASH.fullmatch(parent_hash)
                else None
            ),
            binding_basis=basis,
            binding_address=address,
            binding_chain=(
                resolve_chain(text, address)
                if basis == MarketBindingBasis.CONTRACT_ADDRESS_EXACT and address is not None
                else None
            ),
            provider=PROVIDER,
        )


def _binding(text: str, token_address: str | None) -> tuple[MarketBindingBasis, str | None]:
    """What the cast's own content establishes about which asset it names.

    The search that found this cast is not consulted, and neither is the
    TradeCase's chain. Either the text places the address on a chain or it does
    not, and an unscoped address is the honest record of the second case.
    """
    if token_address is None or token_address not in addresses_in(text):
        return MarketBindingBasis.UNRESOLVED, None
    if resolve_chain(text, token_address) is None:
        return MarketBindingBasis.CONTRACT_ADDRESS_UNSCOPED, token_address
    return MarketBindingBasis.CONTRACT_ADDRESS_EXACT, token_address


def _bounded_content(text: str) -> str:
    """Normalized post text within the observation bound, or an explicit marker.

    Truncation is never silent: a shortened post could read as a different
    sentiment from the one that was written, so the marker makes the cut visible
    to a reviewer and to the content fingerprint alike.
    """
    collapsed = " ".join(text.split()) or "(empty cast)"
    if len(collapsed) <= 600:
        return collapsed
    return collapsed[:585].rstrip() + " …[truncated]"


def _cursor(value: object) -> str | None:
    if value is None:
        return None
    nested = _mapping(value).get("cursor")
    if nested is None or nested == "":
        return None
    if not isinstance(nested, str) or len(nested) > 4096:
        raise SignalTransportError(SignalSourceFailure.INVALID_RESPONSE)
    return nested


def _observation_id(cast_hash: str) -> UUID:
    # Derived from the source as well as the native identifier, because a native
    # identifier only means something inside its own platform.
    return uuid5(NAMESPACE_URL, f"rh-agents:signal:{SignalSource.FARCASTER.value}:{cast_hash}")

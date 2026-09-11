"""Shared parser for the Etherscan-compatible ``getcontractcreation`` response.

Blockscout and Etherscan expose contract creation through the same module,
action and response shape, so one strict parser serves both and the two adapters
cannot drift apart in how they read a creator.

A creator is the address that deployed the contract. It is not the owner, not
the proxy admin, not the token issuer and not whoever created a pool for it.
Nothing here infers any of those.
"""

from collections.abc import Mapping

from src.agents.atlas.models import AtlasSourceFailure, OriginFacts, OriginVerification
from src.agents.atlas.sources.http import SourceRequestError
from src.agents.atlas.sources.parsing import address, mapping, sequence, tx_hash, unsigned
from src.markets.models import Availability


def parse_creation(payload: object, token_address: str, source: str) -> OriginFacts:
    """One creation record for exactly the requested contract, or a failure."""
    body = mapping(payload)
    result = body.get("result")
    if body.get("status") != "1" or not isinstance(result, list) or not result:
        # A well-formed "no data" answer is an absent fact, not a broken source.
        return OriginFacts(
            status=Availability.UNAVAILABLE,
            failure=AtlasSourceFailure.UNAVAILABLE,
            source=source,
        )
    rows = sequence(result, limit=5)
    record: Mapping[str, object] = mapping(rows[0])
    if address(record.get("contractAddress")) != token_address:
        # The provider answered about a different contract. Fail closed rather
        # than attribute another token's deployer to this one.
        raise SourceRequestError(AtlasSourceFailure.TOKEN_MISMATCH)
    factory = record.get("contractFactory")
    return OriginFacts(
        status=Availability.AVAILABLE,
        source=source,
        creator_address=address(record.get("contractCreator")),
        creation_block=unsigned(record.get("blockNumber")),
        creation_tx_hash=tx_hash(record.get("txHash")),
        # An empty string means "no factory", which is how these APIs spell it.
        factory_address=None if factory in (None, "") else address(factory),
        creator_is_contract=None,
        verification=OriginVerification.UNVERIFIED,
    )

"""Strict extraction from untrusted provider JSON.

Every provider response is treated as hostile input. Nothing is coerced: a field
that is not exactly the expected shape is a failure, not a default. This is what
keeps a malformed or malicious payload from becoming an authoritative safety
fact.
"""

import re
from collections.abc import Mapping

from src.agents.atlas.models import AtlasSourceFailure
from src.agents.atlas.sources.http import SourceRequestError

ADDRESS = re.compile(r"^0x[0-9a-fA-F]{40}$")
HASH32 = re.compile(r"^0x[0-9a-fA-F]{64}$")
UNSIGNED = re.compile(r"^(?:0|[1-9][0-9]{0,77})$")


def invalid() -> SourceRequestError:
    return SourceRequestError(AtlasSourceFailure.INVALID_RESPONSE)


def mapping(value: object) -> Mapping[str, object]:
    if not isinstance(value, dict):
        raise invalid()
    return value


def sequence(value: object, *, limit: int) -> list[object]:
    if not isinstance(value, list) or len(value) > limit:
        raise invalid()
    return value


def address(value: object) -> str:
    """A canonical lowercase 20-byte address, or a failure.

    Providers disagree about checksum casing, so the canonical lowercase form is
    the only one stored. Comparing addresses across sources must never depend on
    which vendor capitalised what.
    """
    if not isinstance(value, str) or ADDRESS.fullmatch(value) is None:
        raise invalid()
    return value.lower()


def tx_hash(value: object) -> str:
    if not isinstance(value, str) or HASH32.fullmatch(value) is None:
        raise invalid()
    return value.lower()


def unsigned(value: object) -> int:
    """A non-negative integer given as a decimal string or an int.

    Balances are uint256 and routinely exceed what a float can hold exactly, so a
    float is rejected outright rather than rounded into a safety metric.
    """
    if isinstance(value, bool):
        raise invalid()
    if isinstance(value, int):
        if value < 0:
            raise invalid()
        return value
    if isinstance(value, str) and UNSIGNED.fullmatch(value.strip()) is not None:
        return int(value.strip())
    raise invalid()


def optional_unsigned(value: object) -> int | None:
    if value is None:
        return None
    return unsigned(value)


def flag(value: object) -> bool | None:
    if value is None:
        return None
    if not isinstance(value, bool):
        raise invalid()
    return value

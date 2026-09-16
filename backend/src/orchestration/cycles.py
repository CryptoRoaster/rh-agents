"""The identity of a trading cycle, derived in exactly one place.

A cycle belongs to one case and a case to one cycle, so a cycle is identified by
the case that opened it. The column exists separately because other tables
reference the *cycle*, and referencing the case would say something subtly
different: a case that never filled is not a cycle, and the row is only written
once one actually fills.

Derived rather than generated, so a retry finds the same cycle instead of
starting a second count for one market — and derived as plain equality, so the
migration that gave historical entries their cycles could say the same thing in
portable SQL rather than in a second, differently-spelled rule.
"""

from uuid import UUID


def cycle_of(trade_case_id: UUID) -> UUID:
    """The cycle this case owns."""
    return trade_case_id

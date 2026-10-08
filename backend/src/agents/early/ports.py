"""Typed refusals of the EARLY context. Safe reason codes only, never prose."""


class EarlyContextUnavailable(Exception):
    """The context cannot be assembled, or the market is not early-eligible."""

    def __init__(self, reason_code: str) -> None:
        self.reason_code = reason_code
        super().__init__(reason_code)


class EarlyContextPending(Exception):
    """Something the producer needs is not there *yet*. A wait, never a failure."""

    def __init__(self, reason_code: str) -> None:
        self.reason_code = reason_code
        super().__init__(reason_code)

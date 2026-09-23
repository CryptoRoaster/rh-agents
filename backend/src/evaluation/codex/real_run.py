"""The only entry point that may reach a real model turn.

`CodexEvaluationClient` is the engine. It knows how to run an attempt and it
enforces everything about one attempt -- the pinned catalog, the digest, the
process ownership, the caps -- but on its own it does not know whether a *real*
run was ever cleared, and a comment saying "a real turn requires the sandbox"
is not a check.

So the real path goes through here, and this constructor cannot be satisfied by
asserting anything. It needs a `ReleaseAuthorization`, which only
`release.authorize` produces and only from a preflight where nothing is
blocking; and it re-checks that the configuration in front of it is the one that
preflight described. A caller who skips the preflight has nothing to pass.

This is the same shape as the catalog digest: the property is carried by the
types rather than by a flag someone remembers to set.
"""

from dataclasses import dataclass

from pydantic import BaseModel

from src.evaluation.codex.client import CodexClientConfig, CodexEvaluationClient
from src.evaluation.codex.models import EvaluationOutcome, EvaluationRequest
from src.evaluation.codex.release import ReleaseAuthorization


class RealRunRefused(Exception):
    """The configuration does not match what the preflight cleared."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


@dataclass
class RealCodexRunner:
    """One real attempt, behind an authorization that had to be earned."""

    authorization: ReleaseAuthorization
    config: CodexClientConfig

    def __post_init__(self) -> None:
        if self.config.outer_sandbox is None:
            # The preflight measures a profile; a configuration without one
            # would run outside the boundary that was checked.
            raise RealRunRefused("no outer sandbox configured")
        if not self.config.run_preflight:
            raise RealRunRefused("login preflight disabled")
        self._client = CodexEvaluationClient(config=self.config)

    @property
    def exec_starts(self) -> int:
        return self._client.exec_starts

    async def evaluate[Output: BaseModel](
        self, request: EvaluationRequest[Output]
    ) -> EvaluationOutcome[Output]:
        return await self._client.evaluate(request)


__all__ = ["RealCodexRunner", "RealRunRefused"]

"""The only entry point that may reach a real model turn.

`CodexEvaluationClient` is the engine. It knows how to run an attempt and it
enforces everything about one attempt -- the pinned catalog, the digest, the
process ownership, the caps. What it does not know is whether a *real* run was
ever cleared, and a comment saying "a real turn requires the sandbox" is not a
check.

So the real path goes through here, and this constructor cannot be satisfied by
asserting anything. It needs a `ReleaseAuthorization`, which `release.authorize`
produces only from a preflight of the required shape with nothing blocking, and
it compares the configuration in front of it against the `RunBinding` that
authorization carries, field by field. A cleared preflight for one workspace,
one isolated home, one set of sandbox roots and one profile does not authorise a
run with different ones.

The comparison happens twice on purpose: here, so a mismatch is a loud refusal
with a named field before anything exists, and again inside the client through
the permit, so the check cannot be lost by constructing the client another way.
"""

from dataclasses import dataclass, field

from pydantic import BaseModel

from src.evaluation.codex.client import CodexClientConfig, CodexEvaluationClient, binding_for
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
    _client: CodexEvaluationClient = field(init=False)

    def __post_init__(self) -> None:
        if self.config.outer_sandbox is None:
            # The preflight measures a profile applied to concrete roots. A
            # configuration without one is not the thing that was measured.
            raise RealRunRefused("no outer sandbox configured")
        if self.config.outer_profile is None:
            raise RealRunRefused("no bound profile")
        if not self.config.run_preflight:
            raise RealRunRefused("login preflight disabled")
        if not self.config.outer_profile.still_matches():
            # The bytes on disk are no longer the bytes that were measured.
            raise RealRunRefused("profile digest mismatch")

        expected = self.authorization.binding
        actual = binding_for(self.config, self.config.outer_profile.digest)
        drifted = expected.differences(actual)
        if drifted:
            raise RealRunRefused(f"configuration drifted: {', '.join(drifted)}")

        self._client = CodexEvaluationClient(config=self.config, permit=self.authorization.permit())

    @property
    def exec_starts(self) -> int:
        return self._client.exec_starts

    @property
    def version_starts(self) -> int:
        return self._client.version_starts

    @property
    def preflight_starts(self) -> int:
        return self._client.preflight_starts

    async def evaluate[Output: BaseModel](
        self, request: EvaluationRequest[Output]
    ) -> EvaluationOutcome[Output]:
        return await self._client.evaluate(request)


__all__ = ["RealCodexRunner", "RealRunRefused"]

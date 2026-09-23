"""What a failure may say, and what it may never say.

The first authorised real run exited 1 after 375 ms with no events, and the
harness had nothing further to report: `run_bounded` captured the child's
stderr, `_run_attempt` used only the exit code, and the bytes were dropped. A
failure nobody can diagnose gets retried blindly, which is the one outcome this
harness exists to prevent.

Handing the raw tail out would have been the wrong repair, so these tests are
in two halves. One half checks that something useful now survives. The other
checks that the things which must not survive -- tokens, cookies, headers, the
user's absolute paths -- do not, including on the path where the redactor
itself fails.
"""

import pytest

from src.evaluation.codex.diagnostics import (
    MASKED_VALUE,
    MAX_LINE_CHARS,
    MAX_LINES,
    REDACTED_LINE,
    REDACTION_FAILED,
    PathAliases,
    ProcessDiagnostic,
    diagnose,
    redact,
)
from src.evaluation.codex.models import EvaluationFailure, EvaluationRejected
from tests.evaluation.conftest import Probe

# Values that must never appear in any reported outcome. Deliberately
# distinctive so a substring check over the whole repr is meaningful.
SECRETS = (
    "SUPER_SECRET_VALUE",
    "SUPER_REFRESH_SECRET",
    "SUPER_ACCESS_SECRET",
    "SUPER_COOKIE",
    "SUPER_APIKEY_VALUE",
    "SUPER_PASSWORD_VALUE",
)

SECRET_LINES = (
    "Authorization: Bearer SUPER_SECRET_VALUE",
    "refresh_token=SUPER_REFRESH_SECRET",
    '{"access_token":"SUPER_ACCESS_SECRET"}',
    "Cookie: session=SUPER_COOKIE",
    "Set-Cookie: sid=SUPER_COOKIE",
    "api_key=SUPER_APIKEY_VALUE",
    "password: SUPER_PASSWORD_VALUE",
    "id_token SUPER_ACCESS_SECRET",
    "session_token SUPER_ACCESS_SECRET",
)

# A structurally valid JWT whose payload decodes to nothing meaningful. It is
# here to prove shape-based masking, not to carry anything.
JWT_LIKE = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJub3RhcmVhbHRva2VuIn0.c2lnbmF0dXJlX3BsYWNlaG9sZGVy"


def test_a_line_naming_a_credential_is_dropped_whole() -> None:
    """Dropped, not edited: editing assumes the shape of the value.

    A shape assumption is exactly what fails on the error message nobody
    anticipated, so any line mentioning a credential term is replaced entirely.
    A consecutive run of them collapses into one marker, because nine identical
    placeholders say no more than one and would spend the line budget the
    useful message needs.
    """
    lines = redact("\n".join(SECRET_LINES).encode("utf-8"))
    assert lines == (REDACTED_LINE,)
    for secret in SECRETS:
        assert secret not in "\n".join(lines)

    # Separated runs stay separated, so the position of the safe line is not lost.
    mixed = redact(b"Authorization: Bearer X\nplain one\nCookie: c=Y\nplain two")
    assert mixed == (REDACTED_LINE, "plain one", REDACTED_LINE, "plain two")


@pytest.mark.parametrize("term", ["AUTHORIZATION", "Bearer", "ApiKey", "SET-COOKIE", "Secret"])
def test_the_term_match_ignores_case(term: str) -> None:
    assert redact(f"{term} leaked-value-here".encode()) == (REDACTED_LINE,)


def test_a_token_shaped_value_is_masked_even_on_an_innocent_line() -> None:
    """Defensive second pass, for the line that names no credential at all."""
    assert redact(f"unexpected response {JWT_LIKE}".encode()) == (
        f"unexpected response {MASKED_VALUE}",
    )
    opaque = "A" * 64
    assert redact(f"trace id {opaque}".encode()) == (f"trace id {MASKED_VALUE}",)


def test_bound_paths_become_roles() -> None:
    """A diagnostic is about behaviour, not about where this machine keeps things."""
    aliases = PathAliases.build(home="/Users/someone", codex_home="/Users/someone/tmp/codex-home")
    line = "failed to read /Users/someone/tmp/codex-home/auth.json under /Users/someone"
    redacted = redact(line.encode("utf-8"), aliases)
    assert redacted == ("failed to read <CODEX_HOME>/auth.json under <HOME>",)
    # Longest first, so the shorter containing path does not eat the longer one.
    assert "/Users/someone" not in redacted[0]


def test_ansi_escapes_and_blank_lines_are_removed() -> None:
    raw = "\x1b[31mred error\x1b[0m\n\n   \n\x1b[1mbold\x1b[0m\n"
    assert redact(raw.encode("utf-8")) == ("red error", "bold")


def test_invalid_utf8_does_not_become_a_second_failure() -> None:
    assert redact(b"broken \xff\xfe bytes") == ("broken �� bytes",)


def test_the_output_is_bounded_in_lines_and_in_width() -> None:
    raw = "\n".join(f"line {index} " + "x" * 500 for index in range(50))
    lines = redact(raw.encode("utf-8"))
    assert len(lines) <= MAX_LINES
    assert all(len(line) <= MAX_LINE_CHARS for line in lines)


def test_redaction_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    """If the redactor itself breaks, nothing of the input comes through."""
    import src.evaluation.codex.diagnostics as module

    def explode(line: str) -> str:
        raise RuntimeError("redaction is broken")

    monkeypatch.setattr(module, "_mask", explode)
    assert module.redact(b"harmless looking line") == (REDACTION_FAILED,)

    monkeypatch.setattr(module, "redact", explode)
    result = module.diagnose(
        exit_code=1, stderr_tail=b"SUPER_SECRET_VALUE", captured_limit=0, aliases=PathAliases()
    )
    assert result.safe_lines == (REDACTION_FAILED,)
    assert "SUPER_SECRET_VALUE" not in repr(result)


def test_the_diagnostic_has_no_raw_field() -> None:
    """The type itself must not offer a way to carry the bytes."""
    fields = set(ProcessDiagnostic.__dataclass_fields__)
    assert fields == {
        "exit_code",
        "stderr_present",
        "stderr_captured_bytes",
        "stderr_was_truncated",
        "safe_lines",
    }
    assert not any("raw" in name or "stderr_tail" == name for name in fields)


def test_counts_survive_even_when_every_line_is_redacted() -> None:
    """Otherwise a fully redacted failure looks like a silent one."""
    tail = "\n".join(SECRET_LINES).encode("utf-8")
    diagnostic = diagnose(
        exit_code=1, stderr_tail=tail, captured_limit=len(tail), aliases=PathAliases()
    )
    assert diagnostic.stderr_present is True
    assert diagnostic.stderr_captured_bytes == len(tail)
    assert diagnostic.stderr_was_truncated is True
    assert diagnostic.safe_lines == (REDACTED_LINE,)


# --------------------------------------------------------------------------
# End to end, through a real child process
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_start_failure_is_classified_as_such(probe: Probe) -> None:
    """Exit non-zero with no events is a failed start, not a missing completion.

    The previous ordering answered "the turn did not complete", which is true
    and useless: it is what the first authorised real run reported, and it is
    why that failure could not be diagnosed.
    """
    probe.scenario("fails_before_turn", exit_code=1, stderr_lines=["error: could not start"])
    outcome = await probe.client().evaluate(probe.request())
    assert isinstance(outcome, EvaluationRejected)
    assert outcome.reason is EvaluationFailure.PROCESS_FAILED
    assert outcome.detail_code == "EXIT_1_BEFORE_TURN"
    assert outcome.diagnostic is not None
    assert outcome.diagnostic.exit_code == 1
    assert outcome.diagnostic.safe_lines == ("error: could not start",)


@pytest.mark.asyncio
async def test_no_secret_from_a_failing_child_reaches_the_outcome(probe: Probe) -> None:
    """The whole point. Every one of these is a shape a real CLI error uses."""
    probe.scenario(
        "fails_before_turn",
        exit_code=7,
        stderr_lines=[*SECRET_LINES, f"token {JWT_LIKE}", "plain diagnostic line"],
    )
    outcome = await probe.client().evaluate(probe.request())
    assert isinstance(outcome, EvaluationRejected)
    assert outcome.detail_code == "EXIT_7_BEFORE_TURN"

    rendered = repr(outcome)
    for secret in SECRETS:
        assert secret not in rendered
    assert JWT_LIKE not in rendered
    # Something useful did survive, so this is not passing by reporting nothing.
    assert "plain diagnostic line" in rendered
    assert outcome.diagnostic is not None
    assert outcome.diagnostic.stderr_present is True


@pytest.mark.asyncio
async def test_a_completed_turn_still_carries_no_diagnostic(probe: Probe) -> None:
    """Success needs no stderr dump, and does not get one."""
    probe.scenario("success")
    outcome = await probe.client().evaluate(probe.request())
    assert not hasattr(outcome, "diagnostic")


@pytest.mark.asyncio
async def test_a_rejection_before_any_process_has_nothing_to_diagnose(probe: Probe) -> None:
    """No child ran, so there is no stderr and the field says so rather than lying."""
    probe.scenario("success")
    client = probe.client(effort="nonsense")
    outcome = await client.evaluate(probe.request())
    assert isinstance(outcome, EvaluationRejected)
    assert outcome.reason is EvaluationFailure.EFFORT_NOT_SUPPORTED
    assert outcome.diagnostic is None
    assert client.exec_starts == 0


@pytest.mark.asyncio
async def test_a_failing_exit_after_a_completed_turn_keeps_its_meaning(probe: Probe) -> None:
    """The new branch is narrow: a started turn is still judged by the events."""
    probe.scenario("exit_nonzero")
    outcome = await probe.client().evaluate(probe.request())
    assert isinstance(outcome, EvaluationRejected)
    assert outcome.reason is EvaluationFailure.INCONSISTENT_COMPLETION
    assert outcome.detail_code == "EXIT_3"

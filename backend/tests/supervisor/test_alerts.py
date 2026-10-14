"""Alerting: when a message goes out, what it says, and what a failure changes.

No test here talks to Telegram. The transport is a recorder, or httpx's
`MockTransport` where the Telegram request itself is the subject.
"""

import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from src.supervisor.alerts import (
    AlertKind,
    AlertPassBusy,
    AlertState,
    Observation,
    TelegramTransport,
    TransportUnavailable,
    apply,
    decide,
    load_alert_state,
    message,
    observe,
    read_secret_file,
    run_alert_pass,
    save_alert_state,
    telegram_from_environment,
    transport_from_environment,
)

T0 = datetime(2026, 10, 14, 8, tzinfo=UTC)
TOKEN = "123456789:" + "A" * 35
CHAT = "987654321"


def seen(status: str, *reasons: str, at: datetime = T0, positions: int | None = 0) -> Observation:
    return Observation(status, reasons, positions, at)


class Recorder:
    def __init__(self, failure: str | None = None) -> None:
        self.sent: list[str] = []
        self.failure = failure

    def send(self, text: str) -> str | None:
        self.sent.append(text)
        return self.failure


def run(tmp_path: Path, observation: Observation, transport=None, **options):
    return run_alert_pass(
        tmp_path, observation, transport=transport or Recorder(), host="rh-vps", **options
    )


# Decisions ---------------------------------------------------------------------


def test_a_first_healthy_verdict_is_a_silent_baseline():
    decision = decide(AlertState(), seen("HEALTHY"))
    assert decision.kind is None and decision.baseline


def test_a_first_verdict_that_is_not_healthy_alerts():
    assert decide(AlertState(), seen("STOPPED")).kind is AlertKind.ALERT


@pytest.mark.parametrize(
    ("before", "after", "kind"),
    [
        ("HEALTHY", "DEGRADED", AlertKind.ALERT),
        ("HEALTHY", "CRITICAL", AlertKind.ALERT),
        ("HEALTHY", "STOPPED", AlertKind.ALERT),
        ("DEGRADED", "CRITICAL", AlertKind.ESCALATION),
        ("STOPPED", "CRITICAL", AlertKind.ESCALATION),
        ("CRITICAL", "DEGRADED", AlertKind.ALERT),
    ],
)
def test_every_change_of_status_alerts(before, after, kind):
    state = AlertState(notified_status=before, notified_at=T0 - timedelta(minutes=1))
    assert decide(state, seen(after)).kind is kind


def test_an_unchanged_status_repeats_only_after_thirty_minutes():
    state = AlertState(
        notified_status="CRITICAL",
        notified_reasons=("OPEN_POSITIONS_EXIT_STALE:EXIT_JOB_TOO_OLD",),
        notified_at=T0,
    )
    same = ("OPEN_POSITIONS_EXIT_STALE:EXIT_JOB_TOO_OLD",)
    early = T0 + timedelta(minutes=30) - timedelta(seconds=1)
    late = T0 + timedelta(minutes=30)
    assert decide(state, seen("CRITICAL", *same, at=early)).kind is None
    assert decide(state, seen("CRITICAL", *same, at=late)).kind is AlertKind.REPEAT


def test_a_stopped_status_is_not_repeated():
    state = AlertState(notified_status="STOPPED", notified_at=T0)
    assert decide(state, seen("STOPPED", at=T0 + timedelta(hours=5))).kind is None


def test_a_new_kind_of_reason_is_news_but_a_new_count_is_not():
    state = AlertState(
        notified_status="DEGRADED", notified_reasons=("CONSECUTIVE_FAILURES:1",), notified_at=T0
    )
    later = T0 + timedelta(minutes=2)
    assert decide(state, seen("DEGRADED", "CONSECUTIVE_FAILURES:4", at=later)).kind is None
    changed = decide(
        state, seen("DEGRADED", "CONSECUTIVE_FAILURES:4", "TICK_GAP_DETECTED", at=later)
    )
    assert changed.kind is AlertKind.CHANGE


def test_one_healthy_pass_after_an_alert_is_no_recovery():
    state = AlertState(notified_status="CRITICAL", notified_at=T0)
    first = decide(state, seen("HEALTHY", at=T0 + timedelta(minutes=1)))
    assert first.kind is None
    state = apply(state, first, delivered=False)
    second = decide(state, seen("HEALTHY", at=T0 + timedelta(minutes=2)))
    assert second.kind is AlertKind.RECOVERY


def test_a_flap_back_to_the_alerted_status_neither_recovers_nor_realerts():
    state = AlertState(notified_status="CRITICAL", notified_at=T0)
    state = apply(
        state, decide(state, seen("HEALTHY", at=T0 + timedelta(minutes=1))), delivered=False
    )
    flap = decide(state, seen("CRITICAL", at=T0 + timedelta(minutes=2)))
    assert flap.kind is None
    state = apply(state, flap, delivered=False)
    assert state.healthy_streak == 0
    assert decide(state, seen("HEALTHY", at=T0 + timedelta(minutes=3))).kind is None


def test_no_recovery_without_a_delivered_alert():
    baseline = AlertState(notified_status="HEALTHY", notified_at=T0, healthy_streak=5)
    assert decide(baseline, seen("HEALTHY", at=T0 + timedelta(minutes=9))).kind is None


def test_five_deliveries_in_an_hour_leave_room_for_a_sixth():
    sent = tuple(T0 - timedelta(minutes=50 - index) for index in range(5))
    state = AlertState(notified_status="HEALTHY", notified_at=T0, sent_at=sent)
    assert not decide(state, seen("CRITICAL")).capped


def test_the_hourly_cap_holds_messages_back_and_counts_them():
    # Six deliveries in the last hour: the cap.
    sent = tuple(T0 - timedelta(minutes=50 - index) for index in range(6))
    state = AlertState(notified_status="HEALTHY", notified_at=T0, sent_at=sent)
    decision = decide(state, seen("DEGRADED"))
    assert decision.kind is AlertKind.ALERT and decision.capped
    after = apply(state, decision, delivered=False)
    assert after.suppressed == 1 and after.notified_status == "HEALTHY"
    # Once the oldest delivery is an hour old, the news is sent.
    later = seen("DEGRADED", at=T0 + timedelta(minutes=11))
    assert not decide(after, later).capped


def test_a_failed_delivery_changes_nothing_that_was_delivered():
    state = AlertState(notified_status="HEALTHY", notified_at=T0)
    decision = decide(state, seen("CRITICAL"))
    after = apply(state, decision, delivered=False, failure="SEND_TIMEOUT")
    assert after.notified_status == "HEALTHY"
    assert after.send_failures == 1 and after.last_failure == "SEND_TIMEOUT"
    # Still news on the next pass.
    assert decide(after, seen("CRITICAL", at=T0 + timedelta(minutes=1))).kind is AlertKind.ALERT


def test_unknown_or_unreadable_health_is_critical():
    assert observe({"status": "MAYBE"}, now=T0).status == "CRITICAL"
    assert observe({"status": "MAYBE"}, now=T0).reasons[0] == "HEALTH_STATUS_UNKNOWN"
    assert observe(None, now=T0).status == "CRITICAL"
    assert observe("HEALTHY", now=T0).status == "CRITICAL"


# Message contents --------------------------------------------------------------


def test_a_message_carries_codes_counts_and_an_instant_only():
    document = {
        "status": "CRITICAL",
        "reasons": [
            "OPEN_POSITIONS_EXIT_STALE:EXIT_JOB_TOO_OLD",
            "postgresql+asyncpg://user:secret@db/rh",
            f"token {TOKEN}",
            "Traceback (most recent call last)",
        ],
        "open_positions": 2,
    }
    observation = observe(document, now=T0)
    text = message(decide(AlertState(), observation), host="rh-vps")
    assert text.splitlines() == [
        "RH-AGENTS ALERT: CRITICAL",
        "host: rh-vps",
        "reasons: OPEN_POSITIONS_EXIT_STALE:EXIT_JOB_TOO_OLD",
        "open positions: 2",
        "at: 2026-10-14T08:00:00Z",
    ]
    for forbidden in ("secret", "postgresql", TOKEN, "Traceback"):
        assert forbidden not in text


def test_a_host_that_is_not_a_plain_alias_is_never_printed():
    decision = decide(AlertState(), seen("CRITICAL"))
    for host in ("10.0.0.7", "vps.example.com", "My Host", ""):
        assert "host: unnamed-host" in message(decision, host=host)


# Persistence and one pass --------------------------------------------------------


def test_the_state_survives_a_restart_and_suppresses_a_repeat(tmp_path):
    transport = Recorder()
    run(tmp_path, seen("CRITICAL", "OPEN_POSITIONS_SUPERVISOR_NOT_RUNNING"), transport)
    # A new process, a minute later: nothing new to say.
    outcome = run(
        tmp_path,
        seen("CRITICAL", "OPEN_POSITIONS_SUPERVISOR_NOT_RUNNING", at=T0 + timedelta(minutes=1)),
        transport,
    )
    assert len(transport.sent) == 1 and outcome.kind is None
    state = load_alert_state(tmp_path)
    assert state is not None and state.notified_status == "CRITICAL"


def test_the_state_file_is_owner_only_and_round_trips(tmp_path):
    state = AlertState(
        notified_status="DEGRADED",
        notified_reasons=("TICK_GAP_DETECTED",),
        notified_at=T0,
        sent_at=(T0,),
        healthy_streak=0,
        send_failures=2,
        last_failure="SEND_TIMEOUT",
        suppressed=1,
    )
    save_alert_state(tmp_path, state)
    assert load_alert_state(tmp_path) == state
    assert (tmp_path / "alert-state.json").stat().st_mode & 0o777 == 0o600


def test_an_unreadable_state_starts_over_and_says_so(tmp_path):
    (tmp_path / "alert-state.json").write_text("{not json")
    transport = Recorder()
    outcome = run(tmp_path, seen("CRITICAL"), transport)
    assert outcome.state_reset and outcome.sent and len(transport.sent) == 1


def test_a_failed_send_is_retried_on_the_next_pass(tmp_path):
    failing = Recorder(failure="SEND_HTTP_502")
    first = run(tmp_path, seen("CRITICAL"), failing)
    assert not first.sent and first.failure == "SEND_HTTP_502"
    working = Recorder()
    second = run(tmp_path, seen("CRITICAL", at=T0 + timedelta(minutes=1)), working)
    assert second.sent and len(working.sent) == 1


def test_without_a_transport_nothing_is_marked_delivered(tmp_path):
    outcome = run_alert_pass(tmp_path, seen("CRITICAL"), transport=None, host="rh-vps")
    assert outcome.failure == "ALERT_TRANSPORT_NOT_CONFIGURED" and not outcome.sent
    state = load_alert_state(tmp_path)
    assert state is not None and state.notified_status is None


def test_a_dry_run_shows_the_text_and_records_nothing(tmp_path):
    transport = Recorder()
    outcome = run(tmp_path, seen("CRITICAL"), transport, dry_run=True)
    assert transport.sent == [] and outcome.dry_run_text is not None
    assert "RH-AGENTS ALERT: CRITICAL" in outcome.dry_run_text
    assert load_alert_state(tmp_path) is None


def test_one_alert_pass_at_a_time(tmp_path):
    import fcntl

    descriptor = os.open(tmp_path / "alert.lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        with pytest.raises(AlertPassBusy):
            run(tmp_path, seen("CRITICAL"))
    finally:
        os.close(descriptor)


# Telegram ----------------------------------------------------------------------


def telegram(handler) -> TelegramTransport:
    return TelegramTransport(
        TOKEN, CHAT, client=lambda: httpx.Client(transport=httpx.MockTransport(handler))
    )


def test_telegram_posts_the_text_to_one_chat():
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"ok": True})

    assert telegram(handler).send("RH-AGENTS ALERT: CRITICAL") is None
    (request,) = requests
    assert request.url.path == f"/bot{TOKEN}/sendMessage"
    body = json.loads(request.content)
    assert body == {
        "chat_id": CHAT,
        "text": "RH-AGENTS ALERT: CRITICAL",
        "disable_web_page_preview": True,
    }


@pytest.mark.parametrize(
    ("response", "code"),
    [
        (httpx.Response(500), "SEND_HTTP_500"),
        (httpx.Response(429), "SEND_RATE_LIMITED"),
        (httpx.Response(200, json={"ok": False}), "SEND_NOT_ACCEPTED"),
        (httpx.Response(200, text="not json"), "SEND_NOT_ACCEPTED"),
    ],
)
def test_telegram_failures_are_codes(response, code):
    assert telegram(lambda request: response).send("x") == code


def test_a_transport_error_never_leaks_the_token(capsys):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"cannot reach {request.url}")

    def timeout(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout(f"slow {request.url}")

    assert telegram(handler).send("x") == "SEND_FAILED"
    assert telegram(timeout).send("x") == "SEND_TIMEOUT"
    transport = telegram(handler)
    assert TOKEN not in repr(transport)
    captured = capsys.readouterr()
    assert TOKEN not in captured.out + captured.err


@pytest.mark.parametrize(
    ("token", "chat"), [("not-a-token", CHAT), (TOKEN, "@channel"), (TOKEN, "12; rm")]
)
def test_malformed_telegram_settings_are_refused(token, chat):
    with pytest.raises(TransportUnavailable):
        TelegramTransport(token, chat)


def secret(path: Path, value: str, mode: int = 0o600) -> Path:
    path.write_text(value + "\n")
    path.chmod(mode)
    return path


def test_secret_files_must_be_owner_only(tmp_path):
    loose = secret(tmp_path / "token", TOKEN, 0o644)
    with pytest.raises(TransportUnavailable) as refused:
        read_secret_file(loose)
    assert refused.value.code == "SECRET_FILE_PERMISSIONS_TOO_OPEN"
    assert TOKEN not in str(refused.value)
    with pytest.raises(TransportUnavailable) as missing:
        read_secret_file(tmp_path / "absent")
    assert missing.value.code == "SECRET_FILE_MISSING"


def test_telegram_reads_systemd_credentials_by_default(tmp_path):
    secret(tmp_path / "telegram-token", TOKEN)
    secret(tmp_path / "telegram-chat-id", CHAT)
    transport = transport_from_environment(
        {"RH_AGENTS_ALERT_TRANSPORT": "telegram", "CREDENTIALS_DIRECTORY": str(tmp_path)}
    )
    assert isinstance(transport, TelegramTransport)


@pytest.mark.parametrize(
    ("env", "code"),
    [
        ({}, "ALERT_TRANSPORT_NOT_CONFIGURED"),
        ({"RH_AGENTS_ALERT_TRANSPORT": "pager"}, "ALERT_TRANSPORT_UNKNOWN"),
        ({"RH_AGENTS_ALERT_TRANSPORT": "telegram"}, "TELEGRAM_NOT_CONFIGURED"),
    ],
)
def test_a_missing_transport_is_a_refusal_code(env, code):
    with pytest.raises(TransportUnavailable) as refused:
        transport_from_environment(env)
    assert refused.value.code == code


def test_telegram_from_explicit_files(tmp_path):
    env = {
        "RH_AGENTS_ALERT_TELEGRAM_TOKEN_FILE": str(secret(tmp_path / "t", TOKEN)),
        "RH_AGENTS_ALERT_TELEGRAM_CHAT_ID_FILE": str(secret(tmp_path / "c", CHAT)),
    }
    assert isinstance(telegram_from_environment(env), TelegramTransport)


# Review follow-ups ---------------------------------------------------------------


def test_a_systemd_credential_with_an_acl_mask_is_accepted(tmp_path, monkeypatch):
    # LoadCredential= gives the file 0400 plus an ACL for the service user; the
    # ACL mask shows up as group bits.
    monkeypatch.setenv("CREDENTIALS_DIRECTORY", str(tmp_path))
    token = secret(tmp_path / "telegram-token", TOKEN, 0o440)
    assert read_secret_file(token) == TOKEN
    token.chmod(0o444)
    with pytest.raises(TransportUnavailable):
        read_secret_file(token)


def test_group_bits_outside_the_credentials_directory_still_refuse(tmp_path, monkeypatch):
    monkeypatch.setenv("CREDENTIALS_DIRECTORY", str(tmp_path / "credentials"))
    with pytest.raises(TransportUnavailable):
        read_secret_file(secret(tmp_path / "token", TOKEN, 0o440))


def test_a_move_into_critical_is_never_held_back_by_the_cap():
    sent = tuple(T0 - timedelta(minutes=10 - index) for index in range(6))
    for notified in ("HEALTHY", "DEGRADED"):
        state = AlertState(notified_status=notified, notified_at=T0, sent_at=sent)
        decision = decide(state, seen("CRITICAL", "OPEN_POSITIONS_SUPERVISOR_NOT_RUNNING"))
        assert decision.kind is not None and not decision.capped
    repeat = AlertState(
        notified_status="CRITICAL", notified_at=T0 - timedelta(hours=1), sent_at=sent
    )
    assert decide(repeat, seen("CRITICAL")).capped


def test_what_the_cap_held_back_is_reported(tmp_path):
    state = AlertState(notified_status="HEALTHY", notified_at=T0, suppressed=3)
    save_alert_state(tmp_path, state)
    transport = Recorder()
    outcome = run(tmp_path, seen("DEGRADED", "TICK_GAP_DETECTED"), transport)
    assert outcome.sent and "held back by the hourly cap: 3" in transport.sent[0]
    assert outcome.suppressed == 0


def test_deliveries_in_the_future_do_not_hold_the_cap_shut():
    future = tuple(T0 + timedelta(minutes=index + 1) for index in range(6))
    state = AlertState(notified_status="HEALTHY", notified_at=T0, sent_at=future)
    assert not decide(state, seen("DEGRADED")).capped


def test_a_notification_in_the_future_does_not_silence_repeats():
    state = AlertState(notified_status="CRITICAL", notified_at=T0 + timedelta(hours=2))
    assert decide(state, seen("CRITICAL")).kind is AlertKind.REPEAT


def test_a_state_that_cannot_be_read_sends_nothing(tmp_path):
    (tmp_path / "alert-state.json").mkdir()
    transport = Recorder()
    outcome = run(tmp_path, seen("CRITICAL"), transport)
    assert transport.sent == [] and outcome.failure == "ALERT_STATE_UNREADABLE"

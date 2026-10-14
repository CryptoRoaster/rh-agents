"""The dead-man's switch: signed heartbeats, replay protection, and the outside verdict."""

import json
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from src.supervisor import deadman
from src.supervisor.alerts import TransportUnavailable
from src.supervisor.deadman import (
    MAX_SKEW_SECONDS,
    HeartbeatRejected,
    ReceiverState,
    judge,
    load_key,
    load_receiver_state,
    receive,
    sign,
    verify,
)

KEY = bytes(range(32))
OTHER_KEY = bytes(range(1, 33))
T0 = datetime(2026, 10, 14, 8, tzinfo=UTC)
BACKEND = Path(__file__).resolve().parents[2]


def beat(at: datetime = T0, *, key: bytes = KEY, host: str = "rh-vps", status: str = "HEALTHY"):
    return sign(key, host=host, status=status, now=at)


def check(line, *, now: datetime = T0, last: datetime | None = None, host: str = "rh-vps"):
    return verify(line, KEY, expected_host=host, now=now, last_sent_at=last)


def test_a_signed_heartbeat_verifies():
    heartbeat = check(beat())
    assert heartbeat.host == "rh-vps" and heartbeat.status == "HEALTHY"
    assert heartbeat.sent_at == T0


@pytest.mark.parametrize(
    ("line", "code"),
    [
        (beat(key=OTHER_KEY), "HEARTBEAT_BAD_SIGNATURE"),
        (beat(host="other-host"), "HEARTBEAT_WRONG_HOST"),
        (beat(T0 + timedelta(seconds=MAX_SKEW_SECONDS + 1)), "HEARTBEAT_OUTSIDE_WINDOW"),
        (beat(T0 - timedelta(seconds=MAX_SKEW_SECONDS + 1)), "HEARTBEAT_OUTSIDE_WINDOW"),
        ("not json", "HEARTBEAT_MALFORMED"),
        ("[]", "HEARTBEAT_MALFORMED"),
        ("x" * 2000, "HEARTBEAT_TOO_LARGE"),
    ],
)
def test_a_heartbeat_that_fails_any_check_is_rejected(line, code):
    with pytest.raises(HeartbeatRejected) as rejected:
        check(line)
    assert rejected.value.code == code


def test_a_tampered_status_breaks_the_signature():
    document = json.loads(beat(status="CRITICAL"))
    document["payload"]["status"] = "HEALTHY"
    with pytest.raises(HeartbeatRejected) as rejected:
        check(json.dumps(document))
    assert rejected.value.code == "HEARTBEAT_BAD_SIGNATURE"


def test_the_same_heartbeat_is_never_accepted_twice():
    line = beat()
    with pytest.raises(HeartbeatRejected) as rejected:
        check(line, now=T0 + timedelta(seconds=5), last=T0)
    assert rejected.value.code == "HEARTBEAT_REPLAYED"


def test_an_older_heartbeat_cannot_be_reordered_in():
    older = beat(T0 - timedelta(seconds=30))
    with pytest.raises(HeartbeatRejected) as rejected:
        check(older, last=T0)
    assert rejected.value.code == "HEARTBEAT_REPLAYED"


def test_an_unknown_reported_status_is_critical():
    assert check(beat(status="WHATEVER")).status == "CRITICAL"


def test_the_receiver_records_and_survives_a_restart(tmp_path):
    receive(tmp_path, beat(), KEY, expected_host="rh-vps", now=T0)
    # A new receiver process: the replay is still refused.
    with pytest.raises(HeartbeatRejected):
        receive(tmp_path, beat(), KEY, expected_host="rh-vps", now=T0 + timedelta(seconds=1))
    state = load_receiver_state(tmp_path)
    assert state.last_sent_at == T0 and state.last_received_at == T0
    assert state.rejected == 1 and state.last_rejection == "HEARTBEAT_REPLAYED"
    assert (tmp_path / "heartbeat-state.json").stat().st_mode & 0o777 == 0o600


def test_a_rejected_heartbeat_does_not_refresh_liveness(tmp_path):
    receive(tmp_path, beat(), KEY, expected_host="rh-vps", now=T0)
    later = T0 + timedelta(minutes=10)
    with pytest.raises(HeartbeatRejected):
        receive(tmp_path, beat(later, key=OTHER_KEY), KEY, expected_host="rh-vps", now=later)
    verdict = judge(load_receiver_state(tmp_path), now=later)
    assert verdict.status == "CRITICAL" and verdict.reasons == ("HOST_HEARTBEAT_STALE",)


def test_the_outside_verdict():
    fresh = ReceiverState(last_received_at=T0, last_status="HEALTHY")
    assert judge(ReceiverState(), now=T0).reasons == ("HOST_HEARTBEAT_MISSING",)
    assert judge(fresh, now=T0 + timedelta(seconds=299)).status == "HEALTHY"
    assert judge(fresh, now=T0 + timedelta(seconds=301)).reasons == ("HOST_HEARTBEAT_STALE",)
    reported = ReceiverState(last_received_at=T0, last_status="CRITICAL")
    assert judge(reported, now=T0).reasons == ("HOST_REPORTS_CRITICAL",)


def test_an_unreadable_receiver_state_is_no_heartbeat(tmp_path):
    (tmp_path / "heartbeat-state.json").write_text("garbage")
    assert judge(load_receiver_state(tmp_path), now=T0).status == "CRITICAL"


def test_the_key_must_be_long_and_owner_only(tmp_path):
    short = tmp_path / "short"
    short.write_text("ab" * 8)
    short.chmod(0o600)
    with pytest.raises(TransportUnavailable) as refused:
        load_key(short)
    assert refused.value.code == "HEARTBEAT_KEY_TOO_SHORT"
    good = tmp_path / "good"
    good.write_text(KEY.hex())
    good.chmod(0o600)
    assert load_key(good) == KEY
    good.chmod(0o640)
    with pytest.raises(TransportUnavailable):
        load_key(good)


def test_receive_and_check_from_the_command_line(tmp_path, monkeypatch, capsys):
    key = tmp_path / "key"
    key.write_text(KEY.hex())
    key.chmod(0o600)
    store = tmp_path / "store"
    monkeypatch.setenv("RH_AGENTS_HEARTBEAT_KEY_FILE", str(key))
    line = sign(KEY, host="rh-vps", status="HEALTHY", now=datetime.now(UTC))

    class Stdin:
        buffer = __import__("io").BytesIO(line.encode() + b"\n")

    monkeypatch.setattr(sys, "stdin", Stdin)
    assert deadman.main(["receive", "--store", str(store), "--host", "rh-vps"]) == 0
    assert json.loads(capsys.readouterr().out)["accepted"] is True
    assert deadman.main(["check", "--store", str(store), "--host", "rh-vps", "--dry-run"]) == 0
    document = json.loads(capsys.readouterr().out)
    # A fresh first verdict is HEALTHY: recorded silently, nothing to send.
    assert document["status"] == "HEALTHY" and document["alert"] is None


def test_check_without_a_transport_is_refused(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("RH_AGENTS_ALERT_TRANSPORT", raising=False)
    code = deadman.main(["check", "--store", str(tmp_path), "--host", "rh-vps"])
    document = json.loads(capsys.readouterr().out)
    assert code == 2
    assert document["status"] == "CRITICAL" and document["sent"] is False
    assert document["transport_refusal"] == "ALERT_TRANSPORT_NOT_CONFIGURED"


def test_the_receiver_imports_no_trading_configuration():
    probe = (
        "import sys, src.supervisor.deadman; "
        "bad = [m for m in ('src.core.config', 'src.data.database', 'sqlalchemy', "
        "'src.runner.main') if m in sys.modules]; print(bad)"
    )
    completed = subprocess.run(
        [sys.executable, "-c", probe], cwd=BACKEND, capture_output=True, text=True, check=True
    )
    assert completed.stdout.strip() == "[]"


def test_a_mac_that_is_not_hex_is_malformed_not_a_crash():
    document = json.loads(beat())
    for mac in ("é" * 64, "zz", 7):
        document["mac"] = mac
        with pytest.raises(HeartbeatRejected) as rejected:
            check(json.dumps(document))
        assert rejected.value.code == "HEARTBEAT_MALFORMED"


def test_a_fresh_heartbeat_without_a_status_is_critical():
    state = ReceiverState(last_received_at=T0, last_status=None)
    assert judge(state, now=T0).reasons == ("HOST_HEARTBEAT_STATUS_UNKNOWN",)


def test_a_receipt_from_the_future_is_not_freshness():
    state = ReceiverState(last_received_at=T0 + timedelta(hours=1), last_status="HEALTHY")
    assert judge(state, now=T0).reasons == ("HOST_HEARTBEAT_STALE",)

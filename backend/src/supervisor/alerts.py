"""Alerting on the supervisor's health: when to tell someone, what, and how.

A separate pass from the tick (`python -m src.supervisor.main alert`, its own
timer). It reads one health verdict, decides against a persisted alert state
whether that verdict is news, and hands a short text to a transport — Telegram
when configured. It never starts anything and never changes the verdict.

When a message goes out
-----------------------
- the first verdict ever seen that is not HEALTHY (a HEALTHY first verdict is
  recorded silently as the baseline);
- every change of status (HEALTHY, DEGRADED, CRITICAL, STOPPED), a rise in
  severity reported as an ESCALATION;
- a new kind of reason under an unchanged DEGRADED or CRITICAL status;
- a REPEAT of an unchanged DEGRADED or CRITICAL status every 30 minutes;
- RECOVERY once a non-HEALTHY status that was delivered has been HEALTHY for
  two passes running — one healthy pass between two failures is no recovery.

At most `HOURLY_CAP` messages go out in any hour; what the cap holds back is
counted, and a status that is still news is sent once the hour allows. A
message that could not be delivered changes nothing that was delivered: the
next pass tries again. The state survives restarts, so a restart neither
repeats an old alert nor forgets an open one.

What a message may contain is fixed here: the project, a host alias, the
status, reason codes, the number of open positions and the instant. Codes are
checked against a pattern and anything else is dropped, so no free text — a
provider message, a connection string, a token — can reach a message.

This module imports no settings and no database code: it runs where the
trading configuration is not (the dead-man receiver, `deadman.py`).
"""

import fcntl
import json
import logging
import os
import re
import stat
import tempfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol

import httpx

# httpx logs each request URL at INFO, and a Telegram URL carries the token.
logging.getLogger("httpx").setLevel(logging.WARNING)

PROJECT = "RH-AGENTS"
REPEAT_SECONDS = 30 * 60
HOURLY_CAP = 6
RECOVERY_STREAK = 2
MAX_REASONS = 8
ALERT_STATE_VERSION = 1
ALERT_STATE_FILE = "alert-state.json"
ALERT_LOCK = "alert.lock"

STATUSES = ("HEALTHY", "DEGRADED", "CRITICAL", "STOPPED")
SEVERITY = {"HEALTHY": 0, "STOPPED": 1, "DEGRADED": 2, "CRITICAL": 3}
CODE = re.compile(r"^[A-Z][A-Z0-9_]{0,63}(:[A-Z0-9_]{1,32})?$")
HOST_ALIAS = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")
UNKNOWN_STATUS_REASON = "HEALTH_STATUS_UNKNOWN"

TRANSPORT_ENV = "RH_AGENTS_ALERT_TRANSPORT"
HOST_ALIAS_ENV = "RH_AGENTS_ALERT_HOST_ALIAS"
TOKEN_FILE_ENV = "RH_AGENTS_ALERT_TELEGRAM_TOKEN_FILE"
CHAT_FILE_ENV = "RH_AGENTS_ALERT_TELEGRAM_CHAT_ID_FILE"
# systemd's `LoadCredential=` directory: the default home of both files.
CREDENTIALS_ENV = "CREDENTIALS_DIRECTORY"
TOKEN_CREDENTIAL = "telegram-token"
CHAT_CREDENTIAL = "telegram-chat-id"
TELEGRAM_API = "https://api.telegram.org"
TELEGRAM_TOKEN = re.compile(r"^\d{5,20}:[A-Za-z0-9_-]{30,64}$")
TELEGRAM_CHAT = re.compile(r"^-?\d{1,20}$")
SEND_SECONDS = 10.0
MAX_SECRET_BYTES = 256


class AlertKind(StrEnum):
    ALERT = "ALERT"
    ESCALATION = "ESCALATION"
    CHANGE = "CHANGE"
    REPEAT = "REPEAT"
    RECOVERY = "RECOVERY"


@dataclass(frozen=True)
class Observation:
    """One health verdict, reduced to what an alert may say."""

    status: str
    reasons: tuple[str, ...]
    open_positions: int | None
    observed_at: datetime


@dataclass(frozen=True)
class AlertState:
    """What has been delivered, persisted across passes and restarts."""

    notified_status: str | None = None
    notified_reasons: tuple[str, ...] = ()
    notified_at: datetime | None = None
    sent_at: tuple[datetime, ...] = ()
    healthy_streak: int = 0
    send_failures: int = 0
    last_failure: str | None = None
    suppressed: int = 0


@dataclass(frozen=True)
class Decision:
    """`kind=None`: nothing to send. `baseline`: record HEALTHY silently.

    `capped`: there is news, but the hourly cap holds it back for now.
    """

    kind: AlertKind | None
    observation: Observation
    baseline: bool = False
    capped: bool = False


def codes(values: Any) -> tuple[str, ...]:
    """Only well-formed codes, deduplicated, in order. Anything else is dropped."""
    if not isinstance(values, (list, tuple)):
        return ()
    found = [value for value in values if isinstance(value, str) and CODE.match(value)]
    return tuple(dict.fromkeys(found))


def observe(document: Any, *, now: datetime) -> Observation:
    """A health document as an observation. Anything unreadable is CRITICAL.

    An unknown status is never taken for HEALTHY: it becomes CRITICAL with
    `HEALTH_STATUS_UNKNOWN`, whatever reasons came with it.
    """
    if not isinstance(document, dict):
        return Observation("CRITICAL", (UNKNOWN_STATUS_REASON,), None, now)
    status = document.get("status")
    reasons = codes(document.get("reasons"))
    positions = document.get("open_positions")
    if not isinstance(positions, int) or isinstance(positions, bool) or positions < 0:
        positions = None
    if status not in STATUSES:
        return Observation("CRITICAL", (UNKNOWN_STATUS_REASON, *reasons), positions, now)
    return Observation(str(status), reasons, positions, now)


def _families(reasons: tuple[str, ...]) -> set[str]:
    # `CONSECUTIVE_FAILURES:3` and `:4` are the same kind of news.
    return {reason.split(":", 1)[0] for reason in reasons}


def _recent(sent: tuple[datetime, ...], now: datetime) -> tuple[datetime, ...]:
    # A delivery "in the future" (a clock set back, a hand-edited file) would
    # hold the cap shut until the clock caught up: it is dropped instead.
    return tuple(at for at in sent if timedelta(0) <= now - at < timedelta(hours=1))


def decide(state: AlertState, observation: Observation) -> Decision:
    """Whether this observation is news against what was delivered."""
    status = observation.status
    notified = state.notified_status
    now = observation.observed_at
    kind: AlertKind | None = None
    if notified is None:
        if status == "HEALTHY":
            return Decision(None, observation, baseline=True)
        kind = AlertKind.ALERT
    elif status == "HEALTHY":
        streak = state.healthy_streak + 1
        if notified != "HEALTHY" and streak >= RECOVERY_STREAK:
            kind = AlertKind.RECOVERY
    elif status != notified:
        rising = notified != "HEALTHY" and SEVERITY[status] > SEVERITY[notified]
        kind = AlertKind.ESCALATION if rising else AlertKind.ALERT
    elif status in ("DEGRADED", "CRITICAL"):
        if _families(observation.reasons) - _families(state.notified_reasons):
            kind = AlertKind.CHANGE
        else:
            since = None if state.notified_at is None else (now - state.notified_at).total_seconds()
            # Never notified, long enough ago, or "in the future": repeat.
            if since is None or since >= REPEAT_SECONDS or since < 0:
                kind = AlertKind.REPEAT
    if kind is None:
        return Decision(None, observation)
    # A move into CRITICAL always goes out: the cap is for repeats, changes,
    # recoveries and lesser alerts, never for the news that matters most.
    urgent = status == "CRITICAL" and kind in (AlertKind.ALERT, AlertKind.ESCALATION)
    if not urgent and len(_recent(state.sent_at, now)) >= HOURLY_CAP:
        return Decision(kind, observation, capped=True)
    return Decision(kind, observation)


def apply(
    state: AlertState, decision: Decision, *, delivered: bool, failure: str | None = None
) -> AlertState:
    """The state after this pass. Only a delivery changes what was delivered."""
    observation = decision.observation
    now = observation.observed_at
    streak = state.healthy_streak + 1 if observation.status == "HEALTHY" else 0
    sent = _recent(state.sent_at, now)
    if decision.baseline:
        return replace(
            state,
            notified_status="HEALTHY",
            notified_reasons=(),
            notified_at=now,
            sent_at=sent,
            healthy_streak=streak,
        )
    if decision.kind is None:
        return replace(state, sent_at=sent, healthy_streak=streak)
    if decision.capped:
        return replace(state, sent_at=sent, healthy_streak=streak, suppressed=state.suppressed + 1)
    if not delivered:
        return replace(
            state,
            sent_at=sent,
            healthy_streak=streak,
            send_failures=state.send_failures + 1,
            last_failure=failure,
        )
    return AlertState(
        notified_status=observation.status,
        notified_reasons=observation.reasons,
        notified_at=now,
        sent_at=(*sent, now),
        healthy_streak=streak,
        send_failures=0,
        last_failure=None,
        suppressed=0,
    )


def host_alias(value: str | None) -> str:
    """The configured alias when it is a plain short name; never a hostname or address."""
    candidate = (value or "").strip()
    return candidate if HOST_ALIAS.match(candidate) else "unnamed-host"


def message(decision: Decision, *, host: str, suppressed: int = 0) -> str:
    """The whole text of an alert: fixed labels and checked values only."""
    observation = decision.observation
    reasons = observation.reasons[:MAX_REASONS]
    more = len(observation.reasons) - len(reasons)
    listed = ", ".join(reasons) if reasons else "none"
    if more > 0:
        listed += f" (+{more} more)"
    positions = "unknown" if observation.open_positions is None else str(observation.open_positions)
    kind = decision.kind.value if decision.kind is not None else "STATUS"
    at = observation.observed_at.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    text = (
        f"{PROJECT} {kind}: {observation.status}\n"
        f"host: {host_alias(host)}\n"
        f"reasons: {listed}\n"
        f"open positions: {positions}\n"
        f"at: {at}"
    )
    if suppressed > 0:
        text += f"\nheld back by the hourly cap: {suppressed}"
    return text


# State file ------------------------------------------------------------------


class AlertStateUnreadable(Exception):
    """The alert state exists and is not one this code wrote."""


def _instant(value: Any) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("An instant must be an ISO string")
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError("An instant must carry its offset")
    return parsed


def _iso(value: datetime | None) -> str | None:
    return None if value is None else value.astimezone(UTC).isoformat()


def _count(value: Any) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError("A count must be a non-negative integer")
    return value


def load_alert_state(directory: Path) -> AlertState | None:
    try:
        raw = (directory / ALERT_STATE_FILE).read_text()
    except FileNotFoundError:
        return None
    except OSError as error:
        raise AlertStateUnreadable from error
    try:
        document = json.loads(raw)
        if not isinstance(document, dict) or document.get("version") != ALERT_STATE_VERSION:
            raise ValueError("Unknown alert state version")
        notified = document.get("notified_status")
        if notified is not None and notified not in STATUSES:
            raise ValueError("Unknown notified status")
        sent = document.get("sent_at", [])
        if not isinstance(sent, list):
            raise ValueError("Deliveries must be a list")
        failure = document.get("last_failure")
        if failure is not None and not (isinstance(failure, str) and CODE.match(failure)):
            raise ValueError("A failure must be a code")
        return AlertState(
            notified_status=notified,
            notified_reasons=codes(document.get("notified_reasons", [])),
            notified_at=_instant(document.get("notified_at")),
            sent_at=tuple(at for at in (_instant(item) for item in sent[:64]) if at is not None),
            healthy_streak=_count(document.get("healthy_streak", 0)),
            send_failures=_count(document.get("send_failures", 0)),
            last_failure=failure,
            suppressed=_count(document.get("suppressed", 0)),
        )
    except (ValueError, TypeError) as error:
        raise AlertStateUnreadable from error


def save_alert_state(directory: Path, state: AlertState) -> None:
    """Replace the alert state atomically, readable by its owner only."""
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    document = {
        "version": ALERT_STATE_VERSION,
        "notified_status": state.notified_status,
        "notified_reasons": list(state.notified_reasons),
        "notified_at": _iso(state.notified_at),
        "sent_at": [_iso(at) for at in state.sent_at],
        "healthy_streak": state.healthy_streak,
        "send_failures": state.send_failures,
        "last_failure": state.last_failure,
        "suppressed": state.suppressed,
    }
    handle, temporary = tempfile.mkstemp(dir=directory, prefix=".alert.", suffix=".tmp")
    try:
        with os.fdopen(handle, "w") as stream:
            json.dump(document, stream, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, directory / ALERT_STATE_FILE)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


# Transports ------------------------------------------------------------------


class Transport(Protocol):
    def send(self, text: str) -> str | None:
        """Deliver one text. `None` when delivered, else a failure code."""
        ...


class TransportUnavailable(Exception):
    """No usable transport is configured. `code` says why; never a value."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _systemd_credential(path: Path) -> bool:
    directory = os.environ.get(CREDENTIALS_ENV, "").strip()
    if not directory:
        return False
    try:
        return path.resolve().parent == Path(directory).resolve()
    except OSError:
        return False


def read_secret_file(path: Path) -> str:
    """A small secret file nobody else can read. The value is never echoed.

    An ordinary file must be owner-only (no group or other bits). A systemd
    credential (`LoadCredential=`, under `$CREDENTIALS_DIRECTORY`) is 0400 with
    an ACL for the service user, which shows up as group bits: there the rule
    is owner root or this user, and nothing for others.
    """
    try:
        info = path.stat()
    except OSError as error:
        raise TransportUnavailable("SECRET_FILE_MISSING") from error
    if not stat.S_ISREG(info.st_mode):
        raise TransportUnavailable("SECRET_FILE_NOT_REGULAR")
    if _systemd_credential(path):
        if info.st_mode & 0o007 or info.st_uid not in (0, os.geteuid()):
            raise TransportUnavailable("SECRET_FILE_PERMISSIONS_TOO_OPEN")
    elif info.st_mode & 0o077:
        raise TransportUnavailable("SECRET_FILE_PERMISSIONS_TOO_OPEN")
    if info.st_size > MAX_SECRET_BYTES:
        raise TransportUnavailable("SECRET_FILE_TOO_LARGE")
    try:
        return path.read_text().strip()
    except (OSError, UnicodeDecodeError) as error:
        raise TransportUnavailable("SECRET_FILE_UNREADABLE") from error


class TelegramTransport:
    """Telegram `sendMessage` to one chat. The token lives in the URL only.

    Every failure is reduced to a code: an HTTP error's text carries the URL,
    and the URL carries the token, so no exception text ever leaves here.
    """

    def __init__(
        self,
        token: str,
        chat_id: str,
        *,
        client: Callable[[], httpx.Client] | None = None,
    ) -> None:
        if not TELEGRAM_TOKEN.match(token):
            raise TransportUnavailable("TELEGRAM_TOKEN_MALFORMED")
        if not TELEGRAM_CHAT.match(chat_id):
            raise TransportUnavailable("TELEGRAM_CHAT_ID_MALFORMED")
        self._token = token
        self._chat_id = chat_id
        self._client = client or (lambda: httpx.Client(timeout=SEND_SECONDS))

    def __repr__(self) -> str:
        return "TelegramTransport(<redacted>)"

    def send(self, text: str) -> str | None:
        url = f"{TELEGRAM_API}/bot{self._token}/sendMessage"
        payload = {"chat_id": self._chat_id, "text": text, "disable_web_page_preview": True}
        try:
            with self._client() as client:
                response = client.post(url, json=payload)
        except httpx.TimeoutException:
            return "SEND_TIMEOUT"
        except Exception:
            return "SEND_FAILED"
        if response.status_code == 429:
            return "SEND_RATE_LIMITED"
        if response.status_code != 200:
            return (
                f"SEND_HTTP_{response.status_code}"
                if 100 <= response.status_code < 600
                else ("SEND_FAILED")
            )
        try:
            accepted = response.json().get("ok") is True
        except Exception:
            accepted = False
        return None if accepted else "SEND_NOT_ACCEPTED"


def telegram_from_environment(env: Mapping[str, str] | None = None) -> TelegramTransport:
    """The Telegram transport from its two files: configured paths, else credentials."""
    environment = os.environ if env is None else env
    credentials = environment.get(CREDENTIALS_ENV, "").strip()
    token_path = environment.get(TOKEN_FILE_ENV, "").strip() or (
        str(Path(credentials) / TOKEN_CREDENTIAL) if credentials else ""
    )
    chat_path = environment.get(CHAT_FILE_ENV, "").strip() or (
        str(Path(credentials) / CHAT_CREDENTIAL) if credentials else ""
    )
    if not token_path or not chat_path:
        raise TransportUnavailable("TELEGRAM_NOT_CONFIGURED")
    return TelegramTransport(read_secret_file(Path(token_path)), read_secret_file(Path(chat_path)))


def transport_from_environment(env: Mapping[str, str] | None = None) -> Transport:
    environment = os.environ if env is None else env
    configured = environment.get(TRANSPORT_ENV, "").strip().lower()
    if configured == "telegram":
        return telegram_from_environment(environment)
    if not configured:
        raise TransportUnavailable("ALERT_TRANSPORT_NOT_CONFIGURED")
    raise TransportUnavailable("ALERT_TRANSPORT_UNKNOWN")


# One pass --------------------------------------------------------------------


@dataclass(frozen=True)
class AlertOutcome:
    status: str
    kind: str | None
    sent: bool
    capped: bool = False
    baseline: bool = False
    failure: str | None = None
    state_reset: bool = False
    dry_run_text: str | None = None
    suppressed: int = 0

    def as_dict(self) -> dict[str, Any]:
        document: dict[str, Any] = {
            "status": self.status,
            "alert": self.kind,
            "sent": self.sent,
            "capped": self.capped,
            "baseline": self.baseline,
            "failure": self.failure,
            "state_reset": self.state_reset,
            "suppressed": self.suppressed,
        }
        if self.dry_run_text is not None:
            document["dry_run_text"] = self.dry_run_text
        return document


class AlertPassBusy(Exception):
    """Another alert pass holds this state directory."""


def run_alert_pass(
    directory: Path,
    observation: Observation,
    *,
    transport: Transport | None,
    host: str,
    dry_run: bool = False,
) -> AlertOutcome:
    """Decide, send at most one message, record. One pass at a time per directory.

    `dry_run` shows the text that would go out and records nothing. Without a
    transport nothing can be sent: the decision is still made and reported,
    and the state is left as it was, so the news is still news once one is.
    """
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(directory / ALERT_LOCK, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise AlertPassBusy from error
        reset = False
        try:
            state = load_alert_state(directory) or AlertState()
        except AlertStateUnreadable as unreadable:
            if isinstance(unreadable.__cause__, OSError):
                # Cannot be read, so most likely cannot be written either:
                # sending now would send again every pass, uncapped.
                return AlertOutcome(
                    status=observation.status,
                    kind=None,
                    sent=False,
                    failure="ALERT_STATE_UNREADABLE",
                )
            state, reset = AlertState(), True
        decision = decide(state, observation)
        sending = decision.kind is not None and not decision.capped
        if dry_run:
            return AlertOutcome(
                status=observation.status,
                kind=None if decision.kind is None else decision.kind.value,
                sent=False,
                capped=decision.capped,
                baseline=decision.baseline,
                state_reset=reset,
                dry_run_text=message(decision, host=host, suppressed=state.suppressed)
                if sending
                else None,
                suppressed=state.suppressed,
            )
        failure: str | None = None
        delivered = False
        if sending:
            if transport is None:
                failure = "ALERT_TRANSPORT_NOT_CONFIGURED"
            else:
                failure = transport.send(message(decision, host=host, suppressed=state.suppressed))
                delivered = failure is None
        after = apply(state, decision, delivered=delivered, failure=failure)
        save_alert_state(directory, after)
        return AlertOutcome(
            status=observation.status,
            kind=None if decision.kind is None else decision.kind.value,
            sent=delivered,
            capped=decision.capped,
            baseline=decision.baseline,
            failure=failure,
            state_reset=reset,
            suppressed=after.suppressed,
        )
    finally:
        os.close(descriptor)


__all__ = [
    "AlertKind",
    "AlertOutcome",
    "AlertPassBusy",
    "AlertState",
    "AlertStateUnreadable",
    "Decision",
    "Observation",
    "TelegramTransport",
    "Transport",
    "TransportUnavailable",
    "apply",
    "codes",
    "decide",
    "host_alias",
    "load_alert_state",
    "message",
    "observe",
    "read_secret_file",
    "run_alert_pass",
    "save_alert_state",
    "telegram_from_environment",
    "transport_from_environment",
]

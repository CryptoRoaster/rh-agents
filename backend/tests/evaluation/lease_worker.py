"""A separate process that takes the credential lease, for the multiprocess tests.

Run as `python -m tests.evaluation.lease_worker ROLE SOURCE_HOME LEASE_PARENT
SIGNALS [WAIT]`. Coordination goes through marker files in SIGNALS, never
through shared memory, so the two processes share nothing but the filesystem --
exactly what two scheduler runs share.

Roles:

* `refresher` takes the lease, copies the login state, says `holding`, waits
  for `go`, rewrites its copy the way Codex does, writes it back, says
  `persisted`, and releases.
* `reader` says `waiting`, takes the lease, copies the login state and writes
  the generation it received to `received`.
* `holder` takes the lease, says `holding`, and sleeps until it is killed.

All credentials involved are test dummies.
"""

import asyncio
import sys
import time
from pathlib import Path

from src.evaluation.codex.auth_home import AUTH_FILE, build_isolated_home, persist_refresh
from src.evaluation.codex.credential_lease import CredentialLeaseError, credential_lease

GENERATION_2 = b'{"placeholder": "generation-2"}'
EXIT_LEASE_REFUSED = 3


def say(signals: Path, name: str, content: bytes = b"") -> None:
    staged = signals / f".{name}"
    staged.write_bytes(content)
    staged.rename(signals / name)


def await_signal(signals: Path, name: str, timeout: float = 30.0) -> None:
    ends = time.monotonic() + timeout
    while not (signals / name).exists():
        if time.monotonic() > ends:
            raise SystemExit(4)
        time.sleep(0.01)


def rewrite_like_codex(path: Path) -> None:
    """Truncate and write in place, as the 0.153.4 file auth store does."""
    with open(path, "r+b") as handle:
        handle.truncate(0)
        handle.write(GENERATION_2)


async def main(role: str, source: Path, parent: Path, signals: Path, wait: float) -> int:
    if role == "reader":
        say(signals, "waiting")
    try:
        async with credential_lease(source, wait_seconds=wait, parent=parent):
            attempt = signals / f"attempt-{role}"
            attempt.mkdir()
            home = build_isolated_home(source, attempt)
            assert home is not None
            if role == "holder":
                say(signals, "holding")
                await asyncio.sleep(600)
            if role == "reader":
                say(signals, "received", (home.path / AUTH_FILE).read_bytes())
                return 0
            say(signals, "holding")
            await_signal(signals, "go")
            rewrite_like_codex(home.path / AUTH_FILE)
            say(signals, "persisted", persist_refresh(home).value.encode())
            home.discard()
            return 0
    except CredentialLeaseError as error:
        say(signals, "refused", error.code.encode())
        return EXIT_LEASE_REFUSED


if __name__ == "__main__":
    role, source, parent, signals = sys.argv[1:5]
    wait = float(sys.argv[5]) if len(sys.argv) > 5 else 30.0
    raise SystemExit(asyncio.run(main(role, Path(source), Path(parent), Path(signals), wait)))

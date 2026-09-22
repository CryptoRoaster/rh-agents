"""A launcher that takes the process group first and starts Codex second.

`asyncio.create_subprocess_exec` forks the child *before* it connects the pipes.
In CPython's `unix_events._make_subprocess_transport` the transport is built,
and only then is the pipe-connection waiter awaited; if that waiter fails, the
cleanup path is `transp.close()` followed by `await transp._wait()`, and
`BaseSubprocessTransport.close()` calls `self._proc.kill()` -- the direct child,
not the group. The caller is handed an exception and never receives a `Process`.

So "the spawn raised" does not mean "nothing was started". A child can already
be running, and can already have started descendants of its own, while the
parent owns nothing it could terminate or even name.

This gate closes that window. It is started as the child, becomes the session
and process group leader, and then does nothing except wait on an inherited
file descriptor. Codex is executed only after the parent has written one byte
to the other end -- which it does only once it holds the `Process` handle and
has recorded the process group. If the transport fails first, the parent closes
its end instead: the gate reads EOF and exits without ever starting Codex.

`os.execv` keeps the pid, the process group and every inherited pipe, so the
handle the parent owns is the Codex process once the gate releases.
"""

import os
import sys

RELEASE_BYTE = b"\x01"
EXEC_FAILED = 127


def main(argv: list[str]) -> int:
    if len(argv) < 3:
        return EXEC_FAILED
    gate_fd = int(argv[1])
    command = argv[2:]
    try:
        released = os.read(gate_fd, 1) == RELEASE_BYTE
    except OSError:
        released = False
    finally:
        try:
            os.close(gate_fd)
        except OSError:
            pass

    if not released:
        # The parent never took ownership. Nothing has been started, and
        # nothing is going to be.
        return 0

    try:
        os.execv(command[0], command)
    except OSError:
        return EXEC_FAILED
    return EXEC_FAILED  # pragma: no cover - execv does not return on success


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))

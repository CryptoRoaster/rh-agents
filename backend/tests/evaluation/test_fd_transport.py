"""The two descriptors survive the real chain, or they do not.

The harness passes the pinned catalog as `model_catalog_json=/dev/fd/N` and the
output schema as `--output-schema /dev/fd/M`. Both rest on an assumption that
had never been exercised end to end against the real thing: that the
descriptors survive

    run_bounded (pass_fds) -> launch_gate -> /usr/bin/sandbox-exec -> exec child

Every earlier test either used a recording shim in place of `sandbox-exec`, or
read `/dev/fd/N` in the parent, where it trivially works. Neither says anything
about what a sandboxed child sees.

So this uses the real `/usr/bin/sandbox-exec` and the real outer profile, with
a harmless child that only hashes what it is given. No model, no network, no
credential -- the auth file the profile points at is a placeholder.

The two descriptors are checked *separately*. "Some fd was readable" would not
distinguish a working catalog path from a working schema path, and the first
real failure is exactly the kind of thing that distinction would locate.
"""

import hashlib
import os
import stat
import sys
from pathlib import Path

import pytest

from src.evaluation.codex import sandbox
from src.evaluation.codex.catalog import snapshot_payload
from src.evaluation.codex.deadline import Deadline
from src.evaluation.codex.models import OutputLimits
from src.evaluation.codex.process import run_bounded

pytestmark = pytest.mark.skipif(
    not sandbox.available(), reason="the real /usr/bin/sandbox-exec is macOS only"
)

CATALOG_SENTINEL = b'{"models": [{"slug": "fd-transport-catalog-sentinel"}]}\n'
SCHEMA_SENTINEL = b'{"type": "object", "title": "fd-transport-schema-sentinel"}\n'

READER = """\
read_it() {
  if out=$(/bin/cat "$2" 2>/dev/null); then
    echo "${1}_BYTES $out"
  else
    echo "${1}_UNREADABLE yes"
  fi
}
append_to() {
  if /bin/echo -n x >> "$2" 2>/dev/null; then
    echo "${1}_WRITABLE YES"
  else
    echo "${1}_WRITABLE NO"
  fi
}
read_it CATALOG "$1"
read_it SCHEMA "$2"
append_to CATALOG "$1"
append_to SCHEMA "$2"
# Positive control, on the one path the profile does make writable. Without it
# a "NO" above could just mean that appending never works under this profile.
append_to CONTROL "$3"
"""


def write_reader(directory: Path) -> Path:
    """A launcher shaped like the real one: an executable the gate can exec.

    A shell rather than an interpreter. A Python child aborts under this
    profile -- it wants paths the profile does not grant -- and that is a fact
    about the test's helper, not about the descriptors. `/bin/sh` is what the
    boundary probe already runs behind the same profile, so it keeps the
    experiment about the transport chain.
    """
    script = directory / "fd-reader.sh"
    script.write_text(READER, encoding="utf-8")
    launcher = directory / "reader"
    launcher.write_text(f'#!/bin/sh\nexec /bin/sh "{script}" "$@"\n', encoding="utf-8")
    launcher.chmod(launcher.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return launcher


async def transport(tmp_path: Path) -> dict[str, object]:
    """Send both snapshots through the whole chain and return what arrived."""
    workspace = tmp_path / "workspace"
    scratch = tmp_path / "scratch"
    codex_home = tmp_path / "codex-home"
    for directory in (workspace, scratch, codex_home):
        directory.mkdir(mode=0o700)
    placeholder = codex_home / "auth.json"
    placeholder.write_text('{"placeholder": "not a credential"}\n', encoding="utf-8")
    os.chmod(placeholder, 0o600)

    launcher = write_reader(workspace)
    catalog = snapshot_payload(CATALOG_SENTINEL, scratch)
    schema = snapshot_payload(SCHEMA_SENTINEL, scratch)
    assert catalog is not None and schema is not None

    profile = sandbox.write_bound_profile(tmp_path)
    roots = sandbox.SandboxRoots(
        codex_vendor=Path(sys.prefix), workspace=workspace, codex_home=codex_home
    )
    collected: list[bytes] = []
    try:
        command = [str(launcher), catalog.reference, schema.reference, str(placeholder)]
        result = await run_bounded(
            arguments=sandbox.wrap(command, profile.path, roots),
            environment={
                "PATH": "/usr/bin:/bin",
                "HOME": str(tmp_path),
                "TMPDIR": str(tmp_path),
                "CODEX_HOME": str(codex_home),
                "LANG": "en_US.UTF-8",
            },
            working_directory=workspace,
            stdin_payload=b"",
            limits=OutputLimits(),
            deadline=Deadline(total_seconds=60.0, cleanup_reserve_seconds=5.0),
            on_stdout_line=collected.append,
            extra_fds=(catalog.fd, schema.fd),
        )
    finally:
        catalog.close()
        schema.close()
        profile.path.unlink(missing_ok=True)

    reported: dict[str, object] = {"exit_code": result.exit_code}
    for raw in collected:
        line = raw.decode("utf-8", errors="replace").strip()
        key, _, value = line.partition(" ")
        reported[key] = value if value else True
    return reported


@pytest.mark.asyncio
async def test_both_descriptors_survive_the_real_sandbox_exec(tmp_path: Path) -> None:
    """The transport claim, measured rather than assumed.

    The chain is the real one: `run_bounded` with `pass_fds`, the launch gate,
    `/usr/bin/sandbox-exec` with the composed profile, and an `exec` into the
    child. The snapshots were unlinked before the child started and the profile
    grants no readable root that could contain them, so `/dev/fd` is the only
    way the child could see either one.
    """
    reported = await transport(tmp_path)
    assert reported["exit_code"] == 0, reported

    # Byte-exact, and checked separately per descriptor. "Some fd was readable"
    # would not tell the catalog path from the schema path, and telling them
    # apart is what would locate a failure in either.
    assert reported.get("CATALOG_BYTES") == CATALOG_SENTINEL.decode().strip()
    assert reported.get("SCHEMA_BYTES") == SCHEMA_SENTINEL.decode().strip()
    assert reported["CATALOG_BYTES"] != reported["SCHEMA_BYTES"]
    assert "CATALOG_UNREADABLE" not in reported
    assert "SCHEMA_UNREADABLE" not in reported

    print("CATALOG_FD_SHA256", hashlib.sha256(CATALOG_SENTINEL).hexdigest())
    print("SCHEMA_FD_SHA256", hashlib.sha256(SCHEMA_SENTINEL).hexdigest())


@pytest.mark.asyncio
async def test_the_descriptors_stay_read_only_on_the_far_side(tmp_path: Path) -> None:
    """O_RDONLY is a property of the open file description, and it has to hold.

    A writable descriptor would mean the sandboxed child could rewrite the
    catalog it was pinned to, from inside the boundary, with no name involved
    at all -- which is precisely what unlinking the snapshot was supposed to
    make impossible.
    """
    reported = await transport(tmp_path)
    assert reported["exit_code"] == 0, reported
    assert reported["CATALOG_WRITABLE"] == "NO"
    assert reported["SCHEMA_WRITABLE"] == "NO"
    # The control appended successfully, so the two refusals above are about
    # the descriptors and not about the profile refusing every write.
    assert reported["CONTROL_WRITABLE"] == "YES"

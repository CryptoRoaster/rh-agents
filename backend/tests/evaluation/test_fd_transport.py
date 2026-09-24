"""What each transport actually delivers, measured through the real chain.

The harness delivers two payloads to the child. The output schema travels as
`--output-schema /dev/fd/M`, and the model catalog travels as
`model_catalog_json=<named path>`. They differ because the CLI reads them
differently, and the difference was learned the hard way.

The earlier version of this file measured the descriptor transport and was
right about everything it claimed:

    FD_TRANSPORT_SURVIVES  PASS   the descriptor reaches the sandboxed child
    FD_SINGLE_READ         PASS   the child reads the exact bytes, read-only

What it never asked was whether the descriptor could be read *again*. Codex
0.153.4 loads `model_catalog_json` twice -- once in the initial
`ConfigBuilder::build()` in `exec/src/lib.rs`, and again at `thread/start`,
where `ConfigManager::load_with_overrides` runs `ConfigBuilder::build()` a
second time over the same CLI overrides -- and each load is a fresh
`std::fs::read_to_string`. Reopening `/dev/fd/N` returns to the same open file
description, so the second load parses the empty string the first load left
behind:

    Error: thread/start: thread/start failed: failed to load configuration:
    failed to parse model_catalog_json path `/dev/fd/6` as JSON:
    EOF while parsing a value at line 1 column 0

    FD_REPLAYABLE_FOR_MODEL_CATALOG  FAIL

So the property is measured here in both directions. The descriptor transport
is shown to be single-read, and kept for the schema, which
`load_output_schema` reads once. The named runtime catalog is shown to answer
three independent opens with identical bytes while refusing every attempt to
change it.

Everything runs against the real `/usr/bin/sandbox-exec` and the real composed
profile, with a harmless child that only reads what it is given. No model, no
network, no credential -- the auth file the profile points at is a placeholder.
"""

import hashlib
import os
import stat
import sys
from pathlib import Path

import pytest

from src.evaluation.codex import sandbox
from src.evaluation.codex.catalog import (
    RUNTIME_CATALOG_DIR_MODE,
    RUNTIME_CATALOG_MODE,
    materialise_runtime_catalog,
    snapshot_payload,
)
from src.evaluation.codex.deadline import Deadline
from src.evaluation.codex.models import OutputLimits
from src.evaluation.codex.process import run_bounded

pytestmark = pytest.mark.skipif(
    not sandbox.available(), reason="the real /usr/bin/sandbox-exec is macOS only"
)

CATALOG_SENTINEL = b'{"models": [{"slug": "fd-transport-catalog-sentinel"}]}\n'
SCHEMA_SENTINEL = b'{"type": "object", "title": "fd-transport-schema-sentinel"}\n'

# Two reads of the catalog reference, deliberately as two separate `cat`
# invocations rather than one. Each is its own `open`, which is the shape of
# what the two `ConfigBuilder::build()` calls do.
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
read_it CATALOGAGAIN "$1"
read_it SCHEMA "$2"
append_to CATALOG "$1"
append_to SCHEMA "$2"
# Positive control, on the one path the profile does make writable. Without it
# a "NO" above could just mean that appending never works under this profile.
append_to CONTROL "$3"
"""

# Three independent opens of the named catalog, plus every way of changing it.
NAMED_READER = """\
read_three() {
  i=1
  while [ $i -le 3 ]; do
    if out=$(/sbin/md5 -q "$1" 2>/dev/null); then
      echo "READ${i} $out"
    else
      echo "READ${i} UNREADABLE"
    fi
    i=$((i + 1))
  done
}
# Each attempt runs in a subshell. `: > file` is a special builtin, and a
# failed redirection on one of those ends a non-interactive shell outright --
# so without the subshell a denial would stop the script instead of being
# reported as a denial.
refuses() {
  if ( eval "$2" ) 2>/dev/null; then echo "${1} ALLOWED"; else echo "${1} DENIED"; fi
}
read_three "$1"
refuses WRITE "/bin/echo x > \\"$1\\""
refuses TRUNCATE ": > \\"$1\\""
refuses DELETE "/bin/rm -f \\"$1\\" && [ ! -f \\"$1\\" ]"
refuses SIDECAR ": > \\"$1.sidecar\\""
refuses CHMOD "/bin/chmod 600 \\"$1\\""
# Whatever the answers above were, the file has to still be there and still
# read the same. A "DENIED" that came with damage is not a denial.
read_three "$1"
# The loop above ends on a failing `[` test, so say what the exit status is
# rather than letting it report a child failure that never happened.
exit 0
"""


def write_reader(directory: Path, script_body: str, name: str) -> Path:
    """A launcher shaped like the real one: an executable the gate can exec.

    A shell rather than an interpreter. A Python child aborts under this
    profile -- it wants paths the profile does not grant -- and that is a fact
    about the test's helper, not about the transports. `/bin/sh` is what the
    boundary probe already runs behind the same profile, so it keeps the
    experiment about the delivery chain.
    """
    script = directory / f"{name}.sh"
    script.write_text(script_body, encoding="utf-8")
    launcher = directory / name
    launcher.write_text(f'#!/bin/sh\nexec /bin/sh "{script}" "$@"\n', encoding="utf-8")
    launcher.chmod(launcher.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return launcher


def layout(tmp_path: Path) -> tuple[Path, Path, Path, Path]:
    """The four directories every run here needs, with the auth placeholder."""
    workspace = tmp_path / "workspace"
    scratch = tmp_path / "scratch"
    codex_home = tmp_path / "codex-home"
    catalog_runtime = tmp_path / "catalog-runtime"
    for directory in (workspace, scratch, codex_home, catalog_runtime):
        directory.mkdir(mode=0o700)
    placeholder = codex_home / "auth.json"
    placeholder.write_text('{"placeholder": "not a credential"}\n', encoding="utf-8")
    os.chmod(placeholder, 0o600)
    return workspace, scratch, codex_home, catalog_runtime


async def run_child(
    *,
    launcher: Path,
    command_arguments: list[str],
    workspace: Path,
    codex_home: Path,
    catalog_file: Path,
    tmp_path: Path,
    extra_fds: tuple[int, ...],
) -> dict[str, object]:
    """Run one child through the whole chain and return what it reported."""
    profile = sandbox.write_bound_profile(tmp_path)
    roots = sandbox.SandboxRoots(
        codex_vendor=Path(sys.prefix),
        workspace=workspace,
        codex_home=codex_home,
        catalog_file=catalog_file,
    )
    collected: list[bytes] = []
    try:
        result = await run_bounded(
            arguments=sandbox.wrap([str(launcher), *command_arguments], profile.path, roots),
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
            extra_fds=extra_fds,
        )
    finally:
        profile.path.unlink(missing_ok=True)

    reported: dict[str, object] = {"exit_code": result.exit_code}
    for raw in collected:
        line = raw.decode("utf-8", errors="replace").strip()
        key, _, value = line.partition(" ")
        reported[key] = value if value else True
    return reported


async def transport(tmp_path: Path) -> dict[str, object]:
    """Send both snapshots through the whole chain as descriptors."""
    workspace, scratch, codex_home, catalog_runtime = layout(tmp_path)
    launcher = write_reader(workspace, READER, "fd-reader")
    catalog = snapshot_payload(CATALOG_SENTINEL, scratch)
    schema = snapshot_payload(SCHEMA_SENTINEL, scratch)
    assert catalog is not None and schema is not None
    placeholder = codex_home / "auth.json"
    try:
        return await run_child(
            launcher=launcher,
            command_arguments=[catalog.reference, schema.reference, str(placeholder)],
            workspace=workspace,
            codex_home=codex_home,
            # Unused by this child, but the profile names the parameter, so
            # `sandbox-exec` still has to be given one.
            catalog_file=catalog_runtime / "models.json",
            tmp_path=tmp_path,
            extra_fds=(catalog.fd, schema.fd),
        )
    finally:
        catalog.close()
        schema.close()


@pytest.mark.asyncio
async def test_both_descriptors_survive_the_real_sandbox_exec(tmp_path: Path) -> None:
    """The transport claim, measured rather than assumed.

    The chain is the real one: `run_bounded` with `pass_fds`, the launch gate,
    `/usr/bin/sandbox-exec` with the composed profile, and an `exec` into the
    child. The snapshots were unlinked before the child started and the profile
    grants no readable root that could contain them, so `/dev/fd` is the only
    way the child could see either one.

    This is still true and was never the problem. What it does not say is
    anything about a *second* read -- see the test below.
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
async def test_a_descriptor_answers_the_first_read_and_not_the_second(
    tmp_path: Path,
) -> None:
    """The reproduction of what killed the third real probe.

    Two `cat` invocations on the same `/dev/fd/N`, in the same child, through
    the real chain. The first returns the whole payload. The second returns
    nothing, because reopening `/dev/fd/N` resolves to the open file
    description the first read already advanced to the end.

    Read as a specification this says:

        FD_SURVIVES     YES
        FD_REPLAYABLE   NO

    which is a property of the mechanism, not a defect in it. It is the right
    transport for a payload the CLI reads once, and the wrong one for
    `model_catalog_json`, which `ConfigBuilder::build()` loads at startup and
    `ConfigManager::load_with_overrides` loads again at `thread/start`.
    """
    reported = await transport(tmp_path)
    assert reported["exit_code"] == 0, reported

    assert reported.get("CATALOG_BYTES") == CATALOG_SENTINEL.decode().strip()
    # The second read succeeded as a read and delivered nothing. `cat` exits 0
    # on an exhausted descriptor, so this is an empty payload rather than an
    # error -- which is exactly why the CLI reported a JSON parse failure at
    # "line 1 column 0" instead of an I/O failure.
    assert "CATALOGAGAIN_UNREADABLE" not in reported
    assert reported.get("CATALOGAGAIN_BYTES") in (None, "", True)
    assert reported.get("CATALOGAGAIN_BYTES") != reported.get("CATALOG_BYTES")


@pytest.mark.asyncio
async def test_the_descriptors_stay_read_only_on_the_far_side(tmp_path: Path) -> None:
    """O_RDONLY is a property of the open file description, and it has to hold.

    A writable descriptor would mean the sandboxed child could rewrite the
    payload it was pinned to, from inside the boundary, with no name involved
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


async def named_catalog(tmp_path: Path) -> tuple[dict[str, object], str]:
    """Deliver the catalog as the runtime file and let the child hammer it."""
    workspace, scratch, codex_home, catalog_runtime = layout(tmp_path)
    source = scratch / "operator-catalog.json"
    source.write_bytes(CATALOG_SENTINEL)
    catalog = materialise_runtime_catalog(source, catalog_runtime)
    assert catalog is not None

    launcher = write_reader(workspace, NAMED_READER, "named-reader")
    reported = await run_child(
        launcher=launcher,
        command_arguments=[str(catalog.path)],
        workspace=workspace,
        codex_home=codex_home,
        catalog_file=catalog.path,
        tmp_path=tmp_path,
        extra_fds=(),
    )
    return reported, catalog.digest


@pytest.mark.asyncio
async def test_the_named_catalog_answers_three_independent_reads(tmp_path: Path) -> None:
    """Replayability, which is the whole reason the catalog left `/dev/fd`.

    Three separate opens of the same path, all returning the same bytes, under
    the real profile. Two would not be enough to state the property with a
    straight face and one is what the old test did.
    """
    reported, digest = await named_catalog(tmp_path)
    assert reported["exit_code"] == 0, reported

    reads = [reported.get(f"READ{index}") for index in (1, 2, 3)]
    assert "UNREADABLE" not in reads, reported
    assert len(set(reads)) == 1, reads

    # Byte-identical to what the harness judged, not merely self-consistent.
    expected = hashlib.md5(CATALOG_SENTINEL).hexdigest()  # noqa: S324 - /sbin/md5
    assert reads == [expected, expected, expected]
    assert digest == hashlib.sha256(CATALOG_SENTINEL).hexdigest()


@pytest.mark.asyncio
async def test_the_named_catalog_refuses_every_way_of_changing_it(tmp_path: Path) -> None:
    """The name is readable and nothing more.

    Giving up the unlinked descriptor gave up immutability by construction, so
    it is bought back here: the profile grants `file-read*` on one literal path
    and no write operation of any kind, and the file is 0400 inside a 0500
    directory. The three reads are repeated afterwards so a refusal that still
    did damage would be visible.
    """
    reported, _ = await named_catalog(tmp_path)
    assert reported["exit_code"] == 0, reported
    for operation in ("WRITE", "TRUNCATE", "DELETE", "SIDECAR", "CHMOD"):
        assert reported[operation] == "DENIED", (operation, reported)
    assert reported["READ1"] == reported["READ3"]


def test_the_runtime_catalog_modes_are_what_the_profile_assumes(tmp_path: Path) -> None:
    """0400 in a 0500 directory: the second lock, outside the sandbox.

    The profile is the boundary for the Codex process. These modes are what
    stops anything else on the machine running as this user from rewriting the
    judged bytes between the preflight and the exec.
    """
    directory = tmp_path / "catalog-runtime"
    directory.mkdir(mode=0o700)
    source = tmp_path / "operator-catalog.json"
    source.write_bytes(CATALOG_SENTINEL)
    catalog = materialise_runtime_catalog(source, directory)
    assert catalog is not None
    try:
        assert catalog.path.stat().st_mode & 0o777 == RUNTIME_CATALOG_MODE
        assert directory.stat().st_mode & 0o777 == RUNTIME_CATALOG_DIR_MODE
        assert catalog.path.name == "models.json"
        assert catalog.still_matches() is True
    finally:
        catalog.discard()
    assert not catalog.path.exists()
    assert not directory.exists()

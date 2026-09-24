"""A CODEX_HOME that holds the login state and the one file Codex insists on.

Pointing the attempt at the user's own `~/.codex` would mean the outer sandbox
has to make that whole directory readable: config, history, sessions, skills,
plugins, cached models, whatever else has accumulated. None of it is needed for
one structured turn, and all of it would be inside the boundary.

So the harness builds its own: a `0700` directory containing a `0600` copy of
the one file Codex 0.153.4 reads for a ChatGPT session, `auth.json`, and a
`0644` `installation_id`. Nothing else is copied, the directory is removed
afterwards, and `--ignore-user-config` plus `--ephemeral` keep the CLI from
reaching for or leaving anything more.

The second file is not a choice. `codex exec` starts an in-process app-server
client, whose `start_uninitialized` calls `resolve_installation_id`
(`core/src/installation_id.rs`), which opens `CODEX_HOME/installation_id`
read+write+create, locks it, repairs its mode and may rewrite it. With a home
holding only `auth.json` that fails, and the second authorised real probe
reported exactly that: `failed to initialize in-process app-server client:
Operation not permitted (os error 1)`, exit 1, no events.

It is **pre-seeded here rather than left for Codex to create**. Letting the CLI
create it would mean making the directory writable, and a writable directory
means arbitrary sidecar files, an unknown file set, and an `AUTH_HOME_ISOLATION`
gate with nothing left to check. Seeding it keeps the directory unwritable and
the contents known in advance, so exactly one more literal path is opened for
writing and nothing else can appear beside it.

`installation_id` is not a credential -- it is a persistent installation
identifier -- but it is still never read into this process as a value, logged or
reported.

Two things this module will not do. It never reads the token into this process
-- the copy goes through the filesystem, byte for byte, and no value from it is
parsed, logged, asserted on or reported. And it never writes into the user's
own home: a token refresh during an attempt lands in the isolated copy, which
is then discarded, so the session the user holds outside is untouched.

Two questions are kept apart here, because folding them together once produced
a gate nothing could ever clear. Whether the home is *isolated* is checkable
offline and is `AUTH_HOME_ISOLATION`: the directory exists, holds exactly this
one file, and both modes are restrictive. Whether the copied state actually
authenticates against the provider is not checkable without a request, and it
is `AUTH_REMOTE_VALIDITY`, which is advisory and never blocks.

Between the two sits `CHATGPT_SESSION`, which is neither a guess nor a round
trip: the CLI is asked, in this home, behind the outer profile, with the
credential store pinned to `file` so the answer is about this copy rather than
about a keychain entry.
"""

import os
import shutil
import uuid
from dataclasses import dataclass
from pathlib import Path

# `auth.json` is where 0.153.4 keeps the ChatGPT session under the `file`
# credential store, which every invocation pins explicitly rather than leaving
# to the default.
AUTH_FILE = "auth.json"
# The app-server startup file. 0644 is the mode `resolve_installation_id`
# checks for and repairs, so seeding it at anything else would make the CLI
# attempt a chmod on the first run.
INSTALLATION_ID_FILE = "installation_id"

# The complete, closed set. A third entry is an isolation failure.
EXPECTED_ENTRIES = (AUTH_FILE, INSTALLATION_ID_FILE)

HOME_MODE = 0o700
AUTH_MODE = 0o600
INSTALLATION_ID_MODE = 0o644


@dataclass(frozen=True)
class IsolatedHome:
    """A private CODEX_HOME and whether the login state made it in."""

    path: Path
    auth_present: bool

    def discard(self) -> None:
        shutil.rmtree(self.path, ignore_errors=True)


def _seed_installation_id(source_home: Path, target: Path) -> None:
    """Put an `installation_id` in place before the CLI looks for one.

    Reuses the source machine's identifier when there is one, so a probe run
    does not look like a fresh installation to the provider. When there is
    none, a new UUID is generated **here** -- the user's own `CODEX_HOME` is
    never written to, not even to create this file.
    """
    destination = target / INSTALLATION_ID_FILE
    source = source_home / INSTALLATION_ID_FILE
    if source.is_file():
        # copyfile, like the auth file: the value never becomes a Python
        # string in this process.
        shutil.copyfile(source, destination)
    else:
        destination.write_text(str(uuid.uuid4()), encoding="utf-8")
    os.chmod(destination, INSTALLATION_ID_MODE)


def build_isolated_home(source_home: Path, parent: Path) -> IsolatedHome | None:
    """Create the private home holding exactly the two expected files.

    Returns None when it cannot be created. A missing source auth file is not
    an error here -- it produces a home with `auth_present=False`, and the
    preflight decides what that means.
    """
    try:
        target = Path(os.path.join(parent, "codex-home"))
        target.mkdir(mode=HOME_MODE, parents=False, exist_ok=False)
    except OSError:
        return None

    source = source_home / AUTH_FILE
    if not source.is_file():
        return IsolatedHome(path=target, auth_present=False)

    destination = target / AUTH_FILE
    try:
        # copyfile, not copy2: no metadata is carried across, and the content
        # never passes through this process as a value.
        shutil.copyfile(source, destination)
        os.chmod(destination, AUTH_MODE)
        _seed_installation_id(source_home, target)
    except OSError:
        shutil.rmtree(target, ignore_errors=True)
        return None

    return IsolatedHome(path=target, auth_present=True)


__all__ = [
    "AUTH_FILE",
    "AUTH_MODE",
    "EXPECTED_ENTRIES",
    "HOME_MODE",
    "INSTALLATION_ID_FILE",
    "INSTALLATION_ID_MODE",
    "IsolatedHome",
    "build_isolated_home",
]

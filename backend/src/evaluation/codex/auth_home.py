"""A CODEX_HOME that holds the login state and nothing else.

Pointing the attempt at the user's own `~/.codex` would mean the outer sandbox
has to make that whole directory readable: config, history, sessions, skills,
plugins, cached models, whatever else has accumulated. None of it is needed for
one structured turn, and all of it would be inside the boundary.

So the harness builds its own: a `0700` directory containing a `0600` copy of
the one file Codex 0.153.4 reads for a ChatGPT session, `auth.json`. Nothing
else is copied, the directory is removed afterwards, and `--ignore-user-config`
plus `--ephemeral` keep the CLI from reaching for or leaving anything more.

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
from dataclasses import dataclass
from pathlib import Path

# The only file copied. `auth.json` is where 0.153.4 keeps the ChatGPT session
# under the `file` credential store, which every invocation pins explicitly
# rather than leaving to the default.
AUTH_FILE = "auth.json"

HOME_MODE = 0o700
AUTH_MODE = 0o600


@dataclass(frozen=True)
class IsolatedHome:
    """A private CODEX_HOME and whether the login state made it in."""

    path: Path
    auth_present: bool

    def discard(self) -> None:
        shutil.rmtree(self.path, ignore_errors=True)


def build_isolated_home(source_home: Path, parent: Path) -> IsolatedHome | None:
    """Create the private home, copying only the auth file.

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
    except OSError:
        shutil.rmtree(target, ignore_errors=True)
        return None

    return IsolatedHome(path=target, auth_present=True)


__all__ = ["AUTH_FILE", "AUTH_MODE", "HOME_MODE", "IsolatedHome", "build_isolated_home"]

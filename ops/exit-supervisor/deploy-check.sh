#!/usr/bin/env bash
# Read-only deploy gate. Run before every code deploy into the runtime worktree
# (`git pull --ff-only`, `uv sync`, migrations): it refuses while the
# supervisor is loaded, a position is open, or an exit run is going.
# Exit code: 0 deploy allowed, 2 refused.
set -euo pipefail
# shellcheck source=common.sh
source "$(dirname "${BASH_SOURCE[0]}")/common.sh"
resolve_uv
supervisor deploy-check

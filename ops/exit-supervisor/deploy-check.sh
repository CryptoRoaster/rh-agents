#!/usr/bin/env bash
# Read-only deploy gate. Run before every code deploy into the runtime worktree
# (`git pull --ff-only`, `uv sync`, migrations): it refuses while the
# supervisor is loaded, a position is open or unknown, an exit or entry run is
# recorded as going, or any runner process is alive.
# Exit code: 0 deploy allowed, 2 refused.
set -euo pipefail
# shellcheck source=common.sh
source "$(dirname "${BASH_SOURCE[0]}")/common.sh"
resolve_uv

refused=0
if ! supervisor deploy-check; then
  refused=1
fi
status=0
"${RH_AGENTS_PGREP:-pgrep}" -f 'src\.runner\.main|src\.supervisor\.main tick' >/dev/null 2>&1 || status=$?
if [[ "$status" -eq 0 ]]; then
  echo '{"kind": "exit_supervisor_deploy_check", "refusals": ["DEPLOY_REFUSED_RUNNER_PROCESS_ALIVE"]}'
  refused=1
elif [[ "$status" -ne 1 ]]; then
  echo '{"kind": "exit_supervisor_deploy_check", "refusals": ["DEPLOY_REFUSED_PROCESS_LIST_UNKNOWN"]}'
  refused=1
fi
if [[ "$refused" -ne 0 ]]; then
  echo "DEPLOY_REFUSED" >&2
  exit 2
fi

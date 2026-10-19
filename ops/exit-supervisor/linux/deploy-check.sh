#!/usr/bin/env bash
# Read-only deploy gate (Linux). Run before every step that changes what the
# runtime runs: git fast-forward, dependency sync, migrations, unit reinstall,
# supervisor restart. Refuses while the supervisor timer runs or its state is
# unknown, a position is open or unknown, an exit or entry run is recorded as
# going, or any runner, scout or V4 index-job process is alive (the job units
# in ops/jobs/linux). Exit code: 0 allowed, 2 refused.
set -euo pipefail
# shellcheck source=common.sh
source "$(dirname "${BASH_SOURCE[0]}")/common.sh"

refused=0
if ! supervisor deploy-check; then
  refused=1
fi
status=0
"$PGREP" -f 'src\.runner\.main|src\.supervisor\.main tick|src\.agents\.atlas\.v4\.index_job' >/dev/null 2>&1 || status=$?
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
echo "deploy allowed"

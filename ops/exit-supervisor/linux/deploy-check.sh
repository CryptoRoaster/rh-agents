#!/usr/bin/env bash
# Read-only deploy gate (Linux). Run before every step that changes what the
# runtime runs: git fast-forward, dependency sync, migrations, unit reinstall,
# supervisor restart. Refuses while the supervisor timer runs or its state is
# unknown, a position is open or unknown, an exit or entry run is recorded as
# going, any runner, scout or V4 index-job process is alive, or a job unit of
# ops/jobs/linux has its timer running or a run going. Exit code: 0 allowed,
# 2 refused.
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
# The job units (ops/jobs/linux), when installed: every timer stopped and no
# job running, so none fires or runs while the checkout changes. A unit that
# is not installed reads inactive; an unknown state refuses.
for unit in rh-agents-pool-index rh-agents-scout rh-agents-entry; do
  timer_state="$(unit_state "$unit.timer")"
  service_state="$(unit_state "$unit.service")"
  if [[ "$timer_state" != "inactive" ]] ||
    [[ "$service_state" != "inactive" && "$service_state" != "failed" ]]; then
    echo "{\"kind\": \"exit_supervisor_deploy_check\", \"refusals\": [\"DEPLOY_REFUSED_JOB_UNIT_ACTIVE\"], \"unit\": \"$unit\"}"
    refused=1
  fi
done
if [[ "$refused" -ne 0 ]]; then
  echo "DEPLOY_REFUSED" >&2
  exit 2
fi
echo "deploy allowed"

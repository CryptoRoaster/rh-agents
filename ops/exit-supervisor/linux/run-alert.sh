#!/usr/bin/env bash
# One alert pass under systemd: `python -m src.supervisor.main alert`.
# Started by rh-agents-exit-alert.timer every 60 seconds. Reads the read-only
# health verdict and sends news through the configured transport (Telegram,
# credentials from systemd). Output goes to the journal: codes only.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
PYTHON="${RH_AGENTS_PYTHON:-$REPO/backend/.venv/bin/python}"
if [[ ! -x "$PYTHON" ]]; then
  echo "exit-alert: refused PYTHON_MISSING"
  exit 127
fi
cd "$REPO/backend"
exec "$PYTHON" -m src.supervisor.main alert

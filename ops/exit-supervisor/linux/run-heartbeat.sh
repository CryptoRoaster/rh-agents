#!/usr/bin/env bash
# One signed heartbeat to the dead-man receiver under systemd.
# `python -m src.supervisor.main heartbeat` prints one line; SSH delivers it to
# the receiver, whose authorized key forces `python -m src.supervisor.deadman
# receive`. The SSH key and the receiver's host key are systemd credentials;
# the host key is pinned, so the line cannot go anywhere else.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
PYTHON="${RH_AGENTS_PYTHON:-$REPO/backend/.venv/bin/python}"
SSH="${RH_AGENTS_SSH:-ssh}"
TARGET="${RH_AGENTS_HEARTBEAT_TARGET:-unset}"
PORT="${RH_AGENTS_HEARTBEAT_PORT:-22}"
CREDENTIALS="${CREDENTIALS_DIRECTORY:-}"
if [[ "$TARGET" == "unset" || ! "$TARGET" =~ ^([a-z_][a-z0-9_-]{0,31}@)?[A-Za-z0-9][A-Za-z0-9.-]{0,252}$ ]]; then
  echo "heartbeat: refused HEARTBEAT_TARGET_NOT_CONFIGURED"
  exit 78
fi
if [[ ! "$PORT" =~ ^[1-9][0-9]{0,4}$ ]]; then
  echo "heartbeat: refused HEARTBEAT_PORT_INVALID"
  exit 78
fi
if [[ -z "$CREDENTIALS" ]]; then
  echo "heartbeat: refused HEARTBEAT_CREDENTIALS_MISSING"
  exit 78
fi
cd "$REPO/backend"
line="$("$PYTHON" -m src.supervisor.main heartbeat)"
printf '%s\n' "$line" | "$SSH" -T -F /dev/null \
  -o BatchMode=yes -o ConnectTimeout=10 -o IdentitiesOnly=yes \
  -o StrictHostKeyChecking=yes -o "UserKnownHostsFile=$CREDENTIALS/heartbeat-known-hosts" \
  -o GlobalKnownHostsFile=/dev/null -p "$PORT" \
  -i "$CREDENTIALS/heartbeat-ssh-key" "$TARGET"

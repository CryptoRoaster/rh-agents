#!/usr/bin/env bash
# Read-only health of the exit-only supervisor (Linux). Writes nothing.
# Exit code: 0 HEALTHY, 1 DEGRADED, 2 CRITICAL, 3 STOPPED.
set -euo pipefail
# shellcheck source=common.sh
source "$(dirname "${BASH_SOURCE[0]}")/common.sh"
supervisor health

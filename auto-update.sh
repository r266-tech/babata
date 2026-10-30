#!/usr/bin/env bash
# Compatibility entrypoint. Canonical auto-update logic lives in scripts/.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec "$SCRIPT_DIR/scripts/auto-update.sh" "$@"

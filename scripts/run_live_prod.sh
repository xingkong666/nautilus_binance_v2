#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

if [[ "${CONFIRM_LIVE:-}" != "YES" ]]; then
  echo "Refusing to start live trading without CONFIRM_LIVE=YES" >&2
  exit 1
fi

STRATEGY_CONFIG="${LIVE_STRATEGY_CONFIG:-configs/strategies/vegas_tunnel.yaml}"
LOG_LEVEL="${LOG_LEVEL:-WARNING}"

echo "[1/2] Running live readiness checks..."
uv run python scripts/check_live_readiness.py \
  --env prod \
  --log-level "$LOG_LEVEL" \
  --strategy-config "$STRATEGY_CONFIG" \
  --check-account-snapshot

echo "[2/2] Starting live trading process..."
exec uv run python -m src.app.bootstrap \
  --env prod \
  --log-level "$LOG_LEVEL" \
  --strategy-config "$STRATEGY_CONFIG" \
  "$@"

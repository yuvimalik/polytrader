#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# Load .env if present
if [ -f .env ]; then
    set -a
    source .env
    set +a
fi

# Default to dry-run if no args provided
ARGS="${@:---dry-run}"

echo "[$(date -Iseconds)] Starting polytrader bot..."

while true; do
    python3 polymarket_btc_bot.py $ARGS
    EXIT_CODE=$?
    echo "[$(date -Iseconds)] Bot exited with code $EXIT_CODE"

    if [ $EXIT_CODE -eq 0 ]; then
        echo "Clean exit -- stopping"
        break
    fi

    echo "Restarting in 10 seconds..."
    sleep 10
done

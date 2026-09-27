#!/bin/sh
# Safe service launcher. The guardian is opt-in and is never restart-looped when
# credentials are absent or invalid.
set -eu

BOT_PID=""
GUARDIAN_PID=""
LIVE_ARG=""
if [ "${1:-}" = "--live" ]; then
    LIVE_ARG="--live"
elif [ "$#" -ne 0 ]; then
    echo "Usage: $0 [--live]" >&2
    exit 2
fi

cleanup() {
    echo "Received shutdown signal — stopping services"
    [ -z "$GUARDIAN_PID" ] || kill "$GUARDIAN_PID" 2>/dev/null || true
    [ -z "$BOT_PID" ] || kill "$BOT_PID" 2>/dev/null || true
    [ -z "$GUARDIAN_PID" ] || wait "$GUARDIAN_PID" 2>/dev/null || true
    [ -z "$BOT_PID" ] || wait "$BOT_PID" 2>/dev/null || true
}
trap cleanup TERM INT EXIT

if [ -n "$LIVE_ARG" ]; then
    python3 hyperliquid_bot.py --live &
else
    python3 hyperliquid_bot.py &
fi
BOT_PID=$!
echo "Bot/dashboard started"

if [ -n "$LIVE_ARG" ] && [ "${GUARDIAN_ENABLE_LIVE_ACTIONS:-false}" = "true" ]; then
    python3 guardian.py --live-actions &
    GUARDIAN_PID=$!
    echo "Guardian live actions requested; guardian started"
else
    echo "Guardian live actions disabled (safe default)"
fi

wait "$BOT_PID"

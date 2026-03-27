#!/usr/bin/env bash
# Ensure the skill-manager server is running. Idempotent — safe to call on every session start.
set -euo pipefail

PORT=8421
SCRIPT_DIR="${CLAUDE_PLUGIN_ROOT:-$(cd "$(dirname "$0")/.." && pwd)}"
PIDFILE="${SCRIPT_DIR}/.server.pid"
LOG="${SCRIPT_DIR}/.server.log"

# Check if already running
if [ -f "$PIDFILE" ]; then
    PID=$(cat "$PIDFILE" 2>/dev/null || echo "")
    if [ -n "$PID" ] && kill -0 "$PID" 2>/dev/null; then
        # Verify it's actually listening
        if curl -sf -o /dev/null --connect-timeout 1 "http://127.0.0.1:${PORT}/api/skills" 2>/dev/null; then
            exit 0
        fi
    fi
    rm -f "$PIDFILE"
fi

# Check if something else is on the port
if curl -sf -o /dev/null --connect-timeout 1 "http://127.0.0.1:${PORT}/" 2>/dev/null; then
    exit 0
fi

# Start server in background
nohup python3 "${SCRIPT_DIR}/serve.py" > "$LOG" 2>&1 &
echo $! > "$PIDFILE"

# Wait briefly for it to come up
for i in 1 2 3; do
    sleep 0.3
    if curl -sf -o /dev/null --connect-timeout 1 "http://127.0.0.1:${PORT}/" 2>/dev/null; then
        exit 0
    fi
done

exit 0

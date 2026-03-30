#!/usr/bin/env bash
# Ensure the skill-manager server is running. Idempotent — safe to call on every session start.
set -euo pipefail

PORT=8421
SCRIPT_DIR="${CLAUDE_PLUGIN_ROOT:-$(cd "$(dirname "$0")/.." && pwd)}"
DATA_DIR="${HOME}/.claude/skill-manager-data"
mkdir -p "$DATA_DIR"
PIDFILE="${DATA_DIR}/.server.pid"
LOG="${DATA_DIR}/.server.log"

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

# Wait briefly for it to come up, then open browser on fresh start
for i in 1 2 3; do
    sleep 0.3
    if curl -sf -o /dev/null --connect-timeout 1 "http://127.0.0.1:${PORT}/" 2>/dev/null; then
        open "http://127.0.0.1:${PORT}" 2>/dev/null || true
        exit 0
    fi
done

exit 0

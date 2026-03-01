#!/usr/bin/env bash
set -euo pipefail

FACTORY_DIR="${FACTORY_HOME:-/opt/factory}"
LOCKFILE="$FACTORY_DIR/deploy.lock"
LOGFILE="$FACTORY_DIR/deploy.log"
ORCH_DIR="$FACTORY_DIR/orchestrator"
VENV="$FACTORY_DIR/.venv"
API_URL="${FACTORY_API_URL:-http://localhost:8100/api}"
POLL_INTERVAL=15
POLL_TIMEOUT=2100  # 35 minutes

# Redirect all output to log file (owned by this process, survives parent death)
exec >> "$LOGFILE" 2>&1

log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*"
}

exec 200>"$LOCKFILE"
if ! flock -n 200; then
    log "Another deploy is already running. Waiting for lock..."
    flock 200
fi

log "=== Deploy started ==="

cd "$FACTORY_DIR"

# Pull latest code
BEFORE=$(git rev-parse HEAD)
git pull --ff-only origin main
AFTER=$(git rev-parse HEAD)

if [ "$BEFORE" = "$AFTER" ]; then
    log "Already up-to-date at $AFTER. Exiting."
    exit 0
fi

log "Updated $BEFORE -> $AFTER"

# Always reinstall editable (cheap, ensures correct path)
log "Reinstalling editable package..."
"$VENV/bin/pip" install -e "$ORCH_DIR"

# Wait for running agents to finish
log "Checking for running agents..."
elapsed=0
while true; do
    agents=$(curl -sf "$API_URL/agents" 2>/dev/null || echo "[]")
    count=$(echo "$agents" | python3 -c "import sys,json; print(len(json.load(sys.stdin)))" 2>/dev/null || echo "0")

    if [ "$count" = "0" ]; then
        log "No agents running. Proceeding with restart."
        break
    fi

    if [ "$elapsed" -ge "$POLL_TIMEOUT" ]; then
        log "WARNING: Timed out after ${POLL_TIMEOUT}s waiting for agents. Restarting anyway."
        break
    fi

    log "Waiting for $count agent(s) to finish... (${elapsed}s elapsed)"
    sleep "$POLL_INTERVAL"
    elapsed=$((elapsed + POLL_INTERVAL))
done

# Restart the service
SERVICE_NAME="${FACTORY_SERVICE:-factory-orchestrator}"
log "Restarting $SERVICE_NAME..."
systemctl restart "$SERVICE_NAME"

# Wait a moment and verify
sleep 2
if systemctl is-active --quiet "$SERVICE_NAME"; then
    log "=== Deploy successful ==="
else
    log "ERROR: $SERVICE_NAME failed to start!"
    systemctl status "$SERVICE_NAME" || true
    exit 1
fi

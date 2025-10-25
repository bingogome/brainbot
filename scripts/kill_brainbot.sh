#!/usr/bin/env bash

set -euo pipefail

REMOTE_USER="thor"
REMOTE_ADDR="192.168.22.117"
REMOTE_HOST="${REMOTE_USER}@${REMOTE_ADDR}"
REMOTE_PWD="12"

echo "[kill_brainbot] Stopping remote processes on ${REMOTE_HOST}..."

# Kill remote processes
sshpass -p "${REMOTE_PWD}" ssh -o StrictHostKeyChecking=no "${REMOTE_HOST}" << 'EOF'
echo "Killing run_all.py processes..."
pkill -f "run_all.py" || echo "No run_all.py processes found"

echo "Killing Python processes in brainbot..."
pkill -f "scripts/remote/run_all.py" || echo "No matching processes found"
pkill -f "scripts/hub/run_all.py" || echo "No matching processes found"

echo "Remote cleanup complete"
EOF

echo "[kill_brainbot] Stopping local hub processes..."
pkill -f "scripts/hub/run_all.py" || echo "No local hub processes found"

echo "[kill_brainbot] Cleanup complete. You may need to manually close terminal windows."

#!/usr/bin/env bash

set -euo pipefail

REMOTE_USER="thor"
REMOTE_ADDR="192.168.22.117"
REMOTE_HOST="${REMOTE_USER}@${REMOTE_ADDR}"
REMOTE_PWD="12"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REMOTE_YAML_DIR="${SCRIPT_DIR}/remote"
HUB_YAML_DIR="${SCRIPT_DIR}/hub"

# Function to extract ports from YAML files
extract_ports() {
    local yaml_dir="$1"
    local ports=()
    
    if [[ ! -d "$yaml_dir" ]]; then
        echo "[]"
        return
    fi
    
    # Find all YAML files and extract port numbers
    for yaml_file in "$yaml_dir"/*.yaml; do
        if [[ -f "$yaml_file" ]]; then
            # Extract port values (handles "port: 1234" format)
            while IFS= read -r line; do
                if [[ "$line" =~ port:[[:space:]]*([0-9]+) ]]; then
                    ports+=("${BASH_REMATCH[1]}")
                fi
            done < "$yaml_file"
        fi
    done
    
    # Remove duplicates and return as space-separated list
    printf '%s\n' "${ports[@]}" | sort -u | tr '\n' ' '
}

# Function to kill processes on specific ports
kill_ports() {
    local ports="$1"
    local location="$2"
    
    if [[ -z "$ports" ]]; then
        echo "[$location] No ports found"
        return
    fi
    
    echo "[$location] Killing processes on ports: $ports"
    
    for port in $ports; do
        if command -v lsof >/dev/null 2>&1; then
            # Use lsof if available
            local pids=$(lsof -ti:$port 2>/dev/null || true)
            if [[ -n "$pids" ]]; then
                echo "  Port $port: killing PIDs $pids"
                kill -9 $pids 2>/dev/null || true
            fi
        elif command -v ss >/dev/null 2>&1; then
            # Fallback to ss + manual PID extraction
            local pids=$(ss -lptn "sport = :$port" 2>/dev/null | grep -oP 'pid=\K[0-9]+' || true)
            if [[ -n "$pids" ]]; then
                echo "  Port $port: killing PIDs $pids"
                kill -9 $pids 2>/dev/null || true
            fi
        else
            echo "  Port $port: neither lsof nor ss available, cannot kill"
        fi
    done
}

echo "[kill_brainbot] Extracting ports from YAML configurations..."

# Extract ports from remote and hub configs
REMOTE_PORTS=$(extract_ports "$REMOTE_YAML_DIR")
HUB_PORTS=$(extract_ports "$HUB_YAML_DIR")

echo "[kill_brainbot] Remote ports: ${REMOTE_PORTS:-none}"
echo "[kill_brainbot] Hub ports: ${HUB_PORTS:-none}"

# Kill local (hub) processes
echo ""
echo "[kill_brainbot] Stopping local hub processes..."
kill_ports "$HUB_PORTS" "local"

# Kill remote processes via SSH
echo ""
echo "[kill_brainbot] Stopping remote processes on ${REMOTE_HOST}..."

sshpass -p "${REMOTE_PWD}" ssh -o StrictHostKeyChecking=no "${REMOTE_HOST}" bash << EOF
    set -euo pipefail
    
    echo "Killing processes on remote ports: ${REMOTE_PORTS}"
    
    for port in ${REMOTE_PORTS}; do
        if command -v lsof >/dev/null 2>&1; then
            pids=\$(lsof -ti:\$port 2>/dev/null || true)
            if [[ -n "\$pids" ]]; then
                echo "  Port \$port: killing PIDs \$pids"
                kill -9 \$pids 2>/dev/null || true
            fi
        elif command -v ss >/dev/null 2>&1; then
            pids=\$(ss -lptn "sport = :\$port" 2>/dev/null | grep -oP 'pid=\K[0-9]+' || true)
            if [[ -n "\$pids" ]]; then
                echo "  Port \$port: killing PIDs \$pids"
                kill -9 \$pids 2>/dev/null || true
            fi
        else
            echo "  Port \$port: neither lsof nor ss available, cannot kill"
        fi
    done
    
    echo "Remote cleanup complete"
EOF

echo ""
echo "[kill_brainbot] Cleanup complete. Terminal windows may remain open."

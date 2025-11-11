#!/usr/bin/env bash

# Helper script to wait for the mode socket to be ready
# Usage: ./wait_for_socket.sh [socket_path] [timeout_seconds]

SOCKET_PATH="${1:-/tmp/brainbot_modesock}"
TIMEOUT="${2:-30}"
ELAPSED=0

echo "Waiting for socket at ${SOCKET_PATH}..."

while [ $ELAPSED -lt $TIMEOUT ]; do
    if [ -S "${SOCKET_PATH}" ]; then
        echo "Socket ready at ${SOCKET_PATH}"
        exit 0
    fi
    sleep 1
    ELAPSED=$((ELAPSED + 1))
    
    if [ $((ELAPSED % 5)) -eq 0 ]; then
        echo "Still waiting... (${ELAPSED}s/${TIMEOUT}s)"
    fi
done

echo "ERROR: Timeout waiting for socket at ${SOCKET_PATH}"
exit 1

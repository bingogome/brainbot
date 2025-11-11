#!/usr/bin/env bash

set -euo pipefail

REMOTE_USER="thor"
REMOTE_ADDR="192.168.22.117"
REMOTE_HOST="${REMOTE_USER}@${REMOTE_ADDR}"
REMOTE_PWD="12"
REMOTE_DIR="~/Devs/wip/brainbot"
REMOTE_CONDA_ENV="xle"

# Pass all arguments to send_mode_command.py on the remote host
sshpass -p "${REMOTE_PWD}" ssh -t -o StrictHostKeyChecking=no "${REMOTE_HOST}" \
    "cd ${REMOTE_DIR} && source ~/miniconda3/etc/profile.d/conda.sh && conda activate ${REMOTE_CONDA_ENV} && python scripts/remote/send_mode_command.py $*"

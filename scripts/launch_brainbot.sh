#!/usr/bin/env bash

set -euo pipefail

REMOTE_USER="thor"
REMOTE_ADDR="192.168.22.117"
REMOTE_HOST="${REMOTE_USER}@${REMOTE_ADDR}"
REMOTE_PWD="12"
REMOTE_CONDA_ENV="xle"
REMOTE_DIR="~/Devs/wip/brainbot"
HUB_HOST="192.168.22.171"
HUB_CONDA_ENV="base"
HUB_DIR="~/Downloads/software/wip/jetson_deploy/brainbot"

run_terminal() {
    if command -v gnome-terminal >/dev/null 2>&1; then
        gnome-terminal -- bash -lc "$1; exec bash"
    elif command -v xterm >/dev/null 2>&1; then
        xterm -e bash -lc "$1; exec bash"
    else
        echo "[launch] neither gnome-terminal nor xterm is available" >&2
        exit 1
    fi
}

CMD_COMMAND="cd ${REMOTE_DIR} && source ~/miniconda3/etc/profile.d/conda.sh && conda activate ${REMOTE_CONDA_ENV} && export HUB_HOST=${HUB_HOST} && python scripts/remote/run_all.py command --mode-socket /tmp/brainbot_modesock; source ~/miniconda3/etc/profile.d/conda.sh && conda activate ${REMOTE_CONDA_ENV} && exec bash --norc --noprofile -i"
CMD_ROBOT="cd ${REMOTE_DIR} && source ~/miniconda3/etc/profile.d/conda.sh && conda activate ${REMOTE_CONDA_ENV} && export HUB_HOST=${HUB_HOST} && python scripts/remote/run_all.py robot; source ~/miniconda3/etc/profile.d/conda.sh && conda activate ${REMOTE_CONDA_ENV} && exec bash --norc --noprofile -i"
CMD_SHELL="cd ${REMOTE_DIR} && source ~/miniconda3/etc/profile.d/conda.sh && conda activate ${REMOTE_CONDA_ENV} && exec bash --norc --noprofile -i"
CMD_HUB="cd ${HUB_DIR} && source ~/miniconda3/etc/profile.d/conda.sh && conda activate ${HUB_CONDA_ENV} && python scripts/hub/run_all.py; source ~/miniconda3/etc/profile.d/conda.sh && conda activate ${HUB_CONDA_ENV} && exec bash --norc --noprofile -i"

SSH_CMD="sshpass -p '${REMOTE_PWD}' ssh -o StrictHostKeyChecking=no"
SSH_T_CMD="sshpass -p '${REMOTE_PWD}' ssh -t -o StrictHostKeyChecking=no"

run_terminal "${SSH_CMD} ${REMOTE_HOST} 'bash -lc \"${CMD_COMMAND}\"'"
run_terminal "${SSH_CMD} ${REMOTE_HOST} 'bash -lc \"${CMD_ROBOT}\"'"
run_terminal "${SSH_T_CMD} ${REMOTE_HOST} 'bash -lc \"${CMD_SHELL}\"'"
run_terminal "bash -lc '${CMD_HUB}'"

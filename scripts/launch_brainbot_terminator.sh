#!/usr/bin/env bash

set -euo pipefail

if ! command -v terminator >/dev/null 2>&1; then
    echo "[launch] terminator is not installed or not in PATH" >&2
    exit 1
fi

REMOTE_HOST="thor@192.168.0.139"
REMOTE_ENV="source ~/miniconda3/bin/activate xle"
REMOTE_DIR="~/Devs/wip/brainbot"
HUB_HOST="192.168.22.72"
HUB_DIR="~/Downloads/software/brainbot"
HUB_ENV="source ~/miniconda3/bin/activate base"

cmd1="ssh ${REMOTE_HOST} 'bash -lc \"${REMOTE_ENV} && cd ${REMOTE_DIR} && export HUB_HOST=${HUB_HOST} && python scripts/remote/run_all.py command --mode-socket /tmp/brainbot_modesock\"'"
cmd2="ssh ${REMOTE_HOST} 'bash -lc \"${REMOTE_ENV} && cd ${REMOTE_DIR} && export HUB_HOST=${HUB_HOST} && python scripts/remote/run_all.py robot\"'"
cmd3="ssh -t ${REMOTE_HOST} 'bash -lc \"${REMOTE_ENV} && cd ${REMOTE_DIR} && exec bash\"'"
cmd4="bash -lc '${HUB_ENV} && cd ${HUB_DIR} && python scripts/hub/run_all.py'"

CONFIG_FILE=$(mktemp)

cleanup() {
    rm -f "$CONFIG_FILE"
}
trap cleanup EXIT

cat >"$CONFIG_FILE" <<EOF
[global_config]
  suppress_multiple_term_dialog = True
  focus = system

[profiles]
  [[brainbot_command]]
    use_custom_command = True
    custom_command = ${cmd1}
    hold = True
    scrollback_infinite = True
    title = Command Service
  [[brainbot_robot]]
    use_custom_command = True
    custom_command = ${cmd2}
    hold = True
    scrollback_infinite = True
    title = Robot Service
  [[brainbot_remote_shell]]
    use_custom_command = True
    custom_command = ${cmd3}
    hold = True
    scrollback_infinite = True
    title = Remote Shell
  [[brainbot_hub]]
    use_custom_command = True
    custom_command = ${cmd4}
    hold = True
    scrollback_infinite = True
    title = Hub Manager

[layouts]
  [[brainbot_layout]]
    [[[window0]]]
      type = Window
      parent = ""
      size = 1600, 900
    [[[pane_vertical]]]
      type = VPaned
      parent = window0
      position = 450
    [[[pane_top]]]
      type = HPaned
      parent = pane_vertical
      position = 800
    [[[pane_bottom]]]
      type = HPaned
      parent = pane_vertical
      position = 800
    [[[terminal1]]]
      type = Terminal
      parent = pane_top
      profile = brainbot_command
    [[[terminal2]]]
      type = Terminal
      parent = pane_top
      profile = brainbot_robot
    [[[terminal3]]]
      type = Terminal
      parent = pane_bottom
      profile = brainbot_remote_shell
    [[[terminal4]]]
      type = Terminal
      parent = pane_bottom
      profile = brainbot_hub

[plugins]
EOF

terminator -g "$CONFIG_FILE" -l brainbot_layout

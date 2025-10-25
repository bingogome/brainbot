#!/usr/bin/env bash

set -euo pipefail

REMOTE_HOST="thor@192.168.0.139"
REMOTE_ENV="source ~/miniconda3/bin/activate xle"
REMOTE_DIR="~/Devs/wip/brainbot"
HUB_HOST="192.168.22.72"
HUB_DIR="~/Downloads/software/brainbot"
HUB_ENV="source ~/miniconda3/bin/activate base"
MODE_SOCKET="/tmp/brainbot_modesock"

remote_command_body="${REMOTE_ENV} && cd ${REMOTE_DIR} && export HUB_HOST=${HUB_HOST} && rm -f ${MODE_SOCKET} && python scripts/remote/run_all.py command --mode-dispatcher socket --mode-socket ${MODE_SOCKET}"
remote_robot_body="${REMOTE_ENV} && cd ${REMOTE_DIR} && export HUB_HOST=${HUB_HOST} && python scripts/remote/run_all.py robot"
remote_shell_body="${REMOTE_ENV} && cd ${REMOTE_DIR} && exec bash"
hub_body="${HUB_ENV} && cd ${HUB_DIR} && python scripts/hub/run_all.py"

cmd_command=$(cat <<EOF
ssh ${REMOTE_HOST} 'bash -lc '"'"'${remote_command_body}'"'"''
EOF
)

cmd_robot=$(cat <<EOF
ssh ${REMOTE_HOST} 'bash -lc '"'"'${remote_robot_body}'"'"''
EOF
)

cmd_shell=$(cat <<EOF
ssh -t ${REMOTE_HOST} 'bash -lc '"'"'${remote_shell_body}'"'"''
EOF
)

use_gnome() {
    local title="$1"
    local command="$2"
    gnome-terminal --title="${title}" -- bash -lc "${command}; exec bash" &
    sleep 0.2
}

launch_with_gnome() {
    echo "[launch] using gnome-terminal windows"
    use_gnome "Command Service" "${cmd_command}"
    use_gnome "Robot Service" "${cmd_robot}"
    use_gnome "Remote Shell" "${cmd_shell}"
    use_gnome "Hub Manager" "${hub_body}"
}

launch_with_terminator() {
    local config_file
    config_file=$(mktemp)

    trap "rm -f '$config_file'" EXIT

    cat >"$config_file" <<EOF
[global_config]
  suppress_multiple_term_dialog = True
  focus = system

[profiles]
  [[brainbot_command]]
    use_custom_command = True
    custom_command = ${cmd_command}
    hold = True
    scrollback_infinite = True
    title = Command Service
  [[brainbot_robot]]
    use_custom_command = True
    custom_command = ${cmd_robot}
    hold = True
    scrollback_infinite = True
    title = Robot Service
  [[brainbot_remote_shell]]
    use_custom_command = True
    custom_command = ${cmd_shell}
    hold = True
    scrollback_infinite = True
    title = Remote Shell
  [[brainbot_hub]]
    use_custom_command = True
    custom_command = bash -lc '${hub_body}'
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

    terminator -g "$config_file" -l brainbot_layout
}

if command -v gnome-terminal >/dev/null 2>&1; then
    launch_with_gnome
elif command -v terminator >/dev/null 2>&1; then
    launch_with_terminator
else
    echo "[launch] neither gnome-terminal nor terminator is available" >&2
    exit 1
fi

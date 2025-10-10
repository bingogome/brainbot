# brainbot

Distributed control stack for Brainbot deployments with a built-in web dashboard.

## Components
- `brainbot_core`: shared configs, teleop endpoint parsing, message serialization.
- `brainbot_control_service`: robot-side agent that streams observations and applies actions.
- `brainbot_command_service`: mode hub on Thor; connects to local/remote teleops, GR00T, idle, and feeds the visualizer.
- `brainbot_mode_dispatcher`: mode-event abstractions and CLI dispatcher.
- `brainbot_teleop_server`: exposes teleop devices (leader arms, AR controllers) as network endpoints.
- `brainbot_webviz`: HTTP dashboard with charts, image previews, and current mode display.
- `brainbot/scripts`: per-device utilities (`pc/`, `thor/`).

## Typical Setup
### Jetson Thor
1. Run the GR00T policy server.
2. Start the robot agent:
   ```bash
   python brainbot/scripts/thor/run_thor_robot.py --config brainbot/scripts/thor/thor_robot.yaml
   ```
3. Start the command hub (webviz runs automatically if configured):
   ```bash
   python brainbot/scripts/thor/run_thor_command.py --config brainbot/scripts/thor/thor_command.yaml
   ```
   Browse to `http://<thor-ip>:8080/` to see live charts, current mode, and camera imagery.

### Teleop Providers
Define teleops as either local devices (`mode: local`) or remote servers (`mode: remote`). Example remote setups:

- **Leader arms (operator PC):**
  ```bash
  python brainbot/scripts/pc/run_teleop_server.py --config brainbot/scripts/pc/leader_teleop.yaml
  ```
- **AR controller (Quest bridge):**
  ```bash
  python brainbot/scripts/pc/run_teleop_server.py --config brainbot/scripts/pc/ar_teleop.yaml
  ```

### Mode Control
Use the CLI on Thor to switch sources safely:
- `{"teleop": "leader"}`
- `{"teleop": "ar"}`
- `{"infer": "Pick up the block using the left arm and transfer!"}`
- `{"idle": ""}`

The dashboard updates in real time—verify teleop commands before spinning up the robot agent, then keep the browser open to monitor actions, images, and mode changes.

## Testing Tips
1. Start teleop servers with virtual devices to validate connectivity.
2. Run the command hub (with webviz) before connecting the robot to inspect action streams.
3. Launch the robot agent with a mock robot to confirm the loop before enabling torque.
4. Check networking (`telnet <teleop-host> <port>`) and watch the dashboard charts for unexpected spikes.

Notes: include base64 encoded images (data:image/...) in observation payloads to see previews.

To preview teleop/AI commands without the robot agent, run:

```
python brainbot/scripts/thor/run_thor_preview.py --config brainbot/scripts/thor/thor_command.yaml
```

Stop it with CTRL+C when you are ready to start the real robot agent.

# brainbot

Distributed control stack for Brainbot deployments.

## Components
- `brainbot_core`: shared configs and message serialization.
- `brainbot_control_service`: robot-side agent that talks to hardware.
- `brainbot_command_service`: mode hub running on Thor; connects to local or remote providers and GR00T.
- `brainbot_mode_dispatcher`: mode-event abstractions and CLI dispatcher.
- `brainbot_teleop_server`: exposes teleop devices over the network.
- `brainbot_webviz`: HTTP dashboard for observing actions and observations.
- `brainbot/scripts`: per-device launch utilities (`pc/`, `thor/`).

## Typical Setup
### Jetson Thor
1. Run the GR00T policy server.
2. Start the robot agent:
   ```bash
   python brainbot/scripts/thor/run_thor_robot.py --config brainbot/scripts/thor/thor_robot.yaml
   ```
3. Start the command hub:
   ```bash
   python brainbot/scripts/thor/run_thor_command.py --config brainbot/scripts/thor/thor_command.yaml
   ```
   If `webviz` is enabled, open `http://<thor-ip>:8080/` from another machine to see the live dashboard.

### Teleop Providers
You can expose teleop devices locally on Thor (define them with `mode: local` in `thor_command.yaml`) or remotely via action servers.

- **Leader arms (remote example):**
  ```bash
  python brainbot/scripts/pc/run_teleop_server.py --config brainbot/scripts/pc/leader_teleop.yaml
  ```
- **AR teleop (remote example):**
  ```bash
  python brainbot/scripts/pc/run_teleop_server.py --config brainbot/scripts/pc/ar_teleop.yaml
  ```

### Mode Control
The command service on Thor runs a CLI dispatcher. In the Thor console send JSON commands:
- `{"teleop": "leader"}`
- `{"teleop": "ar"}`
- `{"infer": "Pick up the block using the left arm and transfer!"}`
- `{"idle": ""}`

## Testing Tips
1. Start teleop servers without hardware to check connectivity.
2. Run the robot agent with a mock robot before touching real arms.
3. Verify networking (`telnet <teleop-host> <port>`).
4. Confirm the dashboard updates before enabling live motion.

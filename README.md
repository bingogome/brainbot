# brainbot

Brainbot is a modular hub-and-spoke control stack for combining teleoperation, AI policy inference (e.g. GR00T), and lightweight visualization across multiple machines. It enables setups where an edge device runs the follower robot and AI policy, while remote PCs host leader teleop devices, AR controllers, or other modalities.

## Architecture

```
┌───────────────┐        ┌──────────────────┐      ┌───────────────────────┐
│ Teleop Server │◄──────►│ Teleop Action    │      │ Web Dashboard         │
│(on PC or Edge)│    ZMQ │ Server(s)        │      │ (brainbot_webviz)     │
│ run_teleop_…  │        └──────────────────┘      └───────────────────────┘
└───────────────┘                                         ▲
                                                          │ HTTP
                                                          │
                                          ┌───────────────┴─────────────────┐
                                          │ Command Hub (on Edge)           │
                                          │ (brainbot_command_service)      │
                                          │ Mode manager + CLI              │
                                          │  • Local/remote teleop provider │
                                          │  • AI provider                  │
                                          │  • WebViz hook                  │
                                          └────────────▲──────────────┬─────┘
                                                       │ ZMQ          │
                                                       │              │
                                          ┌────────────┴──────────────▼──────┐
                                          │ Robot Controller (on Edge)       │
                                          │ (brainbot_control_service)       │
                                          │  • Streams obs/actions           │
                                          │  • Drives hardware               │
                                          └──────────────▲───────────────────┘
                                                         │ │
                                          ┌────────────────▼──────────────────┐
                                          |  AI Policy Server (on PC or Edge) │
                                          └───────────────────────────────────┘
```

- **Teleop servers** expose local devices over ZMQ (one per leader rig or AR bridge)
- **Command hub** selects the active provider (teleop / AI / idle), forwards observations to GR00T when needed, and pushes actions back to the robot
- **Robot agent** streams observations/actions to the hub and executes them on the hardware
- **WebViz** provides an HTTP dashboard (commands, mode info, history charts)

## Packages

| Package | Description |
|---------|-------------|
| `brainbot_core` | Config loaders, dynamic module imports, message serialization |
| `brainbot_command_service` | Thor hub with providers, mode manager, webviz bridge |
| `brainbot_control_service` | Robot-side loop interfacing with motors and command hub |
| `brainbot_mode_dispatcher` | CLI dispatcher (extendable for other event sources) |
| `brainbot_teleop_server` | Lightweight server exposing local teleop actions via ZMQ |
| `brainbot_webviz` | HTTP dashboard for commands and mode visualization |
| `brainbot/scripts` | Launch helpers for Thor and PC roles |

## Typical Workflow

### Jetson Thor
1. Run policy server.
2. Start the command hub (webviz included):
   ```bash
   python brainbot/scripts/thor/run_thor_command.py \
       --config brainbot/scripts/thor/thor_command.yaml
   ```
3. Optionally preview teleop/AI output without moving the robot:
   ```bash
   python brainbot/scripts/thor/run_thor_preview.py \
       --config brainbot/scripts/thor/thor_command.yaml
   ```
4. Launch the robot controller when safe:
   ```bash
   python brainbot/scripts/thor/run_thor_robot.py \
       --config brainbot/scripts/thor/thor_robot.yaml \
       --no-calibrate    # skip prompts if already calibrated
   ```

### Teleop Servers (Operator PC / AR bridge)
Start one server per device:
```bash
python brainbot/scripts/pc/run_teleop_server.py \
    --config brainbot/scripts/pc/leader_teleop.yaml
```
(AR controllers are configured similarly with `mode: local` and their own driver.)

### Mode Control & Visualization
- Control the active provider with CLI commands on Thor:
  - `{"teleop": "leader"}`
  - `{"infer": "Pick up the block using the left arm and transfer!"}`
  - `{"idle": ""}`
- View live command traces and mode status at `http://<thor-ip>:8080/`
- `run_thor_preview.py` keeps the dashboard alive before the robot agent starts

## Config Examples

`brainbot/scripts/thor/thor_command.yaml`:
```yaml
teleops:
  leader:
    mode: remote
    host: 192.168.22.171
    port: 7001
ai:
  host: 127.0.0.1
  port: 6000
webviz:
  host: 0.0.0.0
  port: 8080
```

`brainbot/scripts/pc/leader_teleop.yaml`:
```yaml
teleop:
  mode: local
  config:
    type: bi_so101_leader
    left_arm_port: /dev/ttyACM1
    right_arm_port: /dev/ttyACM0
network:
  host: 0.0.0.0
  port: 7001
```

`brainbot/scripts/thor/thor_robot.yaml` defines the follower robot/camera setup; set `calibrate_on_start: false` if calibration files already exist.

## Testing Tips
1. Start teleop servers with mock devices to verify connectivity.
2. Run `run_thor_command.py` + `run_thor_preview.py` to inspect WebViz before powering the robot.
3. Launch the robot agent with a mock config first (no torque), then enable hardware once you’re confident.
4. Use `telnet <teleop-host> <port>` to validate network reachability.
5. Adjust `observation_adapter` in `brainbot_control_service` if AI modes need full image data (default keeps only numeric values).

## Notes
- Teleop/robot/camera modules are imported dynamically so Draccus can instantiate their configs.
- Remote teleops are pinged and keep socket timeouts to avoid hanging the control loop.
- Switching out of AI mode clears its last instruction; idle is always available as a safe fallback.
- WebViz omits observation payloads and images to stay lightweight; it focuses on command JSON, mode info, and numeric history.

With these pieces, you can teleoperate, run AI policies, and monitor combined systems spanning Thor, PCs, and AR controllers—all through Brainbot’s modular stack.

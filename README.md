# brainbot

Brainbot is a modular hub-and-spoke control stack for combining teleoperation, AI policy inference (e.g. GR00T), multi-camera streaming, and lightweight visualization across multiple machines. It enables setups where a Jetson Thor runs the follower robot and GR00T policy, while remote PCs host leader teleop devices, AR controllers, or other modalities.

## Architecture

```
Command Providers                 Command Hub & Consumers
┌─────────────────┐              ┌─────────────────────────────┐
│ Teleop Server   │◄───ZMQ──────►│ brainbot_command_service    │
│ (leader / AR)   │              │  • mode manager + CLI       │
│ run_teleop_…    │              │  • teleop & AI providers    │
└─────────────────┘              │  • WebViz hook              │
                                 └──────▲───────────────┬──────┘
                                        │ ZMQ           │
                                        │              │
                              ┌─────────┴─────────┐    │
                              │ brainbot_control  │    │
                              │ service (robot)   │    │
                              │  • obs/actions    │    │
                              │  • camera tap     │    │
                              └────────▲──────────┘    │
                                       │               │
                                       │               │
                          ┌────────────┴───────────┐   │
                          │ GR00T Policy Server    │◄──┘
                          └────────────────────────┘
                                       │
                                       │ PUB/SUB (JPEG frames)
                                       ▼
                               Camera stream consumers
                                       │
                                       │ HTTP
                                       ▼
                               Web dashboard (webviz)
```

- **Teleop servers** expose local devices over ZMQ (one per leader rig or AR bridge).
- **Command hub** selects the active provider (teleop / AI / idle), forwards observations to GR00T, and pushes actions back to the robot. It also feeds the WebViz dashboard.
- **Robot agent** streams observations/actions to the hub, executes them on hardware, and mirrors configured camera feeds to the visualization streamer.
- **Camera streamer** reuses the raw frames already collected by LeRobot, JPEG-encodes them, and publishes via ZMQ PUB/SUB without touching the GR00T pipeline.
- **WebViz** provides a lightweight command and mode dashboard.

## Packages

| Package                    | Description                                                   |
|----------------------------|---------------------------------------------------------------|
| `brainbot_core`            | Config loaders, dynamic module imports, message serialization |
| `brainbot_command_service` | Thor hub with providers, mode manager, webviz bridge          |
| `brainbot_control_service` | Robot-side loop interfacing with motors and command hub       |
| `brainbot_mode_dispatcher` | CLI dispatcher (extendable for other event sources)           |
| `brainbot_teleop_server`   | Lightweight server exposing local teleop actions via ZMQ      |
| `brainbot_webviz`          | HTTP dashboard for command history and mode status            |
| `brainbot/scripts`         | Launch helpers for Thor (`thor/`) and PC (`pc/`) roles        |

## Typical Workflow

### Jetson Thor
1. Run the GR00T policy server.
2. Start the command hub (WebViz included):
   ```bash
   python brainbot/scripts/thor/run_thor_command.py \
       --config brainbot/scripts/thor/thor_command.yaml
   ```
3. (Optional) Preview teleop/AI output without moving the robot:
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
- View live command traces and mode status at `http://<thor-ip>:8080/`.
- `run_thor_preview.py` keeps the dashboard alive before the robot agent starts.
- Camera feeds are duplicated via the streamer: GR00T keeps raw arrays, while visualization clients subscribe to JPEG frames on a PUB/SUB endpoint.

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
camera_stream:
  host: 127.0.0.1
  port: 7005
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

`brainbot/scripts/thor/thor_robot.yaml`:
```yaml
robot:
  type: bi_so101_follower
  left_arm_port: /dev/ttyACM1
  right_arm_port: /dev/ttyACM0
  cameras:
    left:  {type: opencv, index_or_path: 8, width: 640, height: 480, fps: 15}
    right: {type: opencv, index_or_path: 6, width: 640, height: 480, fps: 15}
    top:   {type: opencv, index_or_path: 4, width: 640, height: 480, fps: 15}
network:
  host: 127.0.0.1
  port: 5555
loop_hz: 40
max_missed_actions: 2
calibrate_on_start: true
observation_adapter: identity        # keep raw arrays for GR00T and streaming
camera_stream:
  host: 0.0.0.0
  port: 7005
  quality: 70
  sources:
    - name: left
      path: robot.cameras.left
      fps: 15
    - name: right
      path: robot.cameras.right
      fps: 15
    - name: top
      path: robot.cameras.top
      fps: 15
```

## Camera Streaming Protocol
- **Capture:** LeRobot still acquires frames (no driver changes). When `observation_adapter` is set to `identity`, raw numpy arrays remain in the observation payload handed to GR00T.
- **Publisher:** `brainbot_control_service` JPEG-encodes configured sources in background workers and publishes on a ZMQ `PUB` socket (`tcp://host:port`, topic = camera name). Each message is a MsgPack blob containing `camera`, `timestamp`, `encoding`, `width`, `height`, `quality`, and JPEG bytes.
- **Consumers:** Unity/ROS/diagnostic tools subscribe via `SUB` sockets and decode JPEG frames. This mirrors the length-prefixed JPEG approach from previous projects but scales to multiple consumers. WebViz also subscribes to the same stream and renders previews in-browser.
- **Visualization:** WebViz focuses on commands/modes. If you need inline thumbnails, subscribe to the PUB stream and render independently.

## Testing Tips
1. Start teleop servers with mock devices to verify connectivity.
2. Run `run_thor_command.py` + `run_thor_preview.py` to inspect WebViz and camera streams before powering hardware.
3. Launch the robot agent with a mock config first (no torque), then enable hardware once you’re confident.
4. Validate networking with `telnet <teleop-host> <port>` or a small ZMQ subscriber for camera streams.
5. Choose `observation_adapter: identity` when AI requires full sensor payloads; use `numeric_only` for lightweight numeric-only deployments.

## Notes
- Robot/teleop/camera modules are imported dynamically so Draccus can instantiate their configs from YAML.
- Remote teleops are pinged and set ZMQ timeouts to avoid blocking the control loop.
- Switching out of AI mode clears its last instruction; idle is always available as a safe fallback.
- WebViz omits observation payloads to stay lightweight; use the new camera streamer or preview script for visual inspection.

With these pieces, you can teleoperate, stream multiple cameras, run AI policies, and monitor everything across Thor, PCs, and AR controllers—all within Brainbot’s modular stack.

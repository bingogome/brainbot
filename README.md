# brainbot

Brainbot is a modular hub-and-spoke control stack for combining teleoperation, AI inference, multi-camera streaming, and lightweight visualization. A typical deployment uses an edge compute device (e.g., a Jetson module, but the codebase is not limited to edge device and should be able to run on any device) to run the follower robot and AI policy, while remote PCs host leader teleop devices or AR controllers.

## Architecture

```
Command Providers -> Hub:                   |    Command Consumers:
┌───────────────┐     ┌──────────────────┐  |            ┌───────────────────────┐
│ Teleop Server │◄───►│ Teleop Action    │  |            │ Web Dashboard         │
│(on PC or Edge)│ ZMQ │ Server(s)        │  |            │ (brainbot_webviz)     │
│ run_teleop_…  │     └──────────────────┘  |            └───────────────────────┘
└───────────────┘                           |                      ▲
                                            |                      │ HTTP
┌───────────────────────────────────┐       |                      │
|  AI Policy Server (on PC or Edge) │       |      ┌───────────────┴─────────────────┐
└───────────────────────────────────┘       |      │ Command Hub (on Edge)           │
                                            |      │ (brainbot_command_service)      │
                                            |      │ Mode manager + CLI              │
                                            |      │  • Local/remote teleop provider │
                                            |      │  • AI provider                  │
                                            |      │  • WebViz hook                  │
                                            |      └────────────▲──────────────┬─────┘
                                            |                   │ ZMQ          │
                                            |                   │              │
                                            |      ┌────────────┴──────────────▼──────┐
                                            |      │ Robot Controller (on Edge)       │
                                            |      │ (brainbot_control_service)       │
                                            |      │  • Streams obs/actions           │
                                            |      │  • Drives hardware               |
                                            |      │  • Camera tap                    |
                                            |      └──────────────────────────────────┘                                          
```

## Packages

| Package                    | Description                                                   |
|----------------------------|---------------------------------------------------------------|
| `brainbot_core`            | Config loaders, dynamic module imports, message serialization |
| `brainbot_command_service` | Edge hub with teleop/AI providers, WebViz bridge              |
| `brainbot_control_service` | Robot-side loop interfacing with motors and camera streamer   |
| `brainbot_mode_dispatcher` | CLI dispatcher (swappable for other event sources)            |
| `brainbot_teleop_server`   | Exposes local teleop devices or AR controllers via ZMQ        |
| `brainbot_webviz`          | HTTP dashboard for command history, mode, and camera preview  |
| `brainbot/scripts`         | Launch helpers for edge host (`thor/`) and PC (`pc/`) roles   |

## Typical Workflow

### Edge Computer (e.g., Jetson)
1. Run the GR00T policy server (defaults use `127.0.0.1:5555`).
2. Start the command hub (WebViz + GR00T integration are automatic):
   ```bash
   python brainbot/scripts/thor/run_thor_command.py \
       --config brainbot/scripts/thor/thor_command.yaml
   ```
3. (Optional) Preview teleop/AI output without powering the robot:
   ```bash
   python brainbot/scripts/thor/run_thor_preview.py \
       --config brainbot/scripts/thor/thor_command.yaml
   ```
4. Launch the robot controller when safe:
   ```bash
   python brainbot/scripts/thor/run_thor_robot.py \
       --config brainbot/scripts/thor/thor_robot.yaml \
       --no-calibrate
   ```

### Teleop Servers (Operator PC / AR bridge)
```bash
python brainbot/scripts/pc/run_teleop_server.py \
    --config brainbot/scripts/pc/leader_teleop.yaml
```
(AR controllers are configured similarly using `mode: local`.)

### Mode Control & Visualization
- Send JSON commands in runtime on the edge machine to swap providers:
  - `{"teleop": "leader"}`
  - `{"teleop": "ar"}`
  - `{"teleop": "joycon"}`
  - `{"teleop": "gamepad"}`
  - `{"infer": "Pick up the block using the left arm and transfer!"}`
  - `{"infer": "Open the shelf"}`
  - `{"idle": ""}`
  - `{"shutdown": ""}` (gracefully shuts down robot controller, then the hub)
- View live command traces, numeric history, and camera previews at `http://<edge-ip>:8080/`.
- `run_thor_preview.py` keeps WebViz and camera streams active before the robot agent starts.

## Config Examples

`brainbot/scripts/thor/thor_command.yaml`:
```yaml
teleops:
  leader:
    mode: remote
    host: 192.168.22.171
    port: 7001
    timeout_ms: 1000
  joycon:
    mode: remote
    host: 192.168.22.171
    port: 7002
    timeout_ms: 1000
  gamepad:
    mode: remote
    host: 192.168.22.171
    port: 7003
    timeout_ms: 1000
ai:
  host: 172.17.0.3
  port: 5555
  timeout_ms: 10000
  modality_config_path: scripts/thor/xlerobot_modality.json
  camera_keys: [left, right, top]
  state_keys:
    - left_shoulder_pan.pos
    - left_shoulder_lift.pos
    - left_elbow_flex.pos
    - left_wrist_flex.pos
    - left_wrist_roll.pos
    - left_gripper.pos
    - right_shoulder_pan.pos
    - right_shoulder_lift.pos
    - right_elbow_flex.pos
    - right_wrist_flex.pos
    - right_wrist_roll.pos
    - right_gripper.pos
    - x.vel
    - y.vel
    - theta.vel
    - mount_pan.pos
    - mount_tilt.pos
network:
  host: 127.0.0.1
  port: 6000
webviz:
  host: 0.0.0.0
  port: 8080
camera_stream:
  host: 127.0.0.1
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

RobotControlService now switches to numeric-only observations when a teleop provider is active and brings back preprocessed camera frames automatically while AI mode is running, keeping both workflows responsive.

Adjust the modality path, camera keys, and state keys so they match the GR00T build you deploy (add or remove base/mount keys as needed).

`brainbot/scripts/pc/leader_teleop.yaml`:
```yaml
teleop:
  mode: local
  config:
    type: xlerobot_leader_gamepad
    id: leader
    arms:
      left_arm_port: /dev/ttyACM0
      right_arm_port: /dev/ttyACM1
    base:
      joystick_index: 0
      max_speed_mps: 0.8
      deadzone: 0.15
      yaw_speed_deg: 45
    mount: {}
network:
  host: 0.0.0.0
  port: 7001
```

`brainbot/scripts/thor/thor_robot.yaml`:
```yaml
robot:
  type: xlerobot
  id: follower
  arms:
    left_arm_port: /dev/ttyACM2
    right_arm_port: /dev/ttyACM3
  base:
    port: /dev/ttyACM4
    wheel_radius_m: 0.05
    base_radius_m: 0.125
  mount: {}
  cameras:
    left:  {type: opencv, index_or_path: 8, width: 640, height: 480, fps: 15, enable_mjpeg: true}
    right: {type: opencv, index_or_path: 6, width: 640, height: 480, fps: 15, enable_mjpeg: true}
    top:   {type: opencv, index_or_path: 4, width: 640, height: 480, fps: 15, enable_mjpeg: true}
network:
  host: 127.0.0.1
  port: 6000
loop_hz: 40
max_missed_actions: 2
calibrate_on_start: true
observation_adapter: identity        # start in full mode; dynamic switching keeps teleop fast
observation_preprocess:
  target_height: 224
  target_width: 224
  interpolation: linear
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
- **Capture:** LeRobot still acquires frames; `enable_mjpeg: true` requests MJPEG from cameras to save USB bandwidth when supported.
- **Publisher:** `brainbot_control_service` keeps raw arrays for GR00T, and asynchronously JPEG-encodes configured sources for visualization. Frames publish via ZMQ `PUB` (`tcp://host:port`, topic = camera name). Message format is MsgPack with `camera`, `timestamp`, `encoding`, `width`, `height`, `quality`, and JPEG bytes.
- **Consumers:** Unity, ROS bridges, WebViz, or custom tools subscribe via ZMQ `SUB`. WebViz renders previews automatically.

## Notes
- Robot/teleop/camera modules auto-import at load time so YAML configs “just work”.
- Remote teleops are pinged and keep socket timeouts, preventing the control loop from blocking.
- Switching out of AI mode clears the last instruction; idle is always available as a safe fallback.

With these pieces, you can teleoperate, stream multiple cameras, run GR00T policies, and monitor everything across edge computers, PCs, and AR controllers—all within Brainbot’s modular stack.

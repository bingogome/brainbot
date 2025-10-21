from __future__ import annotations

import argparse
import signal
from pathlib import Path

from lerobot.robots.utils import make_robot_from_config

from brainbot_core.config import EdgeControlConfig, load_edge_config

from . import CameraStreamer, CommandChannelClient, CommandLoop, RobotControlService


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--no-calibrate", action="store_true")
    args = parser.parse_args(argv)

    config: EdgeControlConfig = load_edge_config(args.config)
    robot = make_robot_from_config(config.robot)
    adapter_name = config.observation_adapter.lower()
    if adapter_name == "identity":
        initial_mode = "full"
    elif adapter_name == "numeric_only":
        initial_mode = "numeric"
    else:
        raise ValueError(f"Unknown observation_adapter '{config.observation_adapter}'")

    service = RobotControlService(
        robot=robot,
        preprocess_config=config.observation_preprocess,
        initial_observation_mode=initial_mode,
    )
    client = CommandChannelClient(
        host=config.network.host,
        port=config.network.port,
        timeout_ms=config.network.timeout_ms,
        api_token=config.network.api_token,
    )
    camera_streamer = CameraStreamer(config.camera_stream) if config.camera_stream else None
    loop = CommandLoop(
        service=service,
        client=client,
        rate_hz=config.loop_hz,
        max_missed_actions=config.max_missed_actions,
        fallback_action=config.fallback_action,
        camera_streamer=camera_streamer,
    )

    def shutdown_handler(signum, frame):
        loop.stop()

    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, shutdown_handler)

    service.connect(calibrate=config.calibrate_on_start and not args.no_calibrate)
    client.sync_config(
        {
            "action_keys": list(robot.action_features.keys()),
            "metadata": dict(config.metadata or {}),
        }
    )

    try:
        loop.run()
    finally:
        loop.stop()
        service.disconnect()
        if camera_streamer:
            camera_streamer.close()


if __name__ == "__main__":
    main()

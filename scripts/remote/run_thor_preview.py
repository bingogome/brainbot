from __future__ import annotations

import argparse
import time
from pathlib import Path

from brainbot_control_service.command_client import CommandChannelClient
from brainbot_core.config import load_server_config
from brainbot_core.proto import ObservationMessage

COMMAND_CONFIG = Path(__file__).with_name("thor_command.yaml")


def main() -> None:
    parser = argparse.ArgumentParser(description="Preview teleop/AI commands without running the robot agent")
    parser.add_argument("--config", type=Path, default=COMMAND_CONFIG, help="Thor command YAML config")
    parser.add_argument("--hz", type=float, default=5.0, help="Preview frequency (Hz)")
    args = parser.parse_args()

    config = load_server_config(args.config)
    client = CommandChannelClient(
        host=config.network.host,
        port=config.network.port,
        timeout_ms=config.network.timeout_ms,
        api_token=config.network.api_token,
    )

    interval = 1.0 / max(args.hz, 0.1)
    print(f"Previewing actions at {args.hz:.1f} Hz against {config.network.host}:{config.network.port}")
    try:
        while True:
            obs = ObservationMessage(payload={"preview": True})
            try:
                client.compute_action(obs)
            except TimeoutError:
                print("[preview] command service timeout")
            time.sleep(interval)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()

from __future__ import annotations

import argparse
import importlib
import io
import yaml
from pathlib import Path

import draccus
from lerobot.teleoperators.config import TeleoperatorConfig
from lerobot.teleoperators.utils import make_teleoperator_from_config

from brainbot_teleop_server import TeleopActionServer

# Default to generic teleop server template
DEFAULT_CONFIG = Path(__file__).with_name("teleop_server.yaml")


def _load_yaml(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as handle:
        return yaml.safe_load(handle) or {}


def _load_teleop_config(data: dict) -> TeleoperatorConfig:
    mode = data.get("mode")
    if mode and mode != "local":
        raise ValueError("run_teleop_server only supports local teleop configurations")
    payload = data.get("config", data)
    teleop_type = payload.get("type")
    if teleop_type:
        try:
            importlib.import_module(f"lerobot.teleoperators.{teleop_type}")
        except ModuleNotFoundError:
            pass
    buffer = io.StringIO()
    yaml.safe_dump(payload, buffer)
    buffer.seek(0)
    with draccus.config_type("yaml"):
        return draccus.load(TeleoperatorConfig, buffer)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run a Brainbot teleop action server")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG, help="YAML config for the teleop server")
    args = parser.parse_args()

    raw = _load_yaml(args.config)
    teleop_cfg = _load_teleop_config(raw.get("teleop", {}))
    network = raw.get("network", {})
    host = network.get("host", "0.0.0.0")
    port = int(network.get("port", 7001))
    api_token = raw.get("api_token")

    teleop = make_teleoperator_from_config(teleop_cfg)
    server = TeleopActionServer(
        teleop=teleop,
        host=host,
        port=port,
        api_token=api_token,
    )

    try:
        server.run()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()

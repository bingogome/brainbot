from __future__ import annotations

import argparse
import sys
from pathlib import Path

from brainbot_command_service.run_command_service import main as command_main

DEFAULT_CONFIG = Path(__file__).with_name("thor_command.yaml")


def main() -> None:
    parser = argparse.ArgumentParser(description="Launch Brainbot command service on Jetson Thor")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG, help="Command service YAML config")
    parser.add_argument("--log-level", default="INFO", help="Logging level for the command service")
    args = parser.parse_args()

    command_main(["--config", str(args.config), "--log-level", args.log_level])


if __name__ == "__main__":
    main()

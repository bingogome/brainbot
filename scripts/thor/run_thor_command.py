from __future__ import annotations

import argparse
import sys
from pathlib import Path

from brainbot_command_service.run_command_service import main as command_main

DEFAULT_CONFIG = Path(__file__).with_name("thor_command.yaml")


def main() -> None:
    parser = argparse.ArgumentParser(description="Launch Brainbot command service on Jetson Thor")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG, help="Command service YAML config")
    args = parser.parse_args()

    command_main(["--config", str(args.config)])


if __name__ == "__main__":
    main()

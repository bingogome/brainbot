from __future__ import annotations

import argparse
import sys
from pathlib import Path

from brainbot_command_service.run_command_service import main as command_main

DEFAULT_CONFIG = Path(__file__).with_name("pc_command.yaml")


def main() -> None:
    parser = argparse.ArgumentParser(description="Launch Brainbot command service on operator PC")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG, help="Command service YAML config")
    args = parser.parse_args()

    sys.argv = ["brainbot-command-service", "--config", str(args.config)]
    command_main()


if __name__ == "__main__":
    main()

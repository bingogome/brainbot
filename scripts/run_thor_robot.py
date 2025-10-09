from __future__ import annotations

import argparse
from pathlib import Path

import sys

from brainbot_control_service.run_robot_service import main as robot_main


DEFAULT_CONFIG = Path(__file__).with_name("thor_robot.yaml")


def main() -> None:
    parser = argparse.ArgumentParser(description="Launch Brainbot robot control agent on Thor")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG, help="Robot control YAML config")
    parser.add_argument("--no-calibrate", action="store_true", help="Skip calibration on startup")
    args = parser.parse_args()

    argv = ["brainbot-robot-service", "--config", str(args.config)]
    if args.no_calibrate:
        argv.append("--no-calibrate")

    sys.argv = argv
    robot_main()


if __name__ == "__main__":
    main()

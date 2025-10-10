from __future__ import annotations

from pathlib import Path

from brainbot_command_service.run_command_service import main as command_main
from brainbot_control_service.run_robot_service import main as robot_main

COMMAND_CONFIG = Path(__file__).with_name("thor_command.yaml")
ROBOT_CONFIG = Path(__file__).with_name("thor_robot.yaml")


def run_command() -> None:
    command_main(["--config", str(COMMAND_CONFIG)])


def run_robot(no_calibrate: bool = False) -> None:
    argv = ["--config", str(ROBOT_CONFIG)]
    if no_calibrate:
        argv.append("--no-calibrate")
    robot_main(argv)

from __future__ import annotations

import argparse
import threading
import time
from pathlib import Path

from brainbot_command_service.run_command_service import main as command_main
from brainbot_control_service.run_robot_service import main as robot_main

COMMAND_CONFIG = Path(__file__).with_name("thor_command.yaml")
ROBOT_CONFIG = Path(__file__).with_name("thor_robot.yaml")


def run_command(log_level: str | None = None, config: Path | None = None) -> None:
    config_path = config or COMMAND_CONFIG
    argv = ["--config", str(config_path)]
    if log_level:
        argv.extend(["--log-level", log_level])
    command_main(argv)


def run_robot(no_calibrate: bool = False, config: Path | None = None) -> None:
    config_path = config or ROBOT_CONFIG
    argv = ["--config", str(config_path)]
    if no_calibrate:
        argv.append("--no-calibrate")
    robot_main(argv)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Launch Brainbot command and robot services together")
    parser.add_argument("--command-config", type=Path, default=COMMAND_CONFIG, help="Path to command service config")
    parser.add_argument("--robot-config", type=Path, default=ROBOT_CONFIG, help="Path to robot service config")
    parser.add_argument("--log-level", default=None, help="Logging level for the command service")
    parser.add_argument("--no-calibrate", action="store_true", help="Skip robot calibration on startup")
    parser.add_argument("--delay", type=float, default=3.0, help="Delay (seconds) between starting command service and robot")
    mode_group = parser.add_mutually_exclusive_group()
    mode_group.add_argument("--command-only", action="store_true", help="Only run the command service")
    mode_group.add_argument("--robot-only", action="store_true", help="Only run the robot service")
    args = parser.parse_args(argv)

    command_thread: threading.Thread | None = None
    try:
        if not args.robot_only:
            command_thread = threading.Thread(
                target=run_command,
                args=(args.log_level, args.command_config),
                daemon=True,
            )
            command_thread.start()
            # Allow command service time to spin up before starting the robot.
            time.sleep(max(args.delay, 0.0))

        if not args.command_only:
            run_robot(no_calibrate=args.no_calibrate, config=args.robot_config)

        if command_thread and command_thread.is_alive():
            command_thread.join()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()

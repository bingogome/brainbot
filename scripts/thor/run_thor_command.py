from __future__ import annotations

import argparse
from pathlib import Path

from brainbot_command_service.run_command_service import main as command_main

DEFAULT_CONFIG = Path(__file__).with_name("thor_command.yaml")


def main() -> None:
    parser = argparse.ArgumentParser(description="Launch Brainbot command service on Jetson Thor")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG, help="Command service YAML config")
    parser.add_argument("--log-level", default="INFO", help="Logging level for the command service")
    parser.add_argument("--mode-dispatcher", choices=("cli", "socket"), default="cli", help="Dispatcher to use for mode commands")
    parser.add_argument("--mode-socket", type=Path, help="Unix domain socket path when using the socket dispatcher")
    parser.add_argument("--mode-socket-backlog", type=int, default=5, help="Socket dispatcher listen backlog (default: 5)")
    parser.add_argument("--mode-socket-keep", action="store_true", help="Keep existing socket file instead of removing it")
    args = parser.parse_args()

    command_argv = ["--config", str(args.config), "--log-level", args.log_level]
    mode_dispatcher = args.mode_dispatcher
    socket_path: Path | None = args.mode_socket

    if socket_path and mode_dispatcher == "cli":
        mode_dispatcher = "socket"

    if mode_dispatcher == "socket":
        if socket_path is None:
            parser.error("--mode-socket is required when the socket dispatcher is selected")
        command_argv.extend([
            "--mode-dispatcher",
            "socket",
            "--mode-socket",
            str(socket_path),
            "--mode-socket-backlog",
            str(args.mode_socket_backlog),
        ])
        if args.mode_socket_keep:
            command_argv.append("--mode-socket-keep")
    elif mode_dispatcher != "cli":
        command_argv.extend(["--mode-dispatcher", mode_dispatcher])

    command_main(command_argv)


if __name__ == "__main__":
    main()

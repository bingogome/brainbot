from __future__ import annotations

import argparse
import threading
import time
from pathlib import Path

from brainbot_command_service.run_command_service import main as command_main
from brainbot_control_service.run_robot_service import main as robot_main
from brainbot_core.config import load_server_config
from brainbot_core.transport import BaseZMQClient

COMMAND_CONFIG = Path(__file__).with_name("thor_command.yaml")
ROBOT_CONFIG = Path(__file__).with_name("thor_robot.yaml")

CommandEndpoint = tuple[str, int, str | None]


def resolve_command_endpoint(config_path: Path) -> CommandEndpoint:
    config = load_server_config(config_path)
    host = config.network.host or "127.0.0.1"
    if host in {"0.0.0.0", "*"}:
        host = "127.0.0.1"
    return host, config.network.port, config.network.api_token


def wait_for_command_service(endpoint: CommandEndpoint, timeout: float, interval: float) -> bool:
    host, port, api_token = endpoint
    poll_delay = max(interval, 0.1)
    attempt_timeout_ms = max(int(poll_delay * 1000), 1)
    deadline = time.monotonic() + max(timeout, 0.0)

    while True:
        client = BaseZMQClient(host=host, port=port, timeout_ms=attempt_timeout_ms, api_token=api_token)
        try:
            if client.ping():
                return True
        except KeyboardInterrupt:
            raise
        except Exception:
            pass
        finally:
            client.close()

        if time.monotonic() >= deadline:
            return False
        time.sleep(poll_delay)


def run_command(
    log_level: str | None = None,
    config: Path | None = None,
    extra_args: list[str] | None = None,
) -> None:
    config_path = config or COMMAND_CONFIG
    argv = ["--config", str(config_path)]
    if log_level:
        argv.extend(["--log-level", log_level])
    if extra_args:
        argv.extend(extra_args)
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
    parser.add_argument("--delay", type=float, default=10.0, help="Minimum delay (seconds) between starting command service and robot")
    parser.add_argument("--ready-timeout", type=float, default=30.0, help="Seconds to wait for the command service to become reachable")
    parser.add_argument("--ready-check-interval", type=float, default=0.5, help="Polling interval (seconds) while waiting for the command service")
    parser.add_argument("--skip-ready-check", action="store_true", help="Launch robot without waiting for the command service readiness")
    parser.add_argument("--mode-dispatcher", choices=("cli", "socket"), default="cli", help="Dispatcher to use for mode commands")
    parser.add_argument("--mode-socket", type=Path, help="Unix domain socket path for socket mode dispatcher")
    parser.add_argument("--mode-socket-backlog", type=int, default=5, help="Socket dispatcher listen backlog (default: 5)")
    parser.add_argument("--mode-socket-keep", action="store_true", help="Keep existing socket file instead of removing it")
    mode_group = parser.add_mutually_exclusive_group()
    mode_group.add_argument("--command-only", action="store_true", help="Only run the command service")
    mode_group.add_argument("--robot-only", action="store_true", help="Only run the robot service")
    args = parser.parse_args(argv)

    command_extra_args: list[str] = []
    mode_dispatcher = args.mode_dispatcher
    socket_path: Path | None = args.mode_socket

    if socket_path and mode_dispatcher == "cli":
        mode_dispatcher = "socket"

    if mode_dispatcher == "socket":
        if socket_path is None:
            parser.error("--mode-socket is required when the socket dispatcher is selected")
        command_extra_args.extend([
            "--mode-dispatcher",
            "socket",
            "--mode-socket",
            str(socket_path),
        ])
        if args.mode_socket_backlog is not None:
            command_extra_args.extend([
                "--mode-socket-backlog",
                str(args.mode_socket_backlog),
            ])
        if args.mode_socket_keep:
            command_extra_args.append("--mode-socket-keep")
        print(f"[run_all] Mode commands available via UNIX socket {socket_path}")
    elif args.mode_dispatcher != "cli":
        command_extra_args.extend(["--mode-dispatcher", args.mode_dispatcher])

    extra_args = command_extra_args if command_extra_args else None

    command_thread: threading.Thread | None = None
    try:
        if not args.robot_only:
            command_thread = threading.Thread(
                target=run_command,
                args=(args.log_level, args.command_config, extra_args),
                daemon=True,
            )
            command_thread.start()

            wait_start = time.monotonic()
            if not args.skip_ready_check:
                try:
                    endpoint = resolve_command_endpoint(args.command_config)
                except Exception as exc:
                    print(f"[run_all] Could not determine command service endpoint: {exc}")
                    endpoint = None
                else:
                    host, port, _ = endpoint
                    timeout_display = max(args.ready_timeout, 0.0)
                    print(f"[run_all] Waiting for command service at {host}:{port} (timeout {timeout_display:.1f}s)...")
                    ready = wait_for_command_service(endpoint, args.ready_timeout, args.ready_check_interval)
                    if ready:
                        print("[run_all] Command service is reachable.")
                    else:
                        print("[run_all] Command service did not become reachable before timeout; continuing.")
            elapsed = time.monotonic() - wait_start
            remaining_delay = max(args.delay - elapsed, 0.0)
            if remaining_delay > 0.0:
                time.sleep(remaining_delay)

        if not args.command_only:
            run_robot(no_calibrate=args.no_calibrate, config=args.robot_config)

        if command_thread and command_thread.is_alive():
            command_thread.join()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()

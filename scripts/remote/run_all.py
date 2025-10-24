from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
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


def add_mode_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--mode-dispatcher",
        choices=("cli", "socket"),
        default="cli",
        help="Dispatcher to use for mode commands (default: cli)",
    )
    parser.add_argument(
        "--mode-socket",
        type=Path,
        help="Unix domain socket path when using the socket dispatcher",
    )
    parser.add_argument(
        "--mode-socket-backlog",
        type=int,
        default=5,
        help="Socket dispatcher listen backlog (default: 5)",
    )
    parser.add_argument(
        "--mode-socket-keep",
        action="store_true",
        help="Keep existing socket file instead of removing it",
    )


def collect_mode_args(args: argparse.Namespace) -> list[str]:
    dispatcher = getattr(args, "mode_dispatcher", "cli")
    extras: list[str] = []
    if dispatcher and dispatcher != "cli":
        extras.extend(["--mode-dispatcher", dispatcher])
    if dispatcher == "socket":
        mode_socket = getattr(args, "mode_socket", None)
        if mode_socket is None:
            raise SystemExit("--mode-socket is required when --mode-dispatcher=socket")
        extras.extend(["--mode-socket", str(mode_socket)])
        backlog = getattr(args, "mode_socket_backlog", None)
        if backlog is not None:
            extras.extend(["--mode-socket-backlog", str(backlog)])
        if getattr(args, "mode_socket_keep", False):
            extras.append("--mode-socket-keep")
    return extras


def run_command_cli(args: argparse.Namespace) -> int:
    extras = collect_mode_args(args)
    argv = ["--config", str(args.config)]
    if args.log_level:
        argv.extend(["--log-level", args.log_level])
    argv.extend(extras)
    command_main(argv)
    return 0


def run_robot_cli(args: argparse.Namespace) -> int:
    if not args.skip_ready_check:
        try:
            endpoint = resolve_command_endpoint(args.command_config)
        except Exception as exc:
            print(f"[run_all] could not determine command service endpoint: {exc}")
            return 1
        host, port, _ = endpoint
        timeout_display = max(args.ready_timeout, 0.0)
        print(f"[run_all] waiting for command service at {host}:{port} (timeout {timeout_display:.1f}s)...")
        ready = wait_for_command_service(endpoint, args.ready_timeout, args.ready_check_interval)
        if not ready:
            print("[run_all] command service did not become reachable; aborting")
            return 1

    argv = ["--config", str(args.config)]
    if args.no_calibrate:
        argv.append("--no-calibrate")
    robot_main(argv)
    return 0


def build_command_process_argv(args: argparse.Namespace, extras: list[str]) -> list[str]:
    argv = [
        sys.executable,
        "-m",
        "brainbot_command_service.run_command_service",
        "--config",
        str(args.command_config),
    ]
    if args.log_level:
        argv.extend(["--log-level", args.log_level])
    argv.extend(extras)
    return argv


def build_robot_process_argv(args: argparse.Namespace) -> list[str]:
    argv = [
        sys.executable,
        "-m",
        "brainbot_control_service.run_robot_service",
        "--config",
        str(args.robot_config),
    ]
    if args.no_calibrate:
        argv.append("--no-calibrate")
    return argv


def launch_process(name: str, argv: list[str], cwd: Path) -> subprocess.Popen:
    print(f"[run_all] launching {name}: {' '.join(argv)}")
    return subprocess.Popen(argv, cwd=str(cwd), env=os.environ.copy())


def monitor_processes(processes: dict[str, subprocess.Popen | None]) -> int:
    try:
        while True:
            for name, proc in processes.items():
                if proc is None:
                    continue
                code = proc.poll()
                if code is not None:
                    print(f"[run_all] process '{name}' exited with code {code}")
                    return code
            time.sleep(1.0)
    except KeyboardInterrupt:
        print("[run_all] keyboard interrupt received; terminating child processes")
        return -signal.SIGINT


def terminate_processes(processes: dict[str, subprocess.Popen | None]) -> None:
    for proc in processes.values():
        if proc and proc.poll() is None:
            try:
                proc.send_signal(signal.SIGINT)
            except Exception:
                pass
    time.sleep(1.0)
    for proc in processes.values():
        if proc and proc.poll() is None:
            proc.terminate()
    time.sleep(1.0)
    for proc in processes.values():
        if proc and proc.poll() is None:
            proc.kill()


def run_both_cli(args: argparse.Namespace) -> int:
    extras = collect_mode_args(args)
    repo_root = Path(__file__).resolve().parents[2]
    processes: dict[str, subprocess.Popen | None] = {"command": None, "robot": None}

    try:
        command_argv = build_command_process_argv(args, extras)
        processes["command"] = launch_process("command", command_argv, repo_root)

        endpoint = None
        wait_start = time.monotonic()
        if not args.skip_ready_check:
            try:
                endpoint = resolve_command_endpoint(args.command_config)
            except Exception as exc:
                print(f"[run_all] could not determine command service endpoint: {exc}")
                return 1
            host, port, _ = endpoint
            timeout_display = max(args.ready_timeout, 0.0)
            print(f"[run_all] waiting for command service at {host}:{port} (timeout {timeout_display:.1f}s)...")
            ready = wait_for_command_service(endpoint, args.ready_timeout, args.ready_check_interval)
            if not ready:
                print("[run_all] command service did not become reachable; aborting robot launch")
                return 1

        elapsed = time.monotonic() - wait_start
        remaining_delay = max(args.delay - elapsed, 0.0)
        if remaining_delay > 0.0:
            time.sleep(remaining_delay)

        robot_argv = build_robot_process_argv(args)
        processes["robot"] = launch_process("robot", robot_argv, repo_root)

        exit_code = monitor_processes(processes)
    finally:
        terminate_processes(processes)

    return exit_code


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Launch Brainbot command and robot services")
    subparsers = parser.add_subparsers(dest="role", required=True)

    command_parser = subparsers.add_parser("command", help="Run only the command service")
    command_parser.add_argument("--config", type=Path, default=COMMAND_CONFIG, help="Path to command service config")
    command_parser.add_argument("--log-level", default=None, help="Logging level for the command service")
    add_mode_options(command_parser)
    command_parser.set_defaults(func=run_command_cli)

    robot_parser = subparsers.add_parser("robot", help="Run only the robot control service")
    robot_parser.add_argument("--config", type=Path, default=ROBOT_CONFIG, help="Path to robot service config")
    robot_parser.add_argument("--no-calibrate", action="store_true", help="Skip robot calibration on startup")
    robot_parser.add_argument("--command-config", type=Path, default=COMMAND_CONFIG, help="Command config used for readiness checks")
    robot_parser.add_argument("--ready-timeout", type=float, default=30.0, help="Seconds to wait for the command service")
    robot_parser.add_argument("--ready-check-interval", type=float, default=0.5, help="Polling interval (seconds)")
    robot_parser.add_argument("--skip-ready-check", action="store_true", help="Launch without waiting for command readiness")
    robot_parser.set_defaults(func=run_robot_cli)

    both_parser = subparsers.add_parser("both", help="Run command and robot services together")
    both_parser.add_argument("--command-config", type=Path, default=COMMAND_CONFIG, help="Path to command service config")
    both_parser.add_argument("--robot-config", type=Path, default=ROBOT_CONFIG, help="Path to robot service config")
    both_parser.add_argument("--log-level", default=None, help="Logging level for the command service")
    both_parser.add_argument("--no-calibrate", action="store_true", help="Skip robot calibration on startup")
    both_parser.add_argument("--delay", type=float, default=10.0, help="Delay (seconds) before launching the robot service")
    both_parser.add_argument("--ready-timeout", type=float, default=30.0, help="Seconds to wait for the command service")
    both_parser.add_argument("--ready-check-interval", type=float, default=0.5, help="Polling interval (seconds)")
    both_parser.add_argument("--skip-ready-check", action="store_true", help="Launch without waiting for command readiness")
    add_mode_options(both_parser)
    both_parser.set_defaults(func=run_both_cli)

    args = parser.parse_args(argv)
    exit_code = args.func(args)
    if exit_code is None:
        exit_code = 0
    if exit_code not in (0, -signal.SIGINT):
        raise SystemExit(exit_code)


if __name__ == "__main__":
    main()

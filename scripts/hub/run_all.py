from __future__ import annotations

import argparse
import signal
import sys
from pathlib import Path
from typing import Any, Sequence

import yaml

from brainbot_service_manager import ServiceManager, ServiceSpec


def _infer_ready_endpoint(command: Sequence[str], base_dir: Path) -> tuple[str | None, int | None]:
    """Attempt to infer host/port from a --config argument in the command."""
    for index, part in enumerate(command):
        if part != "--config":
            continue
        if index + 1 >= len(command):
            break
        config_path = Path(command[index + 1])
        if not config_path.is_absolute():
            if config_path.exists():
                config_path = config_path.resolve()
            else:
                config_path = (base_dir / config_path).resolve()
        if not config_path.exists():
            raise FileNotFoundError(f"Config file referenced by service command not found: {config_path}")
        with config_path.open("r", encoding="utf-8") as handle:
            config_data = yaml.safe_load(handle) or {}
        network = config_data.get("network", {})
        if isinstance(network, dict):
            port = network.get("port")
            if port is not None:
                host = network.get("host", "127.0.0.1")
                return str(host), int(port)
        break
    return None, None


def _normalize_command(raw: Any) -> Sequence[str]:
    if isinstance(raw, (list, tuple)):
        return [str(part) for part in raw]
    if isinstance(raw, str):
        # Allow string with shell-style splitting.
        import shlex

        return shlex.split(raw)
    raise TypeError(f"Unsupported command type: {type(raw)}")


def _load_config(path: Path) -> tuple[dict[str, Any], dict[str, ServiceSpec]]:
    with path.open("r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle) or {}
    manager_cfg = data.get("manager", {})
    services_cfg = data.get("services", {})
    if not isinstance(services_cfg, dict) or not services_cfg:
        raise ValueError("Manager config must define at least one service under 'services'")

    services: dict[str, ServiceSpec] = {}
    base_dir = path.parent.resolve()
    for name, raw_spec in services_cfg.items():
        if not isinstance(raw_spec, dict):
            raise ValueError(f"Service '{name}' configuration must be a mapping")
        command = _normalize_command(raw_spec.get("command"))
        command = list(command)
        for idx, part in enumerate(command):
            if part == "{python}":
                continue
            candidate = Path(part)
            if candidate.is_absolute():
                continue
            if candidate.exists():
                command[idx] = str(candidate.resolve())
                continue
            resolved_candidate = (base_dir / candidate).resolve()
            if resolved_candidate.exists():
                command[idx] = str(resolved_candidate)
        cwd = raw_spec.get("cwd")
        env = raw_spec.get("env")
        ready_host = raw_spec.get("ready_host")
        ready_port = raw_spec.get("ready_port")
        if ready_port is None:
            inferred_host, inferred_port = _infer_ready_endpoint(command, base_dir)
            if inferred_port is not None:
                ready_port = inferred_port
                if ready_host is None:
                    ready_host = inferred_host or "127.0.0.1"
        if ready_port is None:
            raise ValueError(
                f"Service '{name}' must specify 'ready_port' or provide a --config file with a network.port"
            )
        ready_host = str(ready_host or "127.0.0.1")
        start_timeout = float(raw_spec.get("start_timeout_s", 10.0))
        stop_grace = float(raw_spec.get("stop_grace_s", 5.0))
        services[name] = ServiceSpec(
            name=name,
            command=command,
            cwd=str(cwd) if cwd else None,
            env={str(k): str(v) for k, v in env.items()} if isinstance(env, dict) else None,
            ready_host=ready_host,
            ready_port=int(ready_port) if ready_port is not None else None,
            start_timeout_s=start_timeout,
            stop_grace_s=stop_grace,
        )
    return manager_cfg, services


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Manage Brainbot teleop/data servers on demand")
    parser.add_argument(
        "--config",
        type=Path,
        default=Path(__file__).with_name("hub_manager.yaml"),
        help="Path to manager YAML configuration",
    )
    args = parser.parse_args(argv)

    manager_cfg, services = _load_config(args.config.resolve())
    host = str(manager_cfg.get("host", "0.0.0.0"))
    port = int(manager_cfg.get("port", 7100))
    api_token = manager_cfg.get("api_token")

    server = ServiceManager(host=host, port=port, api_token=api_token, services=services)

    def shutdown_signal(signum, frame):
        print(f"[hub-manager] received signal {signal.Signals(signum).name}, shutting down")
        server.close()
        sys.exit(0)

    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, shutdown_signal)

    print(f"[hub-manager] listening on tcp://{host}:{port} with {len(services)} service(s)")
    try:
        server.run()
    except KeyboardInterrupt:
        pass
    finally:
        server.close()


if __name__ == "__main__":
    main()

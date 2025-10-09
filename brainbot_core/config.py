from __future__ import annotations

import io
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

import draccus
import yaml

from lerobot.robots.config import RobotConfig
from lerobot.teleoperators.config import TeleoperatorConfig


@dataclass(slots=True)
class NetworkConfig:
    host: str = "127.0.0.1"
    port: int = 5555
    timeout_ms: int = 1500
    api_token: str | None = None


@dataclass(slots=True)
class EdgeControlConfig:
    robot: RobotConfig
    network: NetworkConfig = field(default_factory=NetworkConfig)
    loop_hz: float = 30.0
    max_missed_actions: int = 3
    fallback_action: dict[str, float] | None = None
    calibrate_on_start: bool = True
    metadata: Mapping[str, Any] | None = None


@dataclass(slots=True)
class AIClientConfig:
    host: str = "localhost"
    port: int = 5555
    timeout_ms: int = 1500
    api_token: str | None = None
    instruction_key: str = "language_instruction"


@dataclass(slots=True)
class ServerRuntimeConfig:
    teleops: dict[str, TeleoperatorConfig] = field(default_factory=dict)
    default_mode: str | None = None
    ai: AIClientConfig | None = None
    network: NetworkConfig = field(default_factory=NetworkConfig)
    metadata: Mapping[str, Any] | None = None


def load_edge_config(path: Path) -> EdgeControlConfig:
    raw = _load_yaml(path)
    robot_cfg = _load_draccus_config(raw.pop("robot"), RobotConfig)
    network_cfg = NetworkConfig(**raw.pop("network", {}))
    return EdgeControlConfig(
        robot=robot_cfg,
        network=network_cfg,
        loop_hz=float(raw.pop("loop_hz", 30.0)),
        max_missed_actions=int(raw.pop("max_missed_actions", 3)),
        fallback_action=raw.pop("fallback_action", None),
        calibrate_on_start=bool(raw.pop("calibrate_on_start", True)),
        metadata=raw.pop("metadata", None),
    )


def load_server_config(path: Path) -> ServerRuntimeConfig:
    raw = _load_yaml(path)

    teleops_data = raw.pop("teleops", None)
    teleop_single = raw.pop("teleop", None)

    teleop_cfgs: dict[str, TeleoperatorConfig] = {}
    if teleops_data:
        for name, cfg in teleops_data.items():
            teleop_cfgs[name] = _load_draccus_config(cfg, TeleoperatorConfig)
    elif teleop_single is not None:
        teleop_cfgs["default"] = _load_draccus_config(teleop_single, TeleoperatorConfig)

    default_mode = raw.pop("default_mode", None)
    if default_mode is None and teleop_cfgs:
        default_mode = next(iter(teleop_cfgs.keys()))

    ai_data = raw.pop("ai", None)
    ai_cfg = AIClientConfig(**ai_data) if ai_data else None

    network_cfg = NetworkConfig(**raw.pop("network", {}))

    return ServerRuntimeConfig(
        teleops=teleop_cfgs,
        default_mode=default_mode,
        ai=ai_cfg,
        network=network_cfg,
        metadata=raw.pop("metadata", None),
    )


def _load_yaml(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        return yaml.safe_load(handle) or {}


def _load_draccus_config(data: Mapping[str, Any], target_cls: type) -> Any:
    buffer = io.StringIO()
    yaml.safe_dump(dict(data), buffer)
    buffer.seek(0)
    with draccus.config_type("yaml"):
        return draccus.load(target_cls, buffer)

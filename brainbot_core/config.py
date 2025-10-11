from __future__ import annotations

import io
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Mapping

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
    observation_adapter: str = "numeric_only"
    camera_stream: "CameraStreamConfig | None" = None
    metadata: Mapping[str, Any] | None = None


@dataclass(slots=True)
class AIClientConfig:
    host: str = "127.0.0.1"
    port: int = 6000
    timeout_ms: int = 1500
    api_token: str | None = None
    instruction_key: str = "language_instruction"


@dataclass(slots=True)
class RemoteTeleopConfig:
    host: str
    port: int
    timeout_ms: int = 1500
    api_token: str | None = None


@dataclass(slots=True)
class TeleopEndpointConfig:
    mode: Literal["remote", "local"]
    remote: RemoteTeleopConfig | None = None
    local: TeleoperatorConfig | None = None


@dataclass(slots=True)
class CameraStreamSourceConfig:
    name: str
    path: str
    fps: float | None = None
    quality: int | None = None


@dataclass(slots=True)
class CameraStreamConfig:
    host: str = "0.0.0.0"
    port: int = 7005
    quality: int = 70
    sources: list[CameraStreamSourceConfig] = field(default_factory=list)


@dataclass(slots=True)
class WebVizConfig:
    host: str = "0.0.0.0"
    port: int = 8080


@dataclass(slots=True)
class ServerRuntimeConfig:
    teleops: dict[str, TeleopEndpointConfig] = field(default_factory=dict)
    default_mode: str | None = None
    ai: AIClientConfig | None = None
    network: NetworkConfig = field(default_factory=NetworkConfig)
    webviz: WebVizConfig | None = None
    camera_stream: CameraStreamConfig | None = None
    metadata: Mapping[str, Any] | None = None


def load_edge_config(path: Path) -> EdgeControlConfig:
    raw = _load_yaml(path)
    robot_cfg = _load_draccus_config(raw.pop("robot"), RobotConfig)
    network_cfg = NetworkConfig(**raw.pop("network", {}))
    observation_adapter = str(raw.pop("observation_adapter", "numeric_only")).lower()
    camera_stream_raw = raw.pop("camera_stream", None)
    camera_stream_cfg = _load_camera_stream_config(camera_stream_raw) if camera_stream_raw else None
    if camera_stream_cfg is None:
        camera_stream_cfg = _infer_camera_stream_config(robot_cfg)
    return EdgeControlConfig(
        robot=robot_cfg,
        network=network_cfg,
        loop_hz=float(raw.pop("loop_hz", 30.0)),
        max_missed_actions=int(raw.pop("max_missed_actions", 3)),
        fallback_action=raw.pop("fallback_action", None),
        calibrate_on_start=bool(raw.pop("calibrate_on_start", True)),
        observation_adapter=observation_adapter,
        camera_stream=camera_stream_cfg,
        metadata=raw.pop("metadata", None),
    )


def load_server_config(path: Path) -> ServerRuntimeConfig:
    raw = _load_yaml(path)

    teleops_data = raw.pop("teleops", None)
    teleop_single = raw.pop("teleop", None)

    teleop_cfgs: dict[str, TeleopEndpointConfig] = {}
    if teleops_data:
        for name, cfg in teleops_data.items():
            teleop_cfgs[name] = _make_teleop_endpoint(name, cfg)
    elif teleop_single is not None:
        teleop_cfgs["default"] = _make_teleop_endpoint("default", teleop_single)

    default_mode = raw.pop("default_mode", None)
    if default_mode is None and teleop_cfgs:
        default_mode = next(iter(teleop_cfgs.keys()))

    ai_data = raw.pop("ai", None)
    ai_cfg = AIClientConfig(**ai_data) if ai_data else AIClientConfig()

    network_cfg = NetworkConfig(**raw.pop("network", {}))
    webviz_data = raw.pop("webviz", None)
    webviz_cfg = WebVizConfig(**webviz_data) if webviz_data else None
    camera_stream_data = raw.pop("camera_stream", None)
    camera_stream_cfg = _load_camera_stream_config(camera_stream_data) if camera_stream_data else None

    return ServerRuntimeConfig(
        teleops=teleop_cfgs,
        default_mode=default_mode,
        ai=ai_cfg,
        network=network_cfg,
        webviz=webviz_cfg,
        camera_stream=camera_stream_cfg,
        metadata=raw.pop("metadata", None),
    )


def _load_yaml(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        return yaml.safe_load(handle) or {}


def _load_camera_stream_config(data: Mapping[str, Any]) -> CameraStreamConfig:
    host = str(data.get("host", "0.0.0.0"))
    port = int(data.get("port", 7005))
    quality = int(data.get("quality", 70))
    sources: list[CameraStreamSourceConfig] = []
    for entry in data.get("sources", []):
        name = str(entry["name"])
        path = str(entry.get("path", name))
        fps = entry.get("fps")
        fps_val = float(fps) if fps is not None else None
        src_quality = entry.get("quality")
        src_quality_val = int(src_quality) if src_quality is not None else None
        sources.append(
            CameraStreamSourceConfig(
                name=name,
                path=path,
                fps=fps_val,
                quality=src_quality_val,
            )
        )
    return CameraStreamConfig(host=host, port=port, quality=quality, sources=sources)


def _infer_camera_stream_config(robot_cfg: RobotConfig) -> CameraStreamConfig | None:
    cameras = getattr(robot_cfg, "cameras", None)
    if not cameras:
        return None
    sources: list[CameraStreamSourceConfig] = []
    for name in cameras.keys():
        sources.append(
            CameraStreamSourceConfig(
                name=str(name),
                path=f"robot.cameras.{name}",
            )
        )
    if not sources:
        return None
    return CameraStreamConfig(sources=sources)


import importlib


def _load_draccus_config(data: Mapping[str, Any], target_cls: type) -> Any:
    if isinstance(data, Mapping):
        choice = data.get("type")
        if choice:
            module_name = None
            if target_cls is RobotConfig:
                module_name = f"lerobot.robots.{choice}"
            elif target_cls is TeleoperatorConfig:
                module_name = f"lerobot.teleoperators.{choice}"
            if module_name:
                try:
                    importlib.import_module(module_name)
                except ModuleNotFoundError:
                    pass
        if target_cls is RobotConfig and "cameras" in data:
            for cam_cfg in data["cameras"].values():
                cam_type = cam_cfg.get("type") if isinstance(cam_cfg, Mapping) else None
                if cam_type:
                    try:
                        importlib.import_module(f"lerobot.cameras.{cam_type}")
                    except ModuleNotFoundError:
                        pass
    buffer = io.StringIO()
    yaml.safe_dump(dict(data), buffer)
    buffer.seek(0)
    with draccus.config_type("yaml"):
        return draccus.load(target_cls, buffer)


def _make_teleop_endpoint(name: str, cfg: Mapping[str, Any]) -> TeleopEndpointConfig:
    mode = cfg.get("mode")
    if mode == "remote":
        if "host" not in cfg or "port" not in cfg:
            raise ValueError(f"Remote teleop '{name}' requires 'host' and 'port'")
        return TeleopEndpointConfig(
            mode="remote",
            remote=RemoteTeleopConfig(
                host=str(cfg["host"]),
                port=int(cfg["port"]),
                timeout_ms=int(cfg.get("timeout_ms", 1500)),
                api_token=cfg.get("api_token"),
            ),
        )
    if mode == "local":
        local_cfg = cfg.get("config", cfg)
        return TeleopEndpointConfig(mode="local", local=_load_draccus_config(local_cfg, TeleoperatorConfig))
    raise ValueError(f"Teleop '{name}' must declare mode 'remote' or 'local'")

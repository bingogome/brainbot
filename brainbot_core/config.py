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
    port: int = 6000
    timeout_ms: int = 1500
    api_token: str | None = None


@dataclass(slots=True)
class ObservationPreprocessConfig:
    target_height: int = 224
    target_width: int = 224
    interpolation: str = "linear"




@dataclass(slots=True)
class ActionFilterConfig:
    type: str = "median"
    window_size: int = 3
    blend_alpha: float = 0.3

@dataclass(slots=True)
class EdgeControlConfig:
    robot: RobotConfig
    network: NetworkConfig = field(default_factory=NetworkConfig)
    loop_hz: float = 15.0
    max_missed_actions: int = 3
    fallback_action: dict[str, float] | None = None
    calibrate_on_start: bool = True
    observation_adapter: str = "numeric_only"
    camera_stream: "CameraStreamConfig | None" = None
    metadata: Mapping[str, Any] | None = None
    observation_preprocess: ObservationPreprocessConfig | None = None
    action_filter: ActionFilterConfig | None = None


@dataclass(slots=True)
class AIClientConfig:
    host: str = "127.0.0.1"
    port: int = 5555
    timeout_ms: int = 5000
    api_token: str | None = None
    instruction_key: str = "language_instruction"
    modality_config_path: str | None = None
    camera_keys: list[str] | None = None
    state_keys: list[str] | None = None
    action_horizon: int = 90


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
class DatasetRecordConfig:
    repo_id: str
    single_task: str
    root: str | Path | None = None
    fps: int = 30
    episode_time_s: float = 60.0
    reset_time_s: float = 60.0
    num_episodes: int = 50
    video: bool = True
    push_to_hub: bool = True
    private: bool = False
    tags: list[str] | None = None
    num_image_writer_processes: int = 0
    num_image_writer_threads_per_camera: int = 4
    video_encoding_batch_size: int = 1
    rename_map: dict[str, str] = field(default_factory=dict)


@dataclass(slots=True)
class DataModeConfig:
    robot: RobotConfig
    dataset: DatasetRecordConfig
    teleop: "TeleopEndpointConfig | None" = None
    display_data: bool = False
    resume: bool = False
    play_sounds: bool = False


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
    data: DataModeConfig | None = None


def load_edge_config(path: Path) -> EdgeControlConfig:
    raw = _load_yaml(path)
    base_dir = path.parent
    robot_raw = raw.pop("robot", None)
    robot_path = raw.pop("robot_config_path", None)
    if robot_path:
        robot_external = _load_robot_config_from_path(robot_path, base_dir)
        if robot_raw:
            merged = dict(robot_external)
            merged.update(robot_raw)
            robot_raw = merged
        else:
            robot_raw = robot_external
    if robot_raw is None:
        raise ValueError("Edge config requires 'robot' or 'robot_config_path'")
    robot_cfg = _load_draccus_config(robot_raw, RobotConfig)
    network_cfg = NetworkConfig(**raw.pop("network", {}))
    observation_adapter = str(raw.pop("observation_adapter", "numeric_only")).lower()
    camera_stream_raw = raw.pop("camera_stream", None)
    camera_stream_cfg = _load_camera_stream_config(camera_stream_raw) if camera_stream_raw else None
    if camera_stream_cfg is None:
        camera_stream_cfg = _infer_camera_stream_config(robot_cfg)
    preprocess_raw = raw.pop("observation_preprocess", None)
    preprocess_cfg = ObservationPreprocessConfig(**preprocess_raw) if preprocess_raw else None
    action_filter_raw = raw.pop("action_filter", None)
    action_filter_cfg = ActionFilterConfig(**action_filter_raw) if action_filter_raw else None
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
        observation_preprocess=preprocess_cfg,
        action_filter=action_filter_cfg,
    )


def load_server_config(path: Path) -> ServerRuntimeConfig:
    raw = _load_yaml(path)
    base_dir = path.parent

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
    data_raw = raw.pop("data", None)
    data_cfg = _load_data_mode_config(data_raw, base_dir=base_dir) if data_raw else None

    return ServerRuntimeConfig(
        teleops=teleop_cfgs,
        default_mode=default_mode,
        ai=ai_cfg,
        network=network_cfg,
        webviz=webviz_cfg,
        camera_stream=camera_stream_cfg,
        metadata=raw.pop("metadata", None),
        data=data_cfg,
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


def _load_data_mode_config(data: Mapping[str, Any] | str | Path, base_dir: Path | None = None) -> DataModeConfig:
    if isinstance(data, (str, Path)):
        external_path = _resolve_config_path(data, base_dir)
        external_data = _load_yaml(external_path)
        return _load_data_mode_config(external_data.get("data", external_data), base_dir=external_path.parent)
    if not isinstance(data, Mapping):
        raise ValueError("Data mode configuration must be a mapping")

    local = dict(data)
    config_path = local.pop("config_path", None)
    if config_path:
        external_path = _resolve_config_path(config_path, base_dir)
        external_data = _load_yaml(external_path)
        merged = dict(external_data.get("data", external_data))
        merged.update(local)
        local = merged
        base_dir = external_path.parent

    robot_raw = local.pop("robot", None)
    robot_path = local.pop("robot_config_path", None)
    if robot_path:
        robot_external = _load_robot_config_from_path(robot_path, base_dir)
        if robot_raw:
            merged_robot = dict(robot_external)
            merged_robot.update(robot_raw)
            robot_raw = merged_robot
        else:
            robot_raw = robot_external
    if robot_raw is None:
        raise ValueError("Data mode requires a 'robot' configuration block (directly or via 'robot_config_path')")

    dataset_raw = local.pop("dataset", None)
    if dataset_raw is None:
        raise ValueError("Data mode requires a 'dataset' configuration block")

    teleop_raw = local.pop("teleop", None)
    teleop_cfg: TeleopEndpointConfig | None = None
    if teleop_raw:
        if isinstance(teleop_raw, Mapping) and teleop_raw.get("mode"):
            teleop_cfg = _make_teleop_endpoint("data", teleop_raw)
        else:
            local_cfg = _load_draccus_config(teleop_raw, TeleoperatorConfig)
            teleop_cfg = TeleopEndpointConfig(mode="local", local=local_cfg)

    robot_cfg = _load_draccus_config(robot_raw, RobotConfig)
    dataset_cfg = DatasetRecordConfig(**dataset_raw)

    return DataModeConfig(
        robot=robot_cfg,
        dataset=dataset_cfg,
        teleop=teleop_cfg,
        display_data=bool(local.get("display_data", False)),
        resume=bool(local.get("resume", False)),
        play_sounds=bool(local.get("play_sounds", False)),
    )


def _resolve_config_path(value: str | Path, base_dir: Path | None) -> Path:
    path_obj = Path(value)
    if base_dir and not path_obj.is_absolute():
        path_obj = base_dir / path_obj
    return path_obj


def _load_robot_config_from_path(value: str | Path, base_dir: Path | None) -> Mapping[str, Any]:
    path_obj = _resolve_config_path(value, base_dir)
    data = _load_yaml(path_obj)
    robot_data = data.get("robot", data)
    if not isinstance(robot_data, Mapping):
        raise ValueError(f"Robot config at '{path_obj}' must contain a mapping under 'robot'")
    return dict(robot_data)


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

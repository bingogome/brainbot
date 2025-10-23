from __future__ import annotations

import argparse
import logging
from pathlib import Path
from typing import Any
from collections.abc import Mapping, Sequence

import numpy as np
import zmq

try:
    from PIL import Image
except Exception:  # PIL may be optional
    Image = None  # type: ignore[assignment]

try:
    from lerobot.processor import make_default_processors
except ImportError:  # compatibility with newer LeRobot releases
    from lerobot.processor.factory import make_default_processors  # type: ignore
from lerobot.teleoperators.utils import make_teleoperator_from_config

from brainbot_core.config import AIClientConfig, ServerRuntimeConfig, WebVizConfig, load_server_config
from brainbot_core.transport import ActionInferenceClient
from brainbot_core.proto import ObservationMessage
from brainbot_mode_dispatcher import CLIModeDispatcher, SocketModeDispatcher

from brainbot_webviz import VisualizationServer

from . import (
    AICommandProvider,
    CommandProvider,
    CommandService,
    DataCollectionCommandProvider,
    IdleCommandProvider,
    LocalTeleopCommandProvider,
    ModeManager,
    RemoteTeleopCommandProvider,
)
from .gr00t_modality import Gr00TObservationMapper


logger = logging.getLogger(__name__)


def _make_basic_ai_observation_adapter():
    def _normalize(value: Any) -> Any:
        if isinstance(value, np.ndarray) or np.isscalar(value):
            return value
        if isinstance(value, Mapping):
            return {str(k): _normalize(v) for k, v in value.items()}
        if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
            return [_normalize(v) for v in value]
        if Image is not None and isinstance(value, Image.Image):
            return np.asarray(value)
        return value

    def _strip_images(value: Any) -> Any:
        if isinstance(value, np.ndarray) or np.isscalar(value):
            return value
        if Image is not None and isinstance(value, Image.Image):
            return np.asarray(value)
        if isinstance(value, Mapping):
            return {str(k): _strip_images(v) for k, v in value.items()}
        if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
            return [_strip_images(v) for v in value]
        return value

    def _ensure_no_pil(value: Any, path: str = "root") -> None:
        if Image is not None and isinstance(value, Image.Image):
            raise TypeError(f"PIL image detected at {path}")
        if isinstance(value, Mapping):
            for key, val in value.items():
                _ensure_no_pil(val, f"{path}.{key}")
        elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
            for idx, val in enumerate(value):
                _ensure_no_pil(val, f"{path}[{idx}]")

    def adapter(observation: ObservationMessage) -> dict[str, Any]:
        payload = _normalize(observation.payload)
        result: dict[str, Any] = {}

        base = payload.get("base")
        if base is not None:
            result["base"] = base

        robot = payload.get("robot")
        cameras: dict[str, np.ndarray] = {}

        def _coerce_frame(value: Any) -> np.ndarray | None:
            try:
                array = np.asarray(value)
            except Exception:
                return None
            if array.ndim == 0:
                return None
            if array.ndim == 2:
                array = array[:, :, None]
            if array.ndim == 3:
                array = array[None, ...]
            if array.ndim not in (4, 5):
                return None
            if array.dtype != np.uint8:
                if np.issubdtype(array.dtype, np.floating):
                    scaled = array if array.max() > 1.0 else array * 255.0
                    array = np.clip(scaled, 0, 255).astype(np.uint8)
                else:
                    array = np.clip(array, 0, 255).astype(np.uint8)
            return array

        if isinstance(robot, dict):
            robot_data = dict(robot)
            cam_group = robot_data.pop("cameras", None)
            if isinstance(cam_group, dict):
                for key, value in cam_group.items():
                    array = _coerce_frame(value)
                    if array is not None:
                        cameras[key] = array
            for key in list(robot_data.keys()):
                array = _coerce_frame(robot_data[key])
                if array is not None:
                    cameras[key] = array
                    robot_data.pop(key)
            result["robot"] = robot_data
        elif robot is not None:
            result["robot"] = robot

        # Promote any remaining top-level keys (e.g., metadata) unchanged
        for key, value in payload.items():
            if key not in {"robot", "base"}:
                result[key] = value

        for name, array in cameras.items():
            result[f"video.{name}"] = array

        sanitized = _strip_images(_normalize(result))
        _ensure_no_pil(sanitized)
        return sanitized

    return adapter


def _build_ai_observation_adapter(ai_cfg: AIClientConfig):
    if ai_cfg.modality_config_path and ai_cfg.state_keys:
        mapper = Gr00TObservationMapper(
            Path(ai_cfg.modality_config_path),
            ai_cfg.state_keys,
            ai_cfg.camera_keys,
        )
        logger.info(
            "GR00T modality adapter enabled (config=%s)", ai_cfg.modality_config_path
        )

        def adapter(observation: ObservationMessage) -> dict[str, Any]:
            mapped = mapper.build(observation.payload)
            for key, value in list(mapped.items()):
                if isinstance(value, np.ndarray):
                    mapped[key] = value[np.newaxis, ...]
                else:
                    mapped[key] = [value]
            return mapped

        return adapter
    if ai_cfg.modality_config_path and not ai_cfg.state_keys:
        logger.warning(
            "modality_config_path supplied but state_keys missing; falling back to basic adapter"
        )

    return _make_basic_ai_observation_adapter()


def _build_gr00t_action_adapter(ai_cfg: AIClientConfig):
    state_keys = list(ai_cfg.state_keys or [])
    if not state_keys:
        return None

    left_arm_keys = [key for key in state_keys if key.startswith("left_") and "gripper" not in key]
    right_arm_keys = [key for key in state_keys if key.startswith("right_") and "gripper" not in key]
    left_gripper_key = next((key for key in state_keys if key.startswith("left_gripper")), None)
    right_gripper_key = next((key for key in state_keys if key.startswith("right_gripper")), None)

    handled_keys = {"action.left_arm", "action.left_gripper", "action.right_arm", "action.right_gripper"}
    warned_multi: set[str] = set()

    def _assign_targets(values: dict[str, Any], action_key: str, targets: list[str], idx: int, output: dict[str, float]) -> None:
        if not targets:
            return
        array = values.get(action_key)
        if array is None:
            return
        data = np.asarray(array)
        if data.size == 0:
            return
        if data.ndim == 1:
            if idx > 0:
                return
            row = data
        else:
            if idx >= data.shape[0]:
                return
            row = data[idx]
        row = np.asarray(row).reshape(-1)
        if len(row) < len(targets):
            logger.warning(
                "GR00T action '%s' size %s does not match expected keys %s",
                action_key,
                row.shape,
                targets,
            )
        for name, value in zip(targets, row):
            output[name] = float(value)

    def _extract_scalar(value: Any, key: str, idx: int) -> float | None:
        data = np.asarray(value)
        if data.size == 0:
            return None
        if data.ndim == 0:
            return float(data)
        if data.ndim == 1:
            if data.shape[0] == 1:
                return float(data[0])
            if idx >= data.shape[0]:
                return None
            return float(data[idx])
        if idx >= data.shape[0]:
            return None
        row = np.asarray(data[idx]).reshape(-1)
        if row.size == 0:
            return None
        if row.size > 1 and key not in warned_multi:
            logger.warning("Action '%s' has %s values; keeping first entry", key, row.size)
            warned_multi.add(key)
        return float(row[0])

    def _infer_chunk_length(values: dict[str, Any]) -> int:
        candidate_keys = ["action.left_arm", "action.right_arm", "action.left_gripper", "action.right_gripper"]
        for key in candidate_keys:
            array = values.get(key)
            if array is None:
                continue
            data = np.asarray(array)
            if data.ndim >= 2 and data.shape[0] > 1:
                return data.shape[0]
        for value in values.values():
            data = np.asarray(value)
            if data.ndim >= 2 and data.shape[0] > 1:
                return data.shape[0]
        return 1

    def adapter(values: dict[str, Any], horizon: int) -> list[dict[str, float]]:
        chunk_len = _infer_chunk_length(values)
        limit = min(max(1, horizon), max(1, chunk_len))
        steps: list[dict[str, float]] = []
        for idx in range(limit):
            step: dict[str, float] = {}
            _assign_targets(values, "action.left_arm", left_arm_keys, idx, step)
            if left_gripper_key:
                _assign_targets(values, "action.left_gripper", [left_gripper_key], idx, step)
            _assign_targets(values, "action.right_arm", right_arm_keys, idx, step)
            if right_gripper_key:
                _assign_targets(values, "action.right_gripper", [right_gripper_key], idx, step)

            for key, value in values.items():
                if not key.startswith("action."):
                    continue
                if key in handled_keys:
                    continue
                scalar = _extract_scalar(value, key, idx)
                if scalar is None:
                    continue
                step[key] = scalar

            steps.append(step)
        return steps

    return adapter


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument(
        "--log-level",
        default="INFO",
        help="Logging level (e.g. DEBUG, INFO, WARNING)",
    )
    parser.add_argument(
        "--mode-dispatcher",
        choices=("cli", "socket"),
        default="cli",
        help="Input dispatcher for mode commands (default: cli)",
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
        help="Listen backlog size for the socket dispatcher (default: 5)",
    )
    parser.add_argument(
        "--mode-socket-keep",
        action="store_true",
        help="Keep an existing socket file instead of removing it on startup",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    config: ServerRuntimeConfig = load_server_config(args.config)
    providers: dict[str, CommandProvider] = {}
    teleop_aliases: dict[str, str] = {}
    for name, endpoint in config.teleops.items():
        key = f"teleop:{name}"
        if endpoint.mode == "remote" and endpoint.remote is not None:
            providers[key] = RemoteTeleopCommandProvider(
                host=endpoint.remote.host,
                port=endpoint.remote.port,
                timeout_ms=endpoint.remote.timeout_ms,
                api_token=endpoint.remote.api_token,
            )
        elif endpoint.mode == "local" and endpoint.local is not None:
            teleop = make_teleoperator_from_config(endpoint.local)
            teleop_action_processor, robot_action_processor, _ = make_default_processors()
            providers[key] = LocalTeleopCommandProvider(
                teleop=teleop,
                teleop_action_processor=teleop_action_processor,
                robot_action_processor=robot_action_processor,
            )
        else:
            raise ValueError(f"Invalid teleop endpoint configuration for '{name}'")

        teleop_aliases[name] = key
        teleop_aliases[key] = key
    if config.data is not None:
        providers["data"] = DataCollectionCommandProvider(config.data)
        teleop_aliases["data"] = "data"
        teleop_aliases["teleop:data"] = "data"
    ai_cfg = config.ai or AIClientConfig()
    ai_client = ActionInferenceClient(
        host=ai_cfg.host,
        port=ai_cfg.port,
        timeout_ms=ai_cfg.timeout_ms,
        api_token=ai_cfg.api_token,
    )
    startup_timeout_ms = ai_cfg.startup_timeout_ms or ai_cfg.timeout_ms
    try:
        if startup_timeout_ms:
            with ai_client.temporary_timeout(startup_timeout_ms):
                if not ai_client.ping():
                    logger.warning("GR00T ping returned no response (%s:%s)", ai_cfg.host, ai_cfg.port)
        else:
            if not ai_client.ping():
                logger.warning("GR00T ping returned no response (%s:%s)", ai_cfg.host, ai_cfg.port)
    except TimeoutError:
        logger.warning("GR00T ping timed out (%s:%s)", ai_cfg.host, ai_cfg.port)
    except Exception as exc:
        logger.warning("GR00T ping failed: %s", exc)
    ai_provider = AICommandProvider(
        client=ai_client,
        instruction_key=ai_cfg.instruction_key,
        observation_adapter=_build_ai_observation_adapter(ai_cfg),
        action_adapter=_build_gr00t_action_adapter(ai_cfg),
        action_horizon=ai_cfg.action_horizon,
    )
    providers["infer"] = ai_provider
    ai_key: str | None = "infer"

    idle_key = "idle"
    providers[idle_key] = IdleCommandProvider()

    default_key = config.default_mode
    if default_key:
        default_key = teleop_aliases.get(default_key, default_key)
    if not default_key or default_key not in providers:
        default_key = ai_key or next(iter(providers.keys()))

    webviz_cfg = config.webviz or WebVizConfig()
    camera_host = config.camera_stream.host if config.camera_stream else None
    camera_port = config.camera_stream.port if config.camera_stream else None
    visualizer = VisualizationServer(
        host=webviz_cfg.host,
        port=webviz_cfg.port,
        camera_host=camera_host,
        camera_port=camera_port,
    )
    visualizer.start()

    server = CommandService(
        providers=providers,
        default_key=default_key,
        host=config.network.host,
        port=config.network.port,
        api_token=config.network.api_token,
        exchange_hook=visualizer.update,
    )

    if args.mode_dispatcher == "socket":
        socket_path = args.mode_socket
        if socket_path is None:
            parser.error("--mode-socket is required when --mode-dispatcher=socket")
        dispatcher = SocketModeDispatcher(
            path=socket_path,
            backlog=args.mode_socket_backlog,
            unlink_existing=not args.mode_socket_keep,
        )
        print(f"[mode-manager] listening for commands on {socket_path}")
    else:
        dispatcher = CLIModeDispatcher()

    manager = ModeManager(
        service=server,
        dispatcher=dispatcher,
        provider_aliases=teleop_aliases,
        ai_key=ai_key,
        idle_key=idle_key,
    )

    manager.start()
    try:
        server.run()
    except KeyboardInterrupt:
        pass
    finally:
        manager.stop()
        visualizer.stop()


if __name__ == "__main__":
    main()

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
from brainbot_mode_dispatcher import CLIModeDispatcher

from brainbot_webviz import VisualizationServer

from . import (
    AICommandProvider,
    CommandProvider,
    CommandService,
    IdleCommandProvider,
    LocalTeleopCommandProvider,
    ModeManager,
    RemoteTeleopCommandProvider,
)


logger = logging.getLogger(__name__)


def _make_ai_observation_adapter():
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


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True, type=Path)
    args = parser.parse_args(argv)

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
    ai_cfg = config.ai or AIClientConfig()
    ai_client = ActionInferenceClient(
        host=ai_cfg.host,
        port=ai_cfg.port,
        timeout_ms=ai_cfg.timeout_ms,
        api_token=ai_cfg.api_token,
    )
    try:
        if ai_cfg.timeout_ms:
            ai_client.socket.setsockopt(zmq.RCVTIMEO, ai_cfg.timeout_ms)
            ai_client.socket.setsockopt(zmq.SNDTIMEO, ai_cfg.timeout_ms)
        if not ai_client.ping():
            logger.warning("GR00T inference server at %s:%s did not respond to ping", ai_cfg.host, ai_cfg.port)
    except zmq.error.Again:
        logger.warning("GR00T inference server ping timed out (host=%s port=%s)", ai_cfg.host, ai_cfg.port)
    except Exception as exc:
        logger.warning("Could not ping GR00T inference server (%s)", exc)
    ai_provider = AICommandProvider(
        client=ai_client,
        instruction_key=ai_cfg.instruction_key,
        observation_adapter=_make_ai_observation_adapter(),
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

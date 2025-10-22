from __future__ import annotations

import time
from collections import deque
from collections.abc import Callable, Mapping
from typing import Any
import statistics

import numpy as np

try:
    import cv2
except ImportError:  # pragma: no cover - optional dependency
    cv2 = None  # type: ignore[assignment]

from lerobot.robots.robot import Robot

from brainbot_core.config import ActionFilterConfig, ObservationPreprocessConfig
from brainbot_core.proto import ActionMessage, ObservationMessage

ObservationAdapter = Callable[[Mapping[str, Any]], dict[str, Any]]
ActionAdapter = Callable[[Mapping[str, float]], dict[str, float]]


def _numeric_only(values: Mapping[str, Any]) -> dict[str, float]:
    filtered: dict[str, float] = {}
    for key, value in values.items():
        if isinstance(value, (int, float)):
            filtered[key] = float(value)
    return filtered


class _MedianActionFilter:
    def __init__(self, window_size: int):
        self._window_size = max(1, int(window_size))
        self._buffers: dict[str, deque[float]] = {}

    def apply(self, actions: Mapping[str, float]) -> dict[str, float]:
        if self._window_size <= 1:
            return {key: float(value) for key, value in actions.items()}
        filtered: dict[str, float] = {}
        for key, value in actions.items():
            buf = self._buffers.setdefault(key, deque(maxlen=self._window_size))
            buf.append(float(value))
            filtered[key] = float(statistics.median(buf))
        return filtered


class _MedianLowPassFilter:
    def __init__(self, window_size: int, alpha: float):
        self._median = _MedianActionFilter(window_size)
        self._alpha = float(max(0.0, min(1.0, alpha)))
        self._last_output: dict[str, float] | None = None

    def apply(self, actions: Mapping[str, float]) -> dict[str, float]:
        median_output = self._median.apply(actions)
        if self._last_output is None:
            self._last_output = dict(median_output)
            return median_output

        blended: dict[str, float] = {}
        alpha = self._alpha
        for key, value in median_output.items():
            prev = self._last_output.get(key, value)
            blended_value = (1.0 - alpha) * prev + alpha * value
            blended[key] = blended_value
            self._last_output[key] = blended_value

        for key in list(self._last_output.keys()):
            if key not in median_output:
                self._last_output.pop(key)

        return blended


class RobotControlService:
    def __init__(
        self,
        robot: Robot,
        *,
        full_observation_adapter: ObservationAdapter | None = None,
        numeric_observation_adapter: ObservationAdapter | None = None,
        action_adapter: ActionAdapter | None = None,
        preprocess_config: ObservationPreprocessConfig | None = None,
        action_filter_config: ActionFilterConfig | None = None,
        initial_observation_mode: str = "numeric",
    ):
        self.robot = robot
        self._full_adapter = full_observation_adapter or self._identity_adapter
        self._numeric_adapter = numeric_observation_adapter or _numeric_only
        self._action_adapter = action_adapter or (lambda actions: dict(actions))
        self._preprocess_config = preprocess_config
        self._action_filter = self._make_action_filter(action_filter_config)
        allowed = {"numeric", "full", "full_preprocessed"}
        self._current_observation_mode = initial_observation_mode if initial_observation_mode in allowed else "numeric"
        self._last_action = self._zero_action()

    def connect(self, calibrate: bool = True) -> None:
        self.robot.connect(calibrate=calibrate)

    def disconnect(self) -> None:
        self.robot.disconnect()

    def set_observation_mode(self, mode: str) -> None:
        if mode not in {"numeric", "full", "full_preprocessed"}:
            return
        self._current_observation_mode = mode

    def current_observation_mode(self) -> str:
        return self._current_observation_mode

    def get_observation(self, return_raw: bool = False) -> ObservationMessage | tuple[ObservationMessage, dict[str, Any]]:
        robot_obs = self.robot.get_observation()
        processed_obs = self._select_observation_adapter(robot_obs)
        payload: dict[str, Any] = {"robot": processed_obs, "base": {}}
        message = ObservationMessage(payload=payload, timestamp_ns=time.time_ns())
        if return_raw:
            return message, dict(robot_obs)
        return message

    def apply_action(self, action: ActionMessage) -> None:
        mapped_action = self._action_adapter(action.actions)
        mapped_action = self._apply_action_filter(mapped_action)
        if mapped_action:
            self.robot.send_action(mapped_action)
            self._last_action = ActionMessage(actions=dict(mapped_action), timestamp_ns=action.timestamp_ns)
        else:
            self._last_action = ActionMessage(actions={}, timestamp_ns=action.timestamp_ns)

    def last_command(self) -> ActionMessage:
        return self._last_action

    def fallback_command(self) -> ActionMessage:
        return ActionMessage(actions=dict(self._last_action.actions))

    def zero_command(self) -> ActionMessage:
        zero = self._zero_action()
        self._last_action = zero
        return zero

    def _make_action_filter(self, config: ActionFilterConfig | None):
        if config is None:
            return None
        if config.type.lower() == "median":
            return _MedianLowPassFilter(config.window_size, config.blend_alpha)
        return None

    def _apply_action_filter(self, actions: Mapping[str, float]) -> dict[str, float]:
        if not actions:
            return {}
        if self._action_filter is None:
            return dict(actions)
        return self._action_filter.apply(actions)

    def _zero_action(self) -> ActionMessage:
        features = getattr(self.robot, "action_features", {})
        zeros = {key: 0.0 for key in features.keys()}
        return ActionMessage(actions=zeros)

    @staticmethod
    def _identity_adapter(values: Mapping[str, Any]) -> dict[str, Any]:
        return dict(values)

    def _select_observation_adapter(self, robot_obs: Mapping[str, Any]) -> dict[str, Any]:
        if self._current_observation_mode in {"full", "full_preprocessed"}:
            adapted = self._full_adapter(robot_obs)
            if not isinstance(adapted, dict):
                adapted = dict(adapted)
            if self._current_observation_mode == "full_preprocessed" and self._preprocess_config:
                adapted = self._preprocess_cameras(adapted)
            return adapted
        adapted = self._numeric_adapter(robot_obs)
        if not isinstance(adapted, dict):
            adapted = dict(adapted)
        return adapted

    def _preprocess_cameras(self, data: Mapping[str, Any]) -> dict[str, Any]:
        if cv2 is None:
            return dict(data)
        cfg = self._preprocess_config
        assert cfg is not None
        target_height = max(1, int(cfg.target_height))
        target_width = max(1, int(cfg.target_width))
        interpolation = self._resolve_interpolation(cfg.interpolation)
        processed: dict[str, Any] = {}
        for key, value in data.items():
            if isinstance(value, np.ndarray) and value.ndim in (2, 3):
                processed[key] = self._resize_frame(value, target_width, target_height, interpolation)
            else:
                processed[key] = value
        return processed

    def _resolve_interpolation(self, name: str | None) -> int:
        if cv2 is None:
            return 0
        mapping = {
            "nearest": cv2.INTER_NEAREST,
            "linear": cv2.INTER_LINEAR,
            "bilinear": cv2.INTER_LINEAR,
            "cubic": cv2.INTER_CUBIC,
            "area": cv2.INTER_AREA,
            "lanczos": cv2.INTER_LANCZOS4,
        }
        if not name:
            return cv2.INTER_LINEAR
        return mapping.get(name.lower(), cv2.INTER_LINEAR)

    def _resize_frame(self, frame: np.ndarray, width: int, height: int, interpolation: int) -> np.ndarray:
        if cv2 is None:
            return frame
        resized = cv2.resize(frame, (width, height), interpolation=interpolation)
        if frame.ndim == 3 and resized.ndim == 2:
            resized = resized[:, :, None]
        return resized

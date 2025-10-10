from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from typing import Any

from lerobot.robots.robot import Robot

from brainbot_core.proto import ActionMessage, ObservationMessage

from .base import MobileBaseInterface, NoOpMobileBase

ObservationAdapter = Callable[[Mapping[str, Any]], dict[str, Any]]
ActionAdapter = Callable[[Mapping[str, float]], dict[str, float]]


def _numeric_only(values: Mapping[str, Any]) -> dict[str, float]:
    filtered: dict[str, float] = {}
    for key, value in values.items():
        if isinstance(value, (int, float)):
            filtered[key] = float(value)
    return filtered


class RobotControlService:
    def __init__(
        self,
        robot: Robot,
        base: MobileBaseInterface | None = None,
        observation_adapter: ObservationAdapter | None = None,
        action_adapter: ActionAdapter | None = None,
    ):
        self.robot = robot
        self.base = base or NoOpMobileBase()
        self._observation_adapter = observation_adapter or _numeric_only
        self._action_adapter = action_adapter or (lambda actions: dict(actions))
        self._last_action = self._zero_action()

    def connect(self, calibrate: bool = True) -> None:
        self.base.connect()
        self.robot.connect(calibrate=calibrate)

    def disconnect(self) -> None:
        self.base.stop()
        self.base.disconnect()
        self.robot.disconnect()

    def get_observation(self) -> ObservationMessage:
        robot_obs = self.robot.get_observation()
        adapted_robot_obs = self._observation_adapter(robot_obs)
        base_state = self._observation_adapter(self.base.get_state())
        payload: dict[str, Any] = {"robot": adapted_robot_obs, "base": base_state}
        return ObservationMessage(payload=payload, timestamp_ns=time.time_ns())

    def apply_action(self, action: ActionMessage) -> None:
        mapped_action = self._action_adapter(action.actions)
        robot_action: dict[str, float] = {}
        base_action: dict[str, float] = {}
        for key, value in mapped_action.items():
            if key.startswith("base."):
                base_action[key.removeprefix("base.")] = value
            else:
                robot_action[key] = value
        if robot_action:
            self.robot.send_action(robot_action)
        if base_action:
            self.base.send_command(base_action)
        self._last_action = ActionMessage(actions=dict(mapped_action), timestamp_ns=action.timestamp_ns)

    def last_command(self) -> ActionMessage:
        return self._last_action

    def fallback_command(self) -> ActionMessage:
        return ActionMessage(actions=dict(self._last_action.actions))

    def zero_command(self) -> ActionMessage:
        zero = self._zero_action()
        self._last_action = zero
        return zero

    def _zero_action(self) -> ActionMessage:
        features = getattr(self.robot, "action_features", {})
        zeros = {key: 0.0 for key in features.keys()}
        return ActionMessage(actions=zeros)
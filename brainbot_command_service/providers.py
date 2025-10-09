from __future__ import annotations

from abc import ABC, abstractmethod

from collections.abc import Callable
from typing import Any

from gr00t.eval.service import ExternalRobotInferenceClient
from lerobot.processor import RobotProcessorPipeline
from lerobot.teleoperators.teleoperator import Teleoperator

from brainbot_core.proto import ActionMessage, ObservationMessage


class CommandProvider(ABC):
    def prepare(self) -> None:
        return None

    def shutdown(self) -> None:
        return None

    @abstractmethod
    def compute_command(self, observation: ObservationMessage) -> ActionMessage:
        ...


class TeleopCommandProvider(CommandProvider):
    def __init__(
        self,
        teleop: Teleoperator,
        teleop_action_processor: RobotProcessorPipeline | None = None,
        robot_action_processor: RobotProcessorPipeline | None = None,
    ):
        self.teleop = teleop
        self.teleop_action_processor = teleop_action_processor
        self.robot_action_processor = robot_action_processor

    def prepare(self) -> None:
        self.teleop.connect()

    def shutdown(self) -> None:
        self.teleop.disconnect()

    def compute_command(self, observation: ObservationMessage) -> ActionMessage:
        payload = observation.payload.get("robot", {})
        raw_action = self.teleop.get_action()
        teleop_action = raw_action
        if self.teleop_action_processor:
            teleop_action = self.teleop_action_processor((raw_action, payload))
        robot_action = teleop_action
        if self.robot_action_processor:
            robot_action = self.robot_action_processor((robot_action, payload))
        return ActionMessage(actions=dict(robot_action))


class AICommandProvider(CommandProvider):
    def __init__(
        self,
        client: ExternalRobotInferenceClient,
        instruction_key: str = "language_instruction",
        observation_adapter: Callable[[ObservationMessage], dict[str, Any]] | None = None,
        action_adapter: Callable[[dict[str, Any]], dict[str, float]] | None = None,
    ):
        self.client = client
        self.instruction_key = instruction_key
        self._instruction: str | None = None
        self._observation_adapter = observation_adapter or (lambda obs: dict(obs.payload))
        self._action_adapter = action_adapter or _numeric_only

    def set_instruction(self, instruction: str) -> None:
        self._instruction = instruction
        print(f"[ai] instruction set to: {instruction}")

    def compute_command(self, observation: ObservationMessage) -> ActionMessage:
        if not self._instruction:
            return ActionMessage(actions={})
        obs_payload = self._observation_adapter(observation)
        obs_payload[self.instruction_key] = self._instruction
        action_dict = self.client.get_action(obs_payload)
        return ActionMessage(actions=self._action_adapter(action_dict))


class IdleCommandProvider(CommandProvider):
    def __init__(self, actions: dict[str, float] | None = None):
        self._actions = actions or {}

    def compute_command(self, observation: ObservationMessage) -> ActionMessage:
        return ActionMessage(actions=dict(self._actions))


def _numeric_only(values: dict[str, Any]) -> dict[str, float]:
    numeric: dict[str, float] = {}
    for key, value in values.items():
        if isinstance(value, (int, float)):
            numeric[key] = float(value)
    return numeric

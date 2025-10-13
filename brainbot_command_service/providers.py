from __future__ import annotations

from abc import ABC, abstractmethod

from collections.abc import Callable
from typing import Any

from brainbot_core.transport import ActionInferenceClient, BaseZMQClient
import zmq

try:
    from lerobot.processor import RobotProcessorPipeline, make_default_processors
except ImportError:  # compatibility with newer LeRobot releases
    from lerobot.processor.factory import make_default_processors  # type: ignore

    RobotProcessorPipeline = Any  # type: ignore
from lerobot.teleoperators.teleoperator import Teleoperator

from brainbot_core.proto import ActionMessage, MessageSerializer, ObservationMessage


class CommandProvider(ABC):
    def prepare(self) -> None:
        return None

    def shutdown(self) -> None:
        return None

    @abstractmethod
    def compute_command(self, observation: ObservationMessage) -> ActionMessage:
        ...


class IdleCommandProvider(CommandProvider):
    def __init__(self, actions: dict[str, float] | None = None):
        self._actions = actions or {}

    def compute_command(self, observation: ObservationMessage) -> ActionMessage:
        return ActionMessage(actions=dict(self._actions))
    

class AICommandProvider(CommandProvider):
    def __init__(
        self,
        client: ActionInferenceClient,
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

    def clear_instruction(self) -> None:
        self._instruction = None
        print("[ai] instruction cleared")

    def compute_command(self, observation: ObservationMessage) -> ActionMessage:
        if not self._instruction:
            return ActionMessage(actions={})
        obs_payload = self._observation_adapter(observation)
        obs_payload[self.instruction_key] = self._instruction
        obs_payload.setdefault("annotation.human.task_description", self._instruction)
        action_dict = self.client.get_action(obs_payload)
        return ActionMessage(actions=self._action_adapter(action_dict))


class LocalTeleopCommandProvider(CommandProvider):
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
    

class RemoteTeleopClient(BaseZMQClient):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._apply_timeouts()

    def _init_socket(self):
        super()._init_socket()
        self._apply_timeouts()

    def _apply_timeouts(self):
        if self.timeout_ms:
            self.socket.setsockopt(zmq.RCVTIMEO, self.timeout_ms)
            self.socket.setsockopt(zmq.SNDTIMEO, self.timeout_ms)

    def get_action(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self.call_endpoint("get_action", payload)


class RemoteTeleopCommandProvider(CommandProvider):
    def __init__(
        self,
        host: str,
        port: int,
        timeout_ms: int = 1500,
        api_token: str | None = None,
    ):
        self.host = host
        self.port = port
        self.timeout_ms = timeout_ms
        self.api_token = api_token
        self._client: RemoteTeleopClient | None = None

    def prepare(self) -> None:
        if self._client is None:
            self._client = RemoteTeleopClient(
                host=self.host, port=self.port, timeout_ms=self.timeout_ms, api_token=self.api_token
            )
        else:
            self._client._init_socket()
        if not self._client.ping():
            raise ConnectionError(f"Failed to reach teleop server {self.host}:{self.port}")

    def shutdown(self) -> None:
        # Keep the remote connection alive; shutdown is a no-op to
        # avoid closing sockets that might still be in use by ZMQ.
        return None

    def compute_command(self, observation: ObservationMessage) -> ActionMessage:
        if self._client is None:
            raise RuntimeError("Remote teleop client not connected")
        payload = MessageSerializer.to_dict(observation)
        try:
            response = self._client.get_action({"observation": payload})
        except zmq.error.Again as exc:
            raise TimeoutError("Remote teleop timed out") from exc
        if "action" not in response:
            raise RuntimeError(f"Remote teleop response missing action: {response}")
        return MessageSerializer.ensure_action(response["action"])


def _numeric_only(values: dict[str, Any]) -> dict[str, float]:
    numeric: dict[str, float] = {}
    for key, value in values.items():
        if isinstance(value, (int, float)):
            numeric[key] = float(value)
    return numeric

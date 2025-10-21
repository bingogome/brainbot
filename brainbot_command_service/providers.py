from __future__ import annotations

from abc import ABC, abstractmethod

import logging
from collections import deque
from collections.abc import Callable
from typing import Any

from brainbot_core.transport import ActionInferenceClient, BaseZMQClient
import numpy as np
import zmq

try:
    from lerobot.processor import RobotProcessorPipeline, make_default_processors
except ImportError:  # compatibility with newer LeRobot releases
    from lerobot.processor.factory import make_default_processors  # type: ignore

    RobotProcessorPipeline = Any  # type: ignore
from lerobot.teleoperators.teleoperator import Teleoperator

from brainbot_core.proto import ActionMessage, MessageSerializer, ObservationMessage

logger = logging.getLogger(__name__)


class CommandProvider(ABC):
    def prepare(self) -> None:
        return None

    def shutdown(self) -> None:
        return None

    @abstractmethod
    def compute_command(self, observation: ObservationMessage) -> ActionMessage:
        ...

    def wants_full_observation(self) -> bool:
        return False


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
        action_adapter: Callable[[dict[str, Any], int], list[dict[str, float]]] | None = None,
        action_horizon: int = 1,
    ):
        self.client = client
        self.instruction_key = instruction_key
        self._instruction: str | None = None
        self._observation_adapter = observation_adapter or (lambda obs: dict(obs.payload))
        self._action_adapter = action_adapter or _default_action_sequence
        self._action_horizon = max(1, int(action_horizon))
        self._pending_actions: deque[ActionMessage] = deque()

    def set_instruction(self, instruction: str) -> None:
        self._instruction = instruction
        self._pending_actions.clear()
        print(f"[ai] instruction set to: {instruction}")

    def clear_instruction(self) -> None:
        self._instruction = None
        self._pending_actions.clear()
        print("[ai] instruction cleared")

    def wants_full_observation(self) -> bool:
        return True

    def prepare(self) -> None:
        self._pending_actions.clear()

    def shutdown(self) -> None:
        self._pending_actions.clear()

    def compute_command(self, observation: ObservationMessage) -> ActionMessage:
        if not self._instruction:
            self._pending_actions.clear()
            return ActionMessage(actions={})

        if not self._pending_actions:
            obs_payload = self._observation_adapter(observation)
            obs_payload[self.instruction_key] = self._instruction
            desc = obs_payload.get("annotation.human.task_description", self._instruction)
            if isinstance(desc, (list, tuple)):
                obs_payload["annotation.human.task_description"] = list(desc)
            else:
                obs_payload["annotation.human.task_description"] = [desc]
            for key, value in list(obs_payload.items()):
                if isinstance(value, np.ndarray):
                    continue
                if key.startswith("state.") and isinstance(value, list):
                    continue
                if isinstance(value, (list, tuple)):
                    continue
                obs_payload[key] = [value]
            logger.debug("[ai] payload keys: %s", list(obs_payload.keys()))
            try:
                action_chunk = self.client.get_action(obs_payload)
            except TimeoutError:
                logger.warning("[ai] GR00T inference timed out")
                raise
            except Exception as exc:
                logger.error("[ai] inference error: %s", exc)
                raise
            logger.debug("[ai] received action keys: %s", list(action_chunk.keys()))
            try:
                batches = self._action_adapter(action_chunk, self._action_horizon)
            except Exception as exc:
                logger.error("[ai] failed to adapt action chunk: %s", exc)
                raise
            if not batches:
                logger.warning("[ai] action adapter returned no actions; inserting noop")
                batches = [{}]
            for batch in batches:
                self._pending_actions.append(ActionMessage(actions=dict(batch)))

        if not self._pending_actions:
            return ActionMessage(actions={})
        return self._pending_actions.popleft()



def _default_action_sequence(values: dict[str, Any], _: int) -> list[dict[str, float]]:
    numeric = _numeric_only(values)
    return [numeric] if numeric else [{}]



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

from __future__ import annotations

import zmq

from typing import Any, Callable

from brainbot_core.transport import BaseZMQClient
from brainbot_core.proto import ActionMessage, MessageSerializer, ObservationMessage
from brainbot_core.config import RemoteTeleopManagerConfig
from brainbot_service_manager import ServiceManagerClient

from .base import CommandProvider, numeric_only


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


def numeric_observation_payload(observation: ObservationMessage) -> dict[str, Any]:
    serialized = MessageSerializer.to_dict(observation)
    payload = serialized.get("payload", {})
    trimmed: dict[str, any] = {}
    if isinstance(payload, dict):
        robot_raw = payload.get("robot", {})
        base_raw = payload.get("base", {})
        trimmed["robot"] = numeric_only(robot_raw) if isinstance(robot_raw, dict) else {}
        trimmed["base"] = numeric_only(base_raw) if isinstance(base_raw, dict) else {}
        for key, value in payload.items():
            if key in {"robot", "base"}:
                continue
            if isinstance(value, (int, float)):
                trimmed[key] = float(value)
    else:
        trimmed["robot"] = {}
        trimmed["base"] = {}
    trimmed["timestamp_ns"] = serialized.get("timestamp_ns", observation.timestamp_ns)
    metadata = serialized.get("metadata")
    if isinstance(metadata, dict):
        trimmed["metadata"] = {
            key: value for key, value in metadata.items() if isinstance(value, (int, float, str))
        }
    return trimmed


class RemoteTeleopCommandProvider(CommandProvider):
    def __init__(
        self,
        host: str,
        port: int,
        timeout_ms: int = 1500,
        api_token: str | None = None,
        observation_adapter: Callable[[ObservationMessage], dict[str, Any]] | None = None,
        manager_config: RemoteTeleopManagerConfig | None = None,
    ):
        self.host = host
        self.port = port
        self.timeout_ms = timeout_ms
        self.api_token = api_token
        self._client: RemoteTeleopClient | None = None
        self._observation_adapter = observation_adapter or numeric_observation_payload
        self._manager_config = manager_config
        self._manager_client: ServiceManagerClient | None = None
        self._manager_started: bool = False

    def prepare(self) -> None:
        self._ensure_manager_service()
        if self._client is None:
            self._client = RemoteTeleopClient(
                host=self.host, port=self.port, timeout_ms=self.timeout_ms, api_token=self.api_token
            )
        else:
            self._client._init_socket()
        if not self._client.ping():
            raise ConnectionError(f"Failed to reach teleop server {self.host}:{self.port}")

    def shutdown(self) -> None:
        self._stop_manager_service()

    def compute_command(self, observation: ObservationMessage) -> ActionMessage:
        if self._client is None:
            raise RuntimeError("Remote teleop client not connected")
        payload = self._observation_adapter(observation)
        if not isinstance(payload, dict):
            raise TypeError("Remote teleop observation adapter must return a dict")
        try:
            response = self._client.get_action({"observation": payload})
        except zmq.error.Again as exc:
            raise TimeoutError("Remote teleop timed out") from exc
        if "action" not in response:
            raise RuntimeError(f"Remote teleop response missing action: {response}")
        return MessageSerializer.ensure_action(response["action"])

    def _ensure_manager_service(self) -> None:
        if not self._manager_config:
            return
        if self._manager_client is None:
            host = self._manager_config.host or self.host
            self._manager_client = ServiceManagerClient(
                host=host,
                port=self._manager_config.port,
                timeout_ms=max(
                    self.timeout_ms,
                    int((self._manager_config.start_timeout_s + 5.0) * 1000),
                ),
            )
        print(
            f"[remote-teleop] requesting manager start for service '{self._manager_config.service}' "
            f"via {self._manager_client.host}:{self._manager_client.port}"
        )
        self._manager_client.ensure_service(
            self._manager_config.service, timeout_s=self._manager_config.start_timeout_s
        )
        self._manager_started = True

    def _stop_manager_service(self) -> None:
        if not self._manager_started or not self._manager_client or not self._manager_config:
            return
        try:
            print(
                f"[remote-teleop] requesting manager stop for service '{self._manager_config.service}'"
            )
            self._manager_client.stop_service(
                self._manager_config.service, timeout_s=self._manager_config.stop_timeout_s
            )
        except Exception as exc:  # pragma: no cover - defensive
            print(f"[remote-teleop] failed to stop managed service '{self._manager_config.service}': {exc}")
        finally:
            self._manager_started = False


class LocalTeleopCommandProvider(CommandProvider):
    def __init__(
        self,
        teleop,
        teleop_action_processor=None,
        robot_action_processor=None,
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

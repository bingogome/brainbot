from __future__ import annotations

from typing import Any

import zmq

from brainbot_core.transport import BaseZMQClient

from brainbot_core.proto import ActionMessage, MessageSerializer, ObservationMessage


class CommandChannelClient(BaseZMQClient):
    def __init__(
        self,
        host: str,
        port: int,
        timeout_ms: int = 1500,
        api_token: str | None = None,
        max_retries: int = 1,
    ):
        self.max_retries = max_retries
        super().__init__(host=host, port=port, timeout_ms=timeout_ms, api_token=api_token)

    def _init_socket(self) -> None:
        super()._init_socket()
        self.socket.setsockopt(zmq.LINGER, 0)
        self.socket.setsockopt(zmq.RCVTIMEO, self.timeout_ms)
        self.socket.setsockopt(zmq.SNDTIMEO, self.timeout_ms)

    def compute_action(self, observation: ObservationMessage) -> ActionMessage:
        payload = {"observation": MessageSerializer.to_dict(observation)}
        attempt = 0
        while True:
            try:
                response = self.call_endpoint("get_action", data=payload)
            except zmq.error.Again as exc:
                attempt += 1
                if attempt > self.max_retries:
                    raise TimeoutError("Action request timed out") from exc
                self._init_socket()
                continue
            except zmq.error.ZMQError as exc:
                attempt += 1
                if attempt > self.max_retries:
                    raise RuntimeError("Action request failed") from exc
                self._init_socket()
                continue
            if "error" in response:
                raise RuntimeError(f"Command service error: {response['error']}")
            try:
                action_payload = response["action"]
            except KeyError as exc:
                raise RuntimeError(f"Malformed action response: {response}") from exc
            return MessageSerializer.ensure_action(action_payload)

    def sync_config(self, config: dict[str, Any]) -> dict[str, Any]:
        response = self.call_endpoint("sync_config", data=config)
        if "error" in response:
            raise RuntimeError(f"Command service rejected sync_config: {response['error']}")
        return response

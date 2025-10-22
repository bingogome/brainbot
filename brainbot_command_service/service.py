from __future__ import annotations

import threading
from typing import Any, Callable, Mapping

import numpy as np

from brainbot_core.transport import BaseZMQServer
from brainbot_core.proto import ActionMessage, MessageSerializer, ObservationMessage, StatusMessage

from .providers import CommandProvider


class CommandService(BaseZMQServer):
    def __init__(
        self,
        providers: Mapping[str, CommandProvider],
        default_key: str,
        host: str = "*",
        port: int = 6000,
        api_token: str | None = None,
        exchange_hook: Callable[[dict[str, Any], dict[str, Any], str], None] | None = None,
    ):
        super().__init__(host=host, port=port, api_token=api_token)
        if default_key not in providers:
            raise ValueError(f"default provider '{default_key}' not found")
        self._providers = dict(providers)
        self._default_key = default_key
        self._active_key: str | None = None
        self._prepared: set[str] = set()
        self._lock = threading.RLock()
        self._exchange_hook = exchange_hook
        self._last_config: dict[str, Any] = {}
        self._current_mode = default_key
        self._current_observation_hint = "numeric"
        self._shutdown_requested = False
        self._shutdown_notified = threading.Event()
        self.register_endpoint("get_action", self._handle_get_action)
        self.register_endpoint("sync_config", self._handle_sync_config)

    def run(self) -> None:
        self.set_active_provider(self._default_key)
        try:
            super().run()
        finally:
            self._shutdown_active()
            self.close()

    def _handle_get_action(self, data: dict[str, Any]) -> dict[str, Any]:
        observation = MessageSerializer.ensure_observation(data["observation"])
        with self._lock:
            if self._shutdown_requested:
                status_msg = StatusMessage(status="shutdown")
                self._shutdown_notified.set()
                return {"status": MessageSerializer.to_dict(status_msg)}
            key = self._active_key or self._default_key
            provider = self._providers[key]
        requires_full = provider.wants_full_observation()
        if requires_full and not self._observation_contains_images(observation):
            print("[command-service] provider requires camera frames; requesting full observation")
            action = ActionMessage(actions={})
        else:
            action = provider.compute_command(observation)
        obs_dict = MessageSerializer.to_dict(observation)
        action_dict = MessageSerializer.to_dict(action)
        if self._exchange_hook:
            try:
                self._exchange_hook(obs_dict, action_dict, self._current_mode)
            except Exception as exc:
                print(f"[webviz] update failed: {exc}")
        return {"action": action_dict, "observation_hint": self._current_observation_hint}

    def _handle_sync_config(self, config: dict[str, Any]) -> dict[str, Any]:
        self._last_config = config
        return {"status": "ok"}

    def initiate_shutdown(self) -> threading.Event:
        with self._lock:
            self._shutdown_requested = True
        return self._shutdown_notified

    def _post_send(self, endpoint: str, response: dict[str, Any]) -> None:
        if self._shutdown_requested and self._shutdown_notified.is_set():
            self.running = False

    def available_providers(self) -> list[str]:
        return list(self._providers.keys())

    def get_provider(self, key: str) -> CommandProvider:
        return self._providers[key]

    def set_active_provider(self, key: str) -> None:
        with self._lock:
            if key not in self._providers:
                raise ValueError(f"Unknown provider '{key}'")
            if key == self._active_key and key in self._prepared:
                return
            self._shutdown_active()
            provider = self._providers[key]
            provider.prepare()
            self._active_key = key
            self._prepared.add(key)
            self._current_mode = key
            self._current_observation_hint = "full" if provider.wants_full_observation() else "numeric"
            print(f"[command-service] active provider: {key}")

    # ModeController compatibility
    def available_modes(self) -> list[str]:
        return self.available_providers()

    def set_mode(self, key: str) -> None:
        self.set_active_provider(key)

    def get_mode_handler(self, key: str) -> CommandProvider:
        return self.get_provider(key)

    def _observation_contains_images(self, observation: ObservationMessage) -> bool:
        robot_payload = observation.payload.get("robot", {})
        for value in robot_payload.values():
            if isinstance(value, np.ndarray) and value.ndim >= 2:
                return True
        return False

    def _shutdown_active(self) -> None:
        if self._active_key and self._active_key in self._providers:
            provider = self._providers[self._active_key]
            provider.shutdown()
            self._prepared.discard(self._active_key)
            self._active_key = None
            self._current_observation_hint = "numeric"

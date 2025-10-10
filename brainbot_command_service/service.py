from __future__ import annotations

import threading
from typing import Any, Callable, Mapping

from gr00t.eval.service import BaseInferenceServer

from brainbot_core.proto import MessageSerializer

from .providers import CommandProvider


class CommandService(BaseInferenceServer):
    def __init__(
        self,
        providers: Mapping[str, CommandProvider],
        default_key: str,
        host: str = "*",
        port: int = 5555,
        api_token: str | None = None,
        exchange_hook: Callable[[dict[str, Any], dict[str, Any]], None] | None = None,
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
        self.register_endpoint("get_action", self._handle_get_action)
        self.register_endpoint("sync_config", self._handle_sync_config)

    def run(self) -> None:
        self.set_active_provider(self._default_key)
        try:
            super().run()
        finally:
            self._shutdown_active()

    def _handle_get_action(self, data: dict[str, Any]) -> dict[str, Any]:
        observation = MessageSerializer.ensure_observation(data["observation"])
        with self._lock:
            key = self._active_key or self._default_key
            provider = self._providers[key]
        action = provider.compute_command(observation)
        obs_dict = MessageSerializer.to_dict(observation)
        action_dict = MessageSerializer.to_dict(action)
        if self._exchange_hook:
            try:
                self._exchange_hook(obs_dict, action_dict, self._current_mode)
            except Exception as exc:
                print(f"[webviz] update failed: {exc}")
        return {"action": action_dict}

    def _handle_sync_config(self, config: dict[str, Any]) -> dict[str, Any]:
        self._last_config = config
        return {"status": "ok"}

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
            print(f"[command-service] active provider: {key}")

    # ModeController compatibility
    def available_modes(self) -> list[str]:
        return self.available_providers()

    def set_mode(self, key: str) -> None:
        self.set_active_provider(key)

    def get_mode_handler(self, key: str) -> CommandProvider:
        return self.get_provider(key)

    def _shutdown_active(self) -> None:
        if self._active_key and self._active_key in self._providers:
            provider = self._providers[self._active_key]
            provider.shutdown()
            self._prepared.discard(self._active_key)
            self._active_key = None

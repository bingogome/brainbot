from __future__ import annotations

from typing import Mapping

from brainbot_mode_dispatcher import IdleModeEvent, InferenceModeEvent, ModeEvent, ModeEventDispatcher, ShutdownModeEvent, TeleopModeEvent

from .providers import AICommandProvider, CommandProvider
from .service import CommandService


class ModeManager:
    def __init__(
        self,
        service: CommandService,
        dispatcher: ModeEventDispatcher,
        provider_aliases: Mapping[str, str],
        ai_key: str | None = None,
        instruction_attr: str = "set_instruction",
        idle_key: str | None = None,
    ):
        self._service = service
        self._dispatcher = dispatcher
        self._provider_aliases = dict(provider_aliases)
        self._ai_key = ai_key
        self._instruction_attr = instruction_attr
        self._idle_key = idle_key
        self._shutting_down = False

    def start(self) -> None:
        self._dispatcher.start(self._handle_event)
        human_aliases = sorted(
            alias for alias in self._provider_aliases if not alias.startswith("teleop:")
        )
        if human_aliases:
            print(f"[mode-manager] teleop aliases: {', '.join(human_aliases)}")
        if self._ai_key:
            print('[mode-manager] AI mode available: {"infer": "<instruction>"}')

    def stop(self) -> None:
        self._dispatcher.stop()

    def _handle_event(self, event: ModeEvent) -> None:
        if self._shutting_down:
            return
        if isinstance(event, TeleopModeEvent):
            key = self._provider_aliases.get(event.alias, event.alias)
            try:
                if self._ai_key:
                    handler = self._service.get_mode_handler(self._ai_key)
                    if isinstance(handler, AICommandProvider):
                        handler.clear_instruction()
                self._service.set_mode(key)
            except ValueError as exc:
                print(f"[mode-manager] {exc}")
            return

        if isinstance(event, InferenceModeEvent) and self._ai_key:
            handler = self._service.get_mode_handler(self._ai_key)
            if isinstance(handler, AICommandProvider):
                getattr(handler, self._instruction_attr, lambda _: None)(event.instruction)
            self._service.set_mode(self._ai_key)
            return

        if isinstance(event, IdleModeEvent) and self._idle_key:
            try:
                self._service.set_mode(self._idle_key)
                if self._ai_key:
                    handler = self._service.get_mode_handler(self._ai_key)
                    if isinstance(handler, AICommandProvider):
                        handler.clear_instruction()
            except ValueError as exc:
                print(f"[mode-manager] {exc}")
            return

        if isinstance(event, ShutdownModeEvent):
            print("[mode-manager] shutdown requested")
            self._shutting_down = True
            if self._idle_key:
                try:
                    self._service.set_mode(self._idle_key)
                except ValueError as exc:
                    print(f"[mode-manager] {exc}")
            shutdown_event = self._service.initiate_shutdown()
            if shutdown_event.wait(timeout=2.0):
                print("[mode-manager] robot acknowledged shutdown")
            else:
                print("[mode-manager] no shutdown acknowledgement from robot (timeout)")

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Protocol


class ModeEvent:
    """Base class for mode events."""


@dataclass(slots=True)
class TeleopModeEvent(ModeEvent):
    alias: str


@dataclass(slots=True)
class InferenceModeEvent(ModeEvent):
    instruction: str


@dataclass(slots=True)
class IdleModeEvent(ModeEvent):
    reason: str | None = None


@dataclass(slots=True)
class ShutdownModeEvent(ModeEvent):
    reason: str | None = None


ModeEventListener = Callable[[ModeEvent], None]


class ModeEventDispatcher(Protocol):
    def start(self, listener: ModeEventListener) -> None:
        ...

    def stop(self) -> None:
        ...

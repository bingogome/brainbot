from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Mapping

from brainbot_core.proto import ActionMessage, ObservationMessage


def numeric_only(values: Mapping[str, Any]) -> dict[str, float]:
    numeric: dict[str, float] = {}
    for key, value in values.items():
        if isinstance(value, (int, float)):
            numeric[key] = float(value)
    return numeric


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
    def __init__(self, actions: Mapping[str, float] | None = None):
        self._actions = dict(actions or {})

    def compute_command(self, observation: ObservationMessage) -> ActionMessage:
        return ActionMessage(actions=dict(self._actions))

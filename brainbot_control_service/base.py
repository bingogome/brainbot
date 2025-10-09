from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Mapping


class MobileBaseInterface(ABC):
    @abstractmethod
    def connect(self) -> None:
        ...

    @abstractmethod
    def disconnect(self) -> None:
        ...

    @abstractmethod
    def get_state(self) -> Mapping[str, Any]:
        ...

    @abstractmethod
    def send_command(self, command: Mapping[str, float]) -> None:
        ...

    @abstractmethod
    def stop(self) -> None:
        ...


class NoOpMobileBase(MobileBaseInterface):
    def connect(self) -> None:
        return None

    def disconnect(self) -> None:
        return None

    def get_state(self) -> Mapping[str, Any]:
        return {}

    def send_command(self, command: Mapping[str, float]) -> None:
        return None

    def stop(self) -> None:
        return None


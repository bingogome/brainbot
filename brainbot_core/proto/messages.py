from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from typing import Any, Literal, Mapping

import msgpack
import numpy as np


MessageType = Literal["observation", "action", "status"]


@dataclass(slots=True)
class ObservationMessage:
    payload: Mapping[str, Any]
    timestamp_ns: int = field(default_factory=lambda: time.time_ns())
    metadata: Mapping[str, Any] | None = None
    version: int = 1


@dataclass(slots=True)
class ActionMessage:
    actions: Mapping[str, float]
    timestamp_ns: int = field(default_factory=lambda: time.time_ns())
    metadata: Mapping[str, Any] | None = None
    version: int = 1


@dataclass(slots=True)
class StatusMessage:
    status: str
    timestamp_ns: int = field(default_factory=lambda: time.time_ns())
    metadata: Mapping[str, Any] | None = None
    version: int = 1


def _to_builtin(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(k): _to_builtin(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_to_builtin(v) for v in value]
    if isinstance(value, np.ndarray):
        return value.tolist()
    if isinstance(value, (int, float, str, bool)) or value is None:
        return value
    if hasattr(value, "tolist"):
        return value.tolist()
    if hasattr(value, "item"):
        try:
            item = value.item()
        except Exception:
            return value
        if isinstance(item, (int, float, bool)):
            return float(item) if isinstance(item, (int, float)) else item
    return value


class MessageSerializer:
    @staticmethod
    def dump(message: ObservationMessage | ActionMessage | StatusMessage) -> bytes:
        return msgpack.packb(MessageSerializer.to_dict(message), use_bin_type=True)

    @staticmethod
    def load(payload: bytes) -> ObservationMessage | ActionMessage | StatusMessage:
        data = msgpack.unpackb(payload, raw=False)
        return MessageSerializer.from_dict(data)

    @staticmethod
    def to_dict(message: ObservationMessage | ActionMessage | StatusMessage) -> dict[str, Any]:
        data = asdict(message)
        data["message_type"] = message.__class__.__name__.removesuffix("Message").lower()
        if "payload" in data:
            data["payload"] = _to_builtin(data["payload"])
        if "actions" in data:
            data["actions"] = _to_builtin(data["actions"])
        if "metadata" in data:
            data["metadata"] = _to_builtin(data["metadata"])
        return data

    @staticmethod
    def from_dict(data: Mapping[str, Any]) -> ObservationMessage | ActionMessage | StatusMessage:
        message_type: MessageType = data.get("message_type", "status")  # type: ignore[assignment]
        if message_type == "observation":
            return ObservationMessage(
                payload=data["payload"],
                timestamp_ns=int(data.get("timestamp_ns", time.time_ns())),
                metadata=data.get("metadata"),
                version=int(data.get("version", 1)),
            )
        if message_type == "action":
            return ActionMessage(
                actions=data["actions"],
                timestamp_ns=int(data.get("timestamp_ns", time.time_ns())),
                metadata=data.get("metadata"),
                version=int(data.get("version", 1)),
            )
        if message_type == "status":
            return StatusMessage(
                status=data["status"],
                timestamp_ns=int(data.get("timestamp_ns", time.time_ns())),
                metadata=data.get("metadata"),
                version=int(data.get("version", 1)),
            )
        raise ValueError(f"Unsupported message type: {message_type}")

    @staticmethod
    def ensure_action(data: Mapping[str, Any]) -> ActionMessage:
        message = MessageSerializer.from_dict(data)
        if not isinstance(message, ActionMessage):
            raise TypeError(f"Expected ActionMessage, got {type(message).__name__}")
        return message

    @staticmethod
    def ensure_observation(data: Mapping[str, Any]) -> ObservationMessage:
        message = MessageSerializer.from_dict(data)
        if not isinstance(message, ObservationMessage):
            raise TypeError(f"Expected ObservationMessage, got {type(message).__name__}")
        return message

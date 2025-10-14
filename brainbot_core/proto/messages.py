from __future__ import annotations

import io
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
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


_NDARRAY_FLAG = "__ndarray__"
_NDARRAY_BUFFER = "npy"


def _normalize(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(k): _normalize(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_normalize(v) for v in value]
    if isinstance(value, (set, frozenset)):
        return [_normalize(v) for v in sorted(value, key=lambda x: str(x))]
    if hasattr(value, "__fspath__"):
        return str(value)
    if isinstance(value, bytearray):
        return bytes(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, np.generic):
        return value.item()
    return value


def _msgpack_encode(obj: Any) -> Any:
    if isinstance(obj, np.ndarray):
        buffer = io.BytesIO()
        np.save(buffer, obj, allow_pickle=False)
        return {_NDARRAY_FLAG: True, _NDARRAY_BUFFER: buffer.getvalue()}
    if isinstance(obj, np.generic):
        return obj.item()
    raise TypeError(f"Unsupported type for message serialization: {type(obj).__name__}")


def _msgpack_decode(obj: Any) -> Any:
    if isinstance(obj, Mapping) and obj.get(_NDARRAY_FLAG):
        data = obj.get(_NDARRAY_BUFFER)
        if not isinstance(data, (bytes, bytearray)):
            raise TypeError("Invalid ndarray payload")
        buffer = io.BytesIO(data)
        return np.load(buffer, allow_pickle=False)
    return obj


class MessageSerializer:
    @staticmethod
    def dump(message: ObservationMessage | ActionMessage | StatusMessage) -> bytes:
        return msgpack.packb(
            MessageSerializer.to_dict(message),
            use_bin_type=True,
            default=_msgpack_encode,
        )

    @staticmethod
    def load(payload: bytes) -> ObservationMessage | ActionMessage | StatusMessage:
        data = msgpack.unpackb(payload, raw=False, object_hook=_msgpack_decode)
        return MessageSerializer.from_dict(data)

    @staticmethod
    def to_dict(message: ObservationMessage | ActionMessage | StatusMessage) -> dict[str, Any]:
        data = asdict(message)
        data["message_type"] = message.__class__.__name__.removesuffix("Message").lower()
        if "payload" in data:
            data["payload"] = _normalize(data["payload"])
        if "actions" in data:
            data["actions"] = _normalize(data["actions"])
        if "metadata" in data:
            data["metadata"] = _normalize(data["metadata"])
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

    @staticmethod
    def ensure_status(data: Mapping[str, Any]) -> StatusMessage:
        message = MessageSerializer.from_dict(data)
        if not isinstance(message, StatusMessage):
            raise TypeError(f"Expected StatusMessage, got {type(message).__name__}")
        return message

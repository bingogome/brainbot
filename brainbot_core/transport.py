# SPDX-FileCopyrightText: Copyright (c) 2025 NVIDIA CORPORATION &
# AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
#
# Portions derived from the GR00T project
# (https://github.com/NVIDIA-ISAAC/Isaac-GR00T), adapted so that Brainbot
# can speak the same ZeroMQ protocol without requiring the external
# dependency. We keep the original licensing terms and protocol semantics.

from __future__ import annotations

import io
import json
from dataclasses import dataclass
from typing import Any, Callable

import msgpack
import numpy as np
import zmq


@dataclass
class ModalityConfig:
    """Lightweight stand-in for GR00T's Pydantic ModalityConfig."""

    delta_indices: list[int]
    modality_keys: list[str]

    def model_dump_json(self) -> str:
        return json.dumps(
            {
                "delta_indices": list(self.delta_indices),
                "modality_keys": list(self.modality_keys),
            }
        )

    @classmethod
    def from_json(cls, payload: str) -> "ModalityConfig":
        data = json.loads(payload)
        return cls(
            delta_indices=list(data.get("delta_indices", [])),
            modality_keys=list(data.get("modality_keys", [])),
        )


class MsgSerializer:
    @staticmethod
    def to_bytes(data: dict) -> bytes:
        return msgpack.packb(data, default=MsgSerializer.encode_custom_classes)

    @staticmethod
    def from_bytes(data: bytes) -> dict:
        return msgpack.unpackb(data, object_hook=MsgSerializer.decode_custom_classes)

    @staticmethod
    def decode_custom_classes(obj):
        if "__ModalityConfig_class__" in obj:
            return ModalityConfig.from_json(obj["as_json"])
        if "__ndarray_class__" in obj:
            return np.load(io.BytesIO(obj["as_npy"]), allow_pickle=False)
        return obj

    @staticmethod
    def encode_custom_classes(obj):
        if isinstance(obj, ModalityConfig):
            return {"__ModalityConfig_class__": True, "as_json": obj.model_dump_json()}
        if isinstance(obj, np.ndarray):
            output = io.BytesIO()
            np.save(output, obj, allow_pickle=False)
            return {"__ndarray_class__": True, "as_npy": output.getvalue()}
        return obj


class EndpointHandler:
    def __init__(self, handler: Callable, requires_input: bool = True):
        self.handler = handler
        self.requires_input = requires_input


class BaseZMQServer:
    """ZeroMQ REP server exposing named endpoints."""

    def __init__(self, host: str = "*", port: int = 5555, api_token: str | None = None):
        self.running = True
        self.context = zmq.Context.instance()
        self.socket = self.context.socket(zmq.REP)
        self.socket.setsockopt(zmq.LINGER, 0)
        self.socket.bind(f"tcp://{host}:{port}")
        self._endpoints: dict[str, EndpointHandler] = {}
        self.api_token = api_token

        self.register_endpoint("ping", self._handle_ping, requires_input=False)
        self.register_endpoint("kill", self._kill_server, requires_input=False)

    def _kill_server(self):
        self.running = False

    def _handle_ping(self) -> dict:
        return {"status": "ok", "message": "Server is running"}

    def register_endpoint(self, name: str, handler: Callable, requires_input: bool = True):
        self._endpoints[name] = EndpointHandler(handler, requires_input)

    def _validate_token(self, request: dict) -> bool:
        if self.api_token is None:
            return True
        return request.get("api_token") == self.api_token

    def run(self):
        addr = self.socket.getsockopt_string(zmq.LAST_ENDPOINT)
        print(f"Server is ready and listening on {addr}")
        while self.running:
            try:
                message = self.socket.recv()
                request = MsgSerializer.from_bytes(message)

                if not self._validate_token(request):
                    self.socket.send(
                        MsgSerializer.to_bytes({"error": "Unauthorized: Invalid API token"})
                    )
                    continue

                endpoint = request.get("endpoint", "get_action")

                if endpoint not in self._endpoints:
                    raise ValueError(f"Unknown endpoint: {endpoint}")

                handler = self._endpoints[endpoint]
                result = (
                    handler.handler(request.get("data", {}))
                    if handler.requires_input
                    else handler.handler()
                )
                self.socket.send(MsgSerializer.to_bytes(result))
            except zmq.error.ContextTerminated:
                break
            except Exception as exc:
                if not self.running:
                    break
                print(f"Error in server: {exc}")
                import traceback

                print(traceback.format_exc())
                try:
                    self.socket.send(MsgSerializer.to_bytes({"error": str(exc)}))
                except zmq.error.ZMQError:
                    break

    def close(self) -> None:
        self.running = False
        try:
            self.socket.close(0)
        except Exception:
            pass
        self.socket = None  # type: ignore[assignment]


class BaseZMQClient:
    def __init__(
        self,
        host: str = "localhost",
        port: int = 5555,
        timeout_ms: int = 15000,
        api_token: str | None = None,
    ):
        self.context = zmq.Context.instance()
        self.host = host
        self.port = port
        self.timeout_ms = timeout_ms
        self.api_token = api_token
        self._init_socket()

    def _init_socket(self):
        existing = getattr(self, "socket", None)
        if existing is not None:
            try:
                existing.close(0)
            except Exception:
                pass
        self.socket = self.context.socket(zmq.REQ)
        self.socket.setsockopt(zmq.LINGER, 0)
        if self.timeout_ms:
            self.socket.setsockopt(zmq.RCVTIMEO, self.timeout_ms)
            self.socket.setsockopt(zmq.SNDTIMEO, self.timeout_ms)
        self.socket.connect(f"tcp://{self.host}:{self.port}")

    def ping(self) -> bool:
        try:
            self.call_endpoint("ping", requires_input=False)
            return True
        except zmq.error.ZMQError:
            self._init_socket()
            return False

    def kill_server(self):
        self.call_endpoint("kill", requires_input=False)

    def call_endpoint(
        self, endpoint: str, data: dict | None = None, requires_input: bool = True
    ) -> dict:
        request: dict[str, Any] = {"endpoint": endpoint}
        if requires_input:
            request["data"] = data
        if self.api_token:
            request["api_token"] = self.api_token

        self.socket.send(MsgSerializer.to_bytes(request))
        message = self.socket.recv()

        response = MsgSerializer.from_bytes(message)

        if "error" in response:
            raise RuntimeError(f"Server error: {response['error']}")
        return response

    def __del__(self):
        try:
            self.socket.close(0)
        except Exception:
            pass
        try:
            self.context.term()
        except Exception:
            pass

    def close(self) -> None:
        self.__del__()


class ActionInferenceClient(BaseZMQClient):
    """Client to query an inference server for robot actions."""

    def get_action(self, observations: dict[str, Any]) -> dict[str, Any]:
        return self.call_endpoint("get_action", observations)


__all__ = [
    "ActionInferenceClient",
    "BaseZMQClient",
    "BaseZMQServer",
    "ModalityConfig",
]

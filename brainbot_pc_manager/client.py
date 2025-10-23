from __future__ import annotations

import time
from typing import Any

from brainbot_core.transport import BaseZMQClient


class PCServiceManagerClient(BaseZMQClient):
    """Client to control the PC service manager."""

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 7100,
        timeout_ms: int = 5000,
        api_token: str | None = None,
    ):
        super().__init__(host=host, port=port, timeout_ms=timeout_ms, api_token=api_token)

    def start_service(self, service: str, timeout_s: float | None = None) -> dict[str, Any]:
        payload: dict[str, Any] = {"service": service}
        if timeout_s is not None:
            payload["timeout_s"] = float(timeout_s)
        return self.call_endpoint("start_service", payload)

    def stop_service(self, service: str, timeout_s: float | None = None) -> dict[str, Any]:
        payload: dict[str, Any] = {"service": service}
        if timeout_s is not None:
            payload["timeout_s"] = float(timeout_s)
        return self.call_endpoint("stop_service", payload)

    def list_services(self) -> dict[str, Any]:
        return self.call_endpoint("list_services", requires_input=False)

    def ensure_service(self, service: str, timeout_s: float) -> None:
        deadline = time.time() + timeout_s
        remaining = max(0.0, timeout_s)
        response = self.start_service(service, timeout_s=remaining)
        if response.get("status") != "running":
            raise RuntimeError(f"Failed to start service '{service}': {response}")
        status = response.get("service")
        if isinstance(status, dict) and status.get("state") == "running":
            return
        # If the manager reports pending readiness, poll until deadline.
        while time.time() < deadline:
            info = self.list_services()
            service_state = info.get("services", {}).get(service)
            if isinstance(service_state, dict) and service_state.get("state") == "running":
                return
            time.sleep(0.1)
        raise TimeoutError(f"Timed out waiting for service '{service}' to become ready")

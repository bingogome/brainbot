from __future__ import annotations

import os
import shlex
import socket
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

from brainbot_core.transport import BaseZMQServer


@dataclass(slots=True)
class ServiceSpec:
    """Specification for a managed subprocess."""

    name: str
    command: Sequence[str]
    cwd: str | None = None
    env: dict[str, str] | None = None
    ready_host: str = "127.0.0.1"
    ready_port: int | None = None
    start_timeout_s: float = 10.0
    stop_grace_s: float = 5.0

    def resolved_command(self) -> list[str]:
        resolved: list[str] = []
        for part in self.command:
            if part == "{python}":
                resolved.append(os.environ.get("PYTHON_EXECUTABLE", os.sys.executable))
            else:
                resolved.append(part)
        return resolved


@dataclass(slots=True)
class RunningService:
    spec: ServiceSpec
    process: subprocess.Popen[Any] | None = None
    started_at: float = field(default_factory=time.time)

    def is_running(self) -> bool:
        return self.process is not None and self.process.poll() is None


class PCServiceManager(BaseZMQServer):
    """ZeroMQ server that starts/stops teleop/data servers on demand."""

    def __init__(
        self,
        *,
        host: str = "0.0.0.0",
        port: int = 7100,
        api_token: str | None = None,
        services: dict[str, ServiceSpec],
    ):
        super().__init__(host=host, port=port, api_token=api_token)
        self._services = services
        self._running: dict[str, RunningService] = {}
        self._lock = threading.RLock()
        self.register_endpoint("start_service", self._handle_start_service)
        self.register_endpoint("stop_service", self._handle_stop_service)
        self.register_endpoint("list_services", self._handle_list_services, requires_input=False)

    def _handle_start_service(self, data: dict[str, Any]) -> dict[str, Any]:
        service_name = str(data.get("service", "")).strip()
        if not service_name:
            return {"error": "Missing service name"}
        timeout_s = float(data.get("timeout_s", 0.0) or 0.0)
        spec = self._services.get(service_name)
        if spec is None:
            return {"error": f"Unknown service '{service_name}'"}
        with self._lock:
            runner = self._running.get(service_name)
            if runner is None or not runner.is_running():
                runner = self._start_service(spec, timeout_override=timeout_s if timeout_s > 0 else None)
                self._running[service_name] = runner
            else:
                print(f"[pc-manager] service '{service_name}' already running (pid={runner.process.pid})")
        service_state = self._describe_service(service_name)
        return {"status": "running", "service": service_state}

    def _handle_stop_service(self, data: dict[str, Any]) -> dict[str, Any]:
        service_name = str(data.get("service", "")).strip()
        if not service_name:
            return {"error": "Missing service name"}
        timeout_s = float(data.get("timeout_s", 0.0) or 0.0)
        spec = self._services.get(service_name)
        if spec is None:
            return {"error": f"Unknown service '{service_name}'"}
        with self._lock:
            runner = self._running.get(service_name)
            if runner is None or not runner.is_running():
                print(f"[pc-manager] service '{service_name}' already stopped")
                return {"status": "stopped", "service": self._describe_service(service_name)}
            self._stop_service(runner, timeout_override=timeout_s if timeout_s > 0 else None)
            self._running.pop(service_name, None)
        return {"status": "stopped", "service": self._describe_service(service_name)}

    def _handle_list_services(self) -> dict[str, Any]:
        with self._lock:
            services = {name: self._describe_service(name) for name in self._services}
        return {"services": services}

    def _describe_service(self, name: str) -> dict[str, Any]:
        spec = self._services.get(name)
        runner = self._running.get(name)
        state = {
            "command": spec.command if spec else [],
            "state": "stopped",
        }
        if runner and runner.is_running():
            state.update(
                {
                    "state": "running",
                    "pid": runner.process.pid if runner.process else None,
                    "uptime_s": time.time() - runner.started_at,
                }
            )
        elif runner and runner.process is not None:
            state.update({"state": "exited", "returncode": runner.process.poll()})
        return state

    def _start_service(self, spec: ServiceSpec, timeout_override: float | None = None) -> RunningService:
        command = spec.resolved_command()
        cwd = spec.cwd
        if cwd:
            cwd = str(Path(cwd).expanduser())
        env = os.environ.copy()
        if spec.env:
            env.update({key: str(value) for key, value in spec.env.items()})
        print(f"[pc-manager] starting service '{spec.name}': {' '.join(command)} (cwd={cwd or os.getcwd()})")
        process = subprocess.Popen(command, cwd=cwd, env=env, stdout=None, stderr=None)
        runner = RunningService(spec=spec, process=process, started_at=time.time())
        timeout = timeout_override if timeout_override is not None else spec.start_timeout_s
        if spec.ready_port is not None:
            self._wait_for_port(spec.ready_host, spec.ready_port, timeout)
        return runner

    def _stop_service(self, runner: RunningService, timeout_override: float | None = None) -> None:
        proc = runner.process
        if proc is None:
            return
        if proc.poll() is not None:
            return
        timeout = timeout_override if timeout_override is not None else runner.spec.stop_grace_s
        print(f"[pc-manager] stopping service '{runner.spec.name}' (pid={proc.pid})")
        proc.terminate()
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            print(f"[pc-manager] service '{runner.spec.name}' did not exit in {timeout:.1f}s; killing")
            proc.kill()
            proc.wait(timeout=5.0)

    @staticmethod
    def _wait_for_port(host: str, port: int, timeout: float) -> None:
        deadline = time.time() + timeout
        last_error: Exception | None = None
        while time.time() < deadline:
            try:
                with socket.create_connection((host, port), timeout=0.5):
                    return
            except OSError as exc:  # pragma: no cover - network dependent
                last_error = exc
                time.sleep(0.2)
        raise TimeoutError(f"Timed out waiting for service on {host}:{port}") from last_error

    def close(self) -> None:
        with self._lock:
            for runner in list(self._running.values()):
                try:
                    self._stop_service(runner)
                except Exception as exc:  # pragma: no cover - defensive
                    print(f"[pc-manager] failed to stop service '{runner.spec.name}': {exc}")
            self._running.clear()
        super().close()

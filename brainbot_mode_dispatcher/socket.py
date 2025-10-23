from __future__ import annotations

import json
import os
import queue
import socket
import threading
from pathlib import Path

from .commands import enqueue_mode_command
from .events import ModeEvent, ModeEventDispatcher, ModeEventListener


class SocketModeDispatcher(ModeEventDispatcher):
    """Dispatch mode events received over a UNIX domain socket."""

    def __init__(
        self,
        path: str | os.PathLike[str],
        backlog: int = 5,
        unlink_existing: bool = True,
    ) -> None:
        self._path = Path(path)
        self._backlog = max(int(backlog), 1)
        self._unlink_existing = unlink_existing
        self._queue: queue.Queue[ModeEvent] | None = None
        self._stop_event = threading.Event()
        self._listener: ModeEventListener | None = None
        self._dispatcher_thread: threading.Thread | None = None
        self._accept_thread: threading.Thread | None = None
        self._server: socket.socket | None = None
        self._connections: set[socket.socket] = set()
        self._conn_lock = threading.Lock()

    def start(self, listener: ModeEventListener) -> None:  # type: ignore[override]
        self._stop_event.clear()
        self._queue = queue.Queue()
        self._listener = listener
        self._prepare_socket()
        self._dispatcher_thread = threading.Thread(target=self._dispatch_loop, daemon=True)
        self._dispatcher_thread.start()
        self._accept_thread = threading.Thread(target=self._accept_loop, daemon=True)
        self._accept_thread.start()

    def stop(self) -> None:  # type: ignore[override]
        self._stop_event.set()
        if self._server is not None:
            try:
                self._server.close()
            except OSError:
                pass
        with self._conn_lock:
            for conn in list(self._connections):
                try:
                    conn.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                try:
                    conn.close()
                except OSError:
                    pass
            self._connections.clear()
        if self._accept_thread and self._accept_thread.is_alive():
            self._accept_thread.join(timeout=0.5)
        if self._dispatcher_thread and self._dispatcher_thread.is_alive():
            self._dispatcher_thread.join(timeout=0.5)
        self._queue = None
        self._listener = None
        self._dispatcher_thread = None
        self._accept_thread = None
        self._server = None
        if self._unlink_existing and self._path.exists():
            try:
                self._path.unlink()
            except OSError:
                pass

    def _prepare_socket(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        if self._unlink_existing and self._path.exists():
            try:
                if self._path.is_socket():  # type: ignore[attr-defined]
                    self._path.unlink()
                else:
                    raise RuntimeError(f"Socket path {self._path} already exists and is not a socket")
            except AttributeError:
                self._path.unlink()
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind(str(self._path))
        server.listen(self._backlog)
        server.settimeout(0.2)
        self._server = server

    def _accept_loop(self) -> None:
        server = self._server
        if server is None:
            return
        while not self._stop_event.is_set():
            try:
                conn, _ = server.accept()
            except TimeoutError:
                continue
            except OSError:
                break
            conn.setblocking(True)
            with self._conn_lock:
                self._connections.add(conn)
            thread = threading.Thread(target=self._client_loop, args=(conn,), daemon=True)
            thread.start()

    def _client_loop(self, conn: socket.socket) -> None:
        reader = None
        try:
            reader = conn.makefile("r", encoding="utf-8", newline="\n")
            while not self._stop_event.is_set():
                line = reader.readline()
                if not line:
                    break
                payload = line.strip()
                if not payload:
                    continue
                try:
                    data = json.loads(payload)
                except json.JSONDecodeError as exc:
                    self._send_response(conn, f"ERROR: invalid JSON ({exc})\n")
                    continue
                if not isinstance(data, dict):
                    self._send_response(conn, "ERROR: command must be a JSON object\n")
                    continue
                if self._handle_command(data):
                    self._send_response(conn, "OK\n")
                else:
                    self._send_response(conn, "ERROR: unsupported command\n")
        except Exception as exc:
            self._send_response(conn, f"ERROR: {exc}\n")
        finally:
            if reader is not None:
                try:
                    reader.close()
                except Exception:
                    pass
            try:
                conn.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                conn.close()
            except OSError:
                pass
            with self._conn_lock:
                self._connections.discard(conn)


    def _dispatch_loop(self) -> None:
        queue_obj = self._queue
        listener = self._listener
        if queue_obj is None or listener is None:
            return
        while not self._stop_event.is_set():
            try:
                event = queue_obj.get(timeout=0.1)
            except queue.Empty:
                continue
            listener(event)

    def _handle_command(self, data: dict) -> bool:
        queue_obj = self._queue
        if queue_obj is None:
            return False
        return enqueue_mode_command(data, queue_obj)

    def _send_response(self, conn: socket.socket, message: str) -> None:
        try:
            conn.sendall(message.encode("utf-8"))
        except OSError:
            pass


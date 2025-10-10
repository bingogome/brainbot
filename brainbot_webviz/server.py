from __future__ import annotations

import json
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any


class VisualizationServer:
    def __init__(self, host: str = "0.0.0.0", port: int = 8080):
        self.host = host
        self.port = port
        self._lock = threading.Lock()
        self._data: dict[str, Any] = {
            "observation": {},
            "action": {},
            "timestamp": 0.0,
        }
        self._server = ThreadingHTTPServer((self.host, self.port), self._handler_factory())
        self._thread: threading.Thread | None = None

    def _handler_factory(self):
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                if self.path == "/data":
                    with outer._lock:
                        payload = json.dumps(outer._data).encode("utf-8")
                    self.send_response(HTTPStatus.OK)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Cache-Control", "no-store")
                    self.end_headers()
                    self.wfile.write(payload)
                else:
                    self.send_response(HTTPStatus.OK)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.end_headers()
                    self.wfile.write(_DASHBOARD_HTML.encode("utf-8"))

            def log_message(self, format, *args):  # noqa: A003
                return

        return Handler

    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._server.shutdown()
        if self._thread:
            self._thread.join(timeout=1.0)

    def update(self, observation: dict[str, Any], action: dict[str, Any]) -> None:
        with self._lock:
            self._data = {
                "observation": observation,
                "action": action,
                "timestamp": time.time(),
            }


_DASHBOARD_HTML = """
<!DOCTYPE html>
<html lang=\"en\">
<head>
  <meta charset=\"UTF-8\" />
  <title>Brainbot Telemetry</title>
  <style>
    body { font-family: sans-serif; margin: 2rem; }
    pre { background: #f5f5f5; padding: 1rem; border-radius: 6px; }
  </style>
</head>
<body>
  <h1>Brainbot Command/Observation Feed</h1>
  <p>Latest timestamp: <span id="ts">n/a</span></p>
  <h2>Observation</h2>
  <pre id="obs"></pre>
  <h2>Action</h2>
  <pre id="act"></pre>
  <script>
    async function refresh() {
      try {
        const res = await fetch('/data');
        if (!res.ok) return;
        const data = await res.json();
        document.getElementById('ts').textContent = new Date(data.timestamp * 1000).toLocaleString();
        document.getElementById('obs').textContent = JSON.stringify(data.observation, null, 2);
        document.getElementById('act').textContent = JSON.stringify(data.action, null, 2);
      } catch (err) {
        console.error('Refresh error', err);
      }
    }
    refresh();
    setInterval(refresh, 1000);
  </script>
</body>
</html>
"""
